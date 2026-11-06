"""Tests for the Sal Gather speech bridge."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

import server  # noqa: E402


def expected_sig(session_id: str, secret: str = "test-bridge-secret") -> str:
    return hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


class PureTests(unittest.TestCase):
    def test_normalize_spoken_drops_markdown_and_raw_urls(self) -> None:
        spoken = server.normalize_spoken("See **https://example.com/orders** for the list.")
        self.assertNotIn("http", spoken)
        self.assertNotIn("*", spoken)
        self.assertIn("See", spoken)
        self.assertIn("for the list.", spoken)

    def test_split_end_call_is_case_insensitive(self) -> None:
        clean, ended = server.split_end_call("Thanks [end_call] Seif, really [END_CALL].")
        self.assertTrue(ended)
        self.assertNotIn("end_call", clean.lower())
        self.assertIn("Thanks", clean)
        self.assertIn("Seif", clean)

    def test_parse_extraction_accepts_string_items_and_fences(self) -> None:
        summary, items = server.parse_extraction(
            '```json\n{"summary": "Done", "action_items": ["File the PO", {"instruction": "Call the vendor"}]}\n```'
        )
        self.assertEqual(summary, "Done")
        self.assertEqual(
            items,
            [{"instruction": "File the PO"}, {"instruction": "Call the vendor"}],
        )

    def test_parse_extraction_rejects_non_json(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            server.parse_extraction("not json")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved: dict[str, str | None] = {}
        self.setenv("SAL_SESSIONS_PATH", os.path.join(self._tmp.name, "sessions.json"))
        self.setenv("SAL_BRIDGE_SECRET", "test-bridge-secret")
        self.setenv("SAL_LIVE_PUBLIC_BASE", "https://sal-live.onrender.com")
        self.setenv("PUBLIC_BASE", None)
        self.setenv("SAL_TTS_VOICE", None)
        self.setenv("TWILIO_AUTH_TOKEN", None)
        self.setenv("SAL_INBOUND_FROM", None)
        server.clear_store()
        self.llm_calls: list[dict] = []
        self.reply_fn = self.default_reply

        async def fake(messages, *, timeout=12, max_tokens=220):
            self.llm_calls.append(
                {"messages": messages, "timeout": timeout, "max_tokens": max_tokens}
            )
            result = self.reply_fn(messages, timeout, max_tokens)
            if asyncio.iscoroutine(result):
                return await result
            return result

        self._patcher = unittest.mock.patch.object(server, "chat_complete", fake)
        self._patcher.start()
        self.client = TestClient(TestServer(server.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        patcher = getattr(self, "_patcher", None)
        if patcher is not None:
            patcher.stop()
        client = getattr(self, "client", None)
        if client is not None:
            await client.close()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        server.reset_store()

    def setenv(self, key: str, value: str | None) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def auth(self, secret: str = "test-bridge-secret") -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}

    def default_reply(self, messages, timeout, max_tokens):
        system = messages[0]["content"]
        if system.startswith("You extract a post-call record"):
            return json.dumps(
                {
                    "summary": "Seif asked Sal to check the open orders.",
                    "action_items": [{"instruction": "Check the open orders"}],
                }
            )
        last = messages[-1]["content"].lower()
        if "that's all" in last or "thanks sal" in last or "nothing else" in last:
            return "I'll handle it from here. Talk soon, Seif. [END_CALL]"
        return "I'll check the open orders and start that internal work right now."

    async def create(self, **body) -> dict:
        response = await self.client.post("/api/sessions", headers=self.auth(), json=body)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_health_reports_bridge_and_keeps_legacy_fields(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["public_base"], "https://sal-live.onrender.com")
        self.assertIn("llm_configured", body)
        self.assertIsInstance(body["llm_configured"], bool)
        self.assertEqual(body["mode"], "conversation-relay")
        self.assertTrue(body["bridge_configured"])
        self.assertIs(body["gather_ready"], True)

        live = await self.client.post("/voice/live")
        live_xml = await live.text()
        self.assertEqual(live.status, 200)
        self.assertIn("xml", live.content_type)
        self.assertIn("<ConversationRelay", live_xml)
        self.assertIn("wss://sal-live.onrender.com/ws", live_xml)

        fallback = await self.client.post("/voice/fallback", data={"CallStatus": "completed"})
        fallback_xml = await fallback.text()
        self.assertIn("Hi Seif, this is Sal.", fallback_xml)
        self.assertIn("Polly.Matthew", fallback_xml)

    async def test_health_and_api_when_secret_missing(self) -> None:
        self.setenv("SAL_BRIDGE_SECRET", None)
        health = await self.client.get("/health")
        body = await health.json()
        self.assertFalse(body["bridge_configured"])
        self.assertTrue(body["gather_ready"])

        created = await self.client.post("/api/sessions", json={})
        self.assertEqual(created.status, 503)
        self.assertEqual((await created.json())["error"], "bridge_not_configured")

        gather = await self.client.post("/voice/gather?sid=abc&sig=def")
        self.assertEqual(gather.status, 503)

    async def test_api_auth_and_session_creation(self) -> None:
        missing = await self.client.post("/api/sessions", json={})
        self.assertEqual(missing.status, 401)
        wrong = await self.client.post(
            "/api/sessions",
            headers={"Authorization": "Bearer nope"},
            json={},
        )
        self.assertEqual(wrong.status, 401)
        short = await self.client.get("/api/sessions", headers={"Authorization": "Bearer x"})
        self.assertEqual(short.status, 401)

        bad_type = await self.client.post(
            "/api/sessions",
            headers=self.auth(),
            json={"greeting": 12},
        )
        self.assertEqual(bad_type.status, 400)

        created = await self.create(
            greeting="Check A & B <today>.",
            context="Open orders: 14. Stripe available: $1200.",
            caller_note="morning_briefing",
        )
        session_id = created["session_id"]
        self.assertEqual(created["sig"], expected_sig(session_id))
        self.assertEqual(
            created["twiml_url"],
            f"https://sal-live.onrender.com/voice/gather?sid={session_id}&sig={created['sig']}",
        )
        self.assertEqual(
            created["status_url"],
            f"https://sal-live.onrender.com/voice/status?sid={session_id}&sig={created['sig']}",
        )
        self.assertEqual(set(created), {"session_id", "sig", "twiml_url", "status_url"})

        defaults = await self.create()
        gather = await self.client.post(
            f"/voice/gather?sid={defaults['session_id']}&sig={defaults['sig']}"
        )
        xml = await gather.text()
        self.assertIn(server.DEFAULT_GREETING, xml)

        mode = os.stat(server.sessions_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    async def test_public_base_falls_back_to_forwarded_host(self) -> None:
        self.setenv("SAL_LIVE_PUBLIC_BASE", None)
        response = await self.client.post(
            "/api/sessions",
            headers={
                **self.auth(),
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "sal-live.onrender.com",
            },
            json={"caller_note": "on_demand"},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(
            body["twiml_url"].startswith("https://sal-live.onrender.com/voice/gather?")
        )
        self.assertTrue(body["status_url"].startswith("https://sal-live.onrender.com/voice/status?"))

    async def test_gather_twiml_and_signature(self) -> None:
        created = await self.create(greeting="Check A & B <today>.", caller_note="on_demand")
        session_id = created["session_id"]
        sig = created["sig"]

        missing = await self.client.post(f"/voice/gather?sid={session_id}")
        self.assertEqual(missing.status, 403)
        forged = await self.client.post(f"/voice/gather?sid={session_id}&sig=deadbeef")
        self.assertEqual(forged.status, 403)
        ghost = "11111111-1111-4111-8111-111111111111"
        ghost_sig = expected_sig(ghost)
        unknown = await self.client.post(f"/voice/gather?sid={ghost}&sig={ghost_sig}")
        self.assertEqual(unknown.status, 404)

        response = await self.client.post(
            f"/voice/gather?sid={session_id}&sig={sig}",
            data={"CallSid": "CA123"},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("xml", response.content_type)
        xml = await response.text()
        self.assertIn('input="speech"', xml)
        self.assertIn('bargeIn="true"', xml)
        self.assertIn('speechTimeout="auto"', xml)
        self.assertIn('speechModel="phone_call"', xml)
        self.assertIn('enhanced="true"', xml)
        self.assertIn('language="en-US"', xml)
        self.assertIn('method="POST"', xml)
        self.assertIn("Polly.Matthew", xml)
        self.assertIn("Check A &amp; B &lt;today&gt;.", xml)
        self.assertNotIn("<today>", xml)
        self.assertIn(f"/voice/turn?sid={session_id}&amp;sig={sig}", xml)
        self.assertIn(f"/voice/reprompt?sid={session_id}&amp;sig={sig}&amp;n=1", xml)
        self.assertIn("<Gather", xml)
        self.assertIn("<Redirect", xml)

        again = await self.client.get(f"/voice/gather?sid={session_id}&sig={sig}")
        self.assertEqual(again.status, 200)

        fetched = await self.client.get(f"/api/sessions/{session_id}", headers=self.auth())
        body = await fetched.json()
        self.assertEqual(body["status"], "in-progress")
        self.assertEqual(body["call_sid"], "CA123")
        self.assertEqual(body["caller_note"], "on_demand")
        self.assertEqual(len(body["turns"]), 1)
        self.assertEqual(body["turns"][0]["role"], "assistant")
        self.assertEqual(body["turns"][0]["content"], "Check A & B <today>.")
        self.assertTrue(body["turns"][0]["ts"].endswith("Z"))
        self.assertEqual(body["user_utterances"], [])
        self.assertIsNone(body["action_items"])

    async def test_turns_normal_end_call_status_summary_and_ack(self) -> None:
        created = await self.create(
            greeting="Morning Seif, here is the live sheet.",
            context="Open orders: 14. Stripe available: $1200.",
            caller_note="morning_briefing",
        )
        session_id = created["session_id"]
        sig = created["sig"]
        await self.client.post(
            f"/voice/gather?sid={session_id}&sig={sig}",
            data={"CallSid": "CA555"},
        )

        empty = await self.client.post(
            f"/voice/turn?sid={session_id}&sig={sig}",
            data={"SpeechResult": "   ", "CallSid": "CA555"},
        )
        empty_xml = await empty.text()
        self.assertIn("/voice/reprompt?", empty_xml)
        self.assertIn("n=1", empty_xml)
        self.assertEqual(self.llm_calls, [])

        first = await self.client.post(
            f"/voice/turn?sid={session_id}&sig={sig}",
            data={
                "SpeechResult": "Please check the open orders",
                "Confidence": "0.91",
                "CallSid": "CA555",
            },
        )
        self.assertEqual(first.status, 200)
        first_xml = await first.text()
        self.assertIn("<Gather", first_xml)
        self.assertNotIn("<Hangup", first_xml)
        self.assertIn("I'll check the open orders and start that internal work right now.", first_xml)
        self.assertLessEqual(self.llm_calls[0]["timeout"], 12)
        system = self.llm_calls[0]["messages"][0]["content"]
        self.assertIn("Open orders: 14. Stripe available: $1200.", system)
        self.assertIn("[END_CALL]", system)
        self.assertIn("approval card", system)
        self.assertIn("NordSpecAuto", system)
        self.assertIn("morning_briefing", system)

        mid = await self.client.get(f"/api/sessions/{session_id}", headers=self.auth())
        mid_body = await mid.json()
        self.assertIsNone(mid_body["action_items"])
        self.assertEqual(mid_body["user_utterances"], ["Please check the open orders"])
        user_turn = next(turn for turn in mid_body["turns"] if turn["role"] == "user")
        self.assertEqual(user_turn["confidence"], 0.91)
        self.assertEqual(user_turn["content"], "Please check the open orders")

        second = await self.client.post(
            f"/voice/turn?sid={session_id}&sig={sig}",
            data={"SpeechResult": "That's all, thanks Sal", "Confidence": "0.8", "CallSid": "CA555"},
        )
        second_xml = await second.text()
        self.assertIn("<Hangup", second_xml)
        self.assertNotIn("<Gather", second_xml)
        self.assertIn("I'll handle it from here. Talk soon, Seif.", second_xml)
        self.assertNotIn("END_CALL", second_xml)
        self.assertIn("Polly.Matthew", second_xml)

        done = await self.client.get(f"/api/sessions/{session_id}", headers=self.auth())
        done_body = await done.json()
        self.assertTrue(done_body["ended_by_assistant"])
        self.assertEqual(done_body["status"], "completed")
        self.assertIsNotNone(done_body["ended_at"])
        self.assertNotIn("[END_CALL]", json.dumps(done_body["turns"]))
        self.assertEqual(done_body["turns"][-1]["content"], "I'll handle it from here. Talk soon, Seif.")
        self.assertEqual(
            done_body["user_utterances"],
            ["Please check the open orders", "That's all, thanks Sal"],
        )
        self.assertIsNone(done_body["action_items"])

        summarized = await self.client.get(
            f"/api/sessions/{session_id}?summarize=1",
            headers=self.auth(),
        )
        summary_body = await summarized.json()
        self.assertEqual(summary_body["summary"], "Seif asked Sal to check the open orders.")
        self.assertEqual(
            summary_body["action_items"],
            [{"instruction": "Check the open orders"}],
        )
        extract_calls = [
            call
            for call in self.llm_calls
            if call["messages"][0]["content"].startswith("You extract a post-call record")
        ]
        self.assertEqual(len(extract_calls), 1)
        self.assertLessEqual(extract_calls[0]["timeout"], 12)

        status = await self.client.post(
            f"/voice/status?sid={session_id}&sig={sig}",
            data={"CallSid": "CA555", "CallStatus": "completed", "CallDuration": "37"},
        )
        self.assertEqual(status.status, 200)
        self.assertIn("<Response", await status.text())
        extract_calls_after = [
            call
            for call in self.llm_calls
            if call["messages"][0]["content"].startswith("You extract a post-call record")
        ]
        self.assertEqual(len(extract_calls_after), 1)

        final = await self.client.get(f"/api/sessions/{session_id}", headers=self.auth())
        final_body = await final.json()
        self.assertEqual(final_body["call_status"], "completed")
        self.assertEqual(final_body["call_duration"], 37)
        self.assertEqual(final_body["call_sid"], "CA555")
        self.assertEqual(final_body["action_items"][0]["instruction"], "Check the open orders")

        listing = await self.client.get("/api/sessions?unacked=1", headers=self.auth())
        listed = await listing.json()
        self.assertEqual([item["session_id"] for item in listed["sessions"]], [session_id])

        ack = await self.client.post(f"/api/sessions/{session_id}/ack", headers=self.auth())
        self.assertEqual(ack.status, 200)
        self.assertEqual(await ack.json(), {"ok": True, "session_id": session_id, "acked": True})

        after = await self.client.get("/api/sessions?unacked=1", headers=self.auth())
        self.assertEqual((await after.json())["sessions"], [])
        all_sessions = await self.client.get("/api/sessions", headers=self.auth())
        all_body = await all_sessions.json()
        self.assertTrue(all_body["sessions"][0]["acked"])

        missing_ack = await self.client.post(
            "/api/sessions/missing/ack",
            headers=self.auth(),
        )
        self.assertEqual(missing_ack.status, 404)

    async def test_reprompt_then_hangup(self) -> None:
        created = await self.create()
        session_id = created["session_id"]
        sig = created["sig"]
        first = await self.client.post(f"/voice/reprompt?sid={session_id}&sig={sig}&n=1")
        first_xml = await first.text()
        self.assertIn("<Gather", first_xml)
        self.assertIn("I'm still here whenever you're ready, Seif", first_xml)
        self.assertIn("what would you like me to jump on?", first_xml)
        self.assertIn("n=2", first_xml)
        self.assertNotIn("<Hangup", first_xml)

        second = await self.client.post(f"/voice/reprompt?sid={session_id}&sig={sig}&n=2")
        second_xml = await second.text()
        self.assertIn("Sounds like you're tied up", second_xml)
        self.assertIn("everything's in our chat whenever you're ready. Talk soon.", second_xml)
        self.assertIn("<Hangup", second_xml)
        self.assertNotIn("<Gather", second_xml)

        body = await (
            await self.client.get(f"/api/sessions/{session_id}", headers=self.auth())
        ).json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNotNone(body["ended_at"])
        self.assertEqual(body["end_reason"], "no-speech")

        quiet = await self.client.get("/api/sessions?unacked=1", headers=self.auth())
        self.assertEqual((await quiet.json())["sessions"], [])

    async def test_status_extracts_and_falls_back_when_llm_times_out(self) -> None:
        def boom(messages, timeout, max_tokens):
            if messages[0]["content"].startswith("You extract a post-call record"):
                raise asyncio.TimeoutError()
            return "I'll email the shop note after you approve it in chat."

        self.reply_fn = boom
        created = await self.create(context="Customer draft: pickup tomorrow.")
        session_id = created["session_id"]
        sig = created["sig"]
        await self.client.post(
            f"/voice/turn?sid={session_id}&sig={sig}",
            data={"SpeechResult": "Email the customer a pickup time", "Confidence": "0.77"},
        )
        forged = await self.client.post(
            f"/voice/status?sid={session_id}&sig=nope",
            data={"CallStatus": "completed"},
        )
        self.assertEqual(forged.status, 403)

        status = await self.client.post(
            f"/voice/status?sid={session_id}&sig={sig}",
            data={"CallSid": "CA9", "CallStatus": "completed", "CallDuration": "12"},
        )
        self.assertEqual(status.status, 200)
        body = await (
            await self.client.get(f"/api/sessions/{session_id}?summarize=1", headers=self.auth())
        ).json()
        self.assertEqual(body["call_status"], "completed")
        self.assertEqual(body["call_duration"], 12)
        self.assertEqual(body["summary"], "Email the customer a pickup time")
        self.assertEqual(
            body["action_items"],
            [{"instruction": "Email the customer a pickup time"}],
        )

    async def test_token_only_hangup_uses_a_spoken_goodbye(self) -> None:
        self.reply_fn = lambda messages, timeout, max_tokens: "  [end_call]  "
        created = await self.create()
        session_id = created["session_id"]
        response = await self.client.post(
            f"/voice/turn?sid={session_id}&sig={created['sig']}",
            data={"SpeechResult": "Go ahead and do that, bye"},
        )
        xml = await response.text()
        self.assertIn(server.EMPTY_GOODBYE, xml)
        self.assertIn("<Hangup", xml)
        self.assertNotIn("END_CALL", xml)

    async def test_sessions_reload_from_disk_and_keep_latest_hundred(self) -> None:
        created = await self.create(caller_note="persist-me")
        server.reset_store()
        reloaded = await self.client.get(
            f"/api/sessions/{created['session_id']}",
            headers=self.auth(),
        )
        self.assertEqual(reloaded.status, 200)
        self.assertEqual((await reloaded.json())["caller_note"], "persist-me")

        ids = []
        for index in range(104):
            item = await self.create(caller_note=f"n{index}")
            ids.append(item["session_id"])
        self.assertEqual(
            (await self.client.get(f"/api/sessions/{ids[0]}", headers=self.auth())).status,
            404,
        )
        self.assertEqual(
            (await self.client.get(f"/api/sessions/{ids[4]}", headers=self.auth())).status,
            200,
        )
        server.reset_store()
        listing = await self.client.get("/api/sessions", headers=self.auth())
        listed_ids = [item["session_id"] for item in (await listing.json())["sessions"]]
        self.assertEqual(len(listed_ids), 100)
        self.assertIn(ids[-1], listed_ids)
        self.assertNotIn(ids[0], listed_ids)
        self.assertEqual(listed_ids[0], ids[-1])

    async def test_inbound_from_seif_or_twilio_signature(self) -> None:
        allowed = await self.client.post("/voice/inbound", data={"From": "+14133061640", "CallSid": "CAIN"})
        self.assertEqual(allowed.status, 200)
        xml = await allowed.text()
        self.assertIn("<Gather", xml)
        self.assertIn(server.DEFAULT_GREETING, xml)
        listing = await self.client.get("/api/sessions", headers=self.auth())
        inbound = (await listing.json())["sessions"][0]
        self.assertEqual(inbound["caller_note"], "inbound")
        self.assertEqual(inbound["direction"], "inbound")
        self.assertEqual(inbound["from_number"], "+14133061640")
        self.assertEqual(inbound["call_sid"], "CAIN")

        rejected = await self.client.post("/voice/inbound", data={"From": "+15551212000"})
        self.assertEqual(rejected.status, 403)

        self.setenv("TWILIO_AUTH_TOKEN", "inbound-token")
        spoofed = await self.client.post("/voice/inbound", data={"From": "+14133061640"})
        self.assertEqual(spoofed.status, 403)

        params = {"CallSid": "CASIG"}
        url = "https://sal-live.onrender.com/voice/inbound"
        signature = RequestValidator("inbound-token").compute_signature(url, params)
        signed = await self.client.post(
            "/voice/inbound",
            data=params,
            headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(signed.status, 200)
        signed_xml = await signed.text()
        self.assertIn("<Gather", signed_xml)

        other = {"From": "+15557654321", "CallSid": "CAOTHER"}
        other_sig = RequestValidator("inbound-token").compute_signature(url, other)
        other_call = await self.client.post(
            "/voice/inbound",
            data=other,
            headers={"X-Twilio-Signature": other_sig},
        )
        self.assertEqual(other_call.status, 403)


if __name__ == "__main__":
    unittest.main()
