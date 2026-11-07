"""Live lookup turns: filler, barge-in, team bridge, and approval logging."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import lookups  # noqa: E402
import server  # noqa: E402

CHARGE_CREATED = int(datetime(2026, 10, 29, 21, 24, tzinfo=timezone.utc).timestamp())


def tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class LiveLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved: dict[str, str | None] = {}
        self.setenv("SAL_SESSIONS_PATH", os.path.join(self._tmp.name, "sessions.json"))
        self.setenv("SAL_BRIDGE_PATH", os.path.join(self._tmp.name, "bridge.json"))
        self.setenv("SAL_BRIDGE_SECRET", "test-bridge-secret")
        self.setenv("SAL_LIVE_PUBLIC_BASE", "https://sal-live.onrender.com")
        self.setenv("PUBLIC_BASE", None)
        self.setenv("SAL_ASK_TEAM_WAIT_SECONDS", None)
        self.setenv("STRIPE_RESTRICTED_KEY", "rk_test_read")
        self.setenv("ADMIN_READ_TOKEN", "admin-read")
        server.clear_store()
        self.llm_calls: list[dict] = []
        self.gate = asyncio.Event()
        self.block_stripe = False

        async def fake(messages, *, tools=None, timeout=12, max_tokens=220):
            self.llm_calls.append(
                {"messages": messages, "tools": tools, "timeout": timeout, "max_tokens": max_tokens}
            )
            system = messages[0]["content"]
            if system.startswith("You extract a post-call record"):
                return {
                    "role": "assistant",
                    "content": json.dumps({"summary": "Noted.", "action_items": []}),
                }
            tool_messages = [message for message in messages if message.get("role") == "tool"]
            if tool_messages:
                return {
                    "role": "assistant",
                    "content": "Here is what I found. " + tool_messages[-1]["content"],
                }
            last = messages[-1]["content"].lower()
            if "stripe" in last or "charge" in last:
                return tool_call("stripe_recent_payments", {"limit": 1})
            if "website" in last:
                return tool_call("admin_requests", {"limit": 2})
            if "team" in last:
                return tool_call("ask_team", {"question": "Did the bumper ship?"})
            return {"role": "assistant", "content": "The other order is still open."}

        async def fake_stripe(path, params):
            if self.block_stripe:
                await self.gate.wait()
            if path == "/v1/invoices":
                return {"data": []}
            return {
                "data": [
                    {
                        "id": "ch_moe",
                        "amount": 570000,
                        "currency": "usd",
                        "status": "succeeded",
                        "created": CHARGE_CREATED,
                        "description": "Front end",
                        "billing_details": {"name": "MoeModz LLC"},
                    }
                ]
            }

        async def fake_admin():
            return {
                "inquiries": [
                    {
                        "id": 24,
                        "name": "Oreste",
                        "requestedPart": "G30 front end",
                        "status": "paid",
                        "createdAt": "2026-11-01T20:02:00Z",
                        "sellingPrice": 5700,
                    }
                ]
            }

        self._patches = [
            unittest.mock.patch.object(server, "llm_complete", fake),
            unittest.mock.patch.object(lookups, "stripe_get", fake_stripe),
            unittest.mock.patch.object(lookups, "admin_get", fake_admin),
        ]
        for patcher in self._patches:
            patcher.start()
        self.client = TestClient(TestServer(server.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        self.gate.set()
        for patcher in getattr(self, "_patches", []):
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
        await asyncio.sleep(0)

    def setenv(self, key: str, value: str | None) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-bridge-secret"}

    async def create(self) -> dict:
        response = await self.client.post("/api/sessions", headers=self.auth(), json={})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def say(self, created: dict, speech: str) -> str:
        response = await self.client.post(
            f"/voice/turn?sid={created['session_id']}&sig={created['sig']}",
            data={"SpeechResult": speech, "Confidence": "0.9", "CallSid": "CA1"},
        )
        self.assertEqual(response.status, 200, await response.text())
        return await response.text()

    def resume_url(self, created: dict, xml: str) -> str:
        marker = "/voice/resume?"
        start = xml.find(marker)
        self.assertGreaterEqual(start, 0, xml)
        end = xml.find("<", start)
        relative = xml[start:end].replace("&amp;", "&")
        return relative

    async def until(self, created: dict, first_xml: str, needle: str) -> str:
        xml = first_xml
        for _ in range(40):
            if needle in xml and "Give me a sec, checking." not in xml:
                return xml
            if needle in xml and "<Hangup" in xml:
                return xml
            await asyncio.sleep(0.05)
            response = await self.client.post(self.resume_url(created, xml))
            self.assertEqual(response.status, 200, await response.text())
            xml = await response.text()
        self.fail(f"did not hear {needle!r} in {xml}")

    async def test_stripe_lookup_holds_with_barge_in_then_speaks_dollars_and_date(self) -> None:
        created = await self.create()
        xml = await self.say(created, "What did Stripe charge?")
        self.assertIn("<Gather", xml)
        self.assertIn('bargeIn="true"', xml)
        self.assertIn('timeout="1"', xml)
        self.assertIn("Give me a sec, checking.", xml)
        self.assertIn("/voice/resume?", xml)
        self.assertNotIn("5700", xml)
        self.assertIn("stripe_recent_payments", json.dumps(self.llm_calls[0]["tools"]))
        self.assertLessEqual(self.llm_calls[0]["timeout"], 12)

        spoken = await self.until(created, xml, "$5700.00 USD")
        self.assertIn("October 29, 2026, 5:24 PM ET", spoken)
        self.assertIn('bargeIn="true"', spoken)
        self.assertIn("<Gather", spoken)
        session = await (
            await self.client.get(f"/api/sessions/{created['session_id']}", headers=self.auth())
        ).json()
        self.assertIn("$5700.00 USD", session["turns"][-1]["content"])
        self.assertNotIn("Give me a sec", json.dumps(session["turns"]))

    async def test_barge_in_cancels_the_lookup(self) -> None:
        self.block_stripe = True
        created = await self.create()
        held = await self.say(created, "What did Stripe charge?")
        self.assertIn("Give me a sec, checking.", held)
        interrupted = await self.say(created, "Tell me about the other order")
        self.assertIn("The other order is still open.", interrupted)
        self.assertNotIn("STALE", interrupted)
        self.gate.set()
        await asyncio.sleep(0.2)
        stale = await self.client.post(self.resume_url(created, held))
        stale_xml = await stale.text()
        self.assertNotIn("5700", stale_xml)
        self.assertNotIn("ch_moe", stale_xml)
        session = await (
            await self.client.get(f"/api/sessions/{created['session_id']}", headers=self.auth())
        ).json()
        blob = json.dumps(session["turns"])
        self.assertIn("The other order is still open.", blob)
        self.assertNotIn("ch_moe", blob)
        self.assertNotIn("$5700.00", blob)

    async def test_ask_team_waits_for_the_bridge_answer(self) -> None:
        created = await self.create()
        xml = await self.say(created, "Ask the team whether the bumper shipped")
        self.assertIn("Give me a sec, checking.", xml)
        question = None
        for _ in range(30):
            listing = await self.client.get("/bridge/questions", headers=self.auth())
            questions = (await listing.json())["questions"]
            if questions:
                question = questions[0]
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(question)
        assert question is not None
        self.assertEqual(question["question"], "Did the bumper ship?")
        self.assertEqual(question["session_id"], created["session_id"])
        self.assertEqual(question["status"], "pending")
        self.assertIsNone(question["answer"])
        answered = await self.client.post(
            "/bridge/answers",
            headers=self.auth(),
            json={"id": question["id"], "answer": "It shipped yesterday."},
        )
        self.assertEqual(answered.status, 200)
        spoken = await self.until(created, xml, "It shipped yesterday.")
        self.assertIn("Team answer: It shipped yesterday.", spoken)

    async def test_ask_team_follow_up_when_nobody_answers(self) -> None:
        self.setenv("SAL_ASK_TEAM_WAIT_SECONDS", "0.3")
        created = await self.create()
        xml = await self.say(created, "Ask the team whether the bumper shipped")
        spoken = await self.until(created, xml, "follow up after the call")
        self.assertIn("follow up after the call", spoken)
        pending = await self.client.get("/bridge/questions", headers=self.auth())
        questions = (await pending.json())["questions"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["status"], "pending")

    async def test_admin_requests_are_spoken_from_the_tool(self) -> None:
        created = await self.create()
        xml = await self.say(created, "What website requests came in?")
        self.assertIn("Give me a sec, checking.", xml)
        spoken = await self.until(created, xml, "Request #24")
        self.assertIn("Oreste", spoken)
        self.assertIn("Paid in full", spoken)
        self.assertIn("$5700.00 USD", spoken)
        self.assertIn("November 1, 2026, 3:02 PM ET", spoken)

    async def test_write_request_logs_an_action_item_and_does_not_call_tools(self) -> None:
        created = await self.create()
        xml = await self.say(created, "Refund the deposit to the customer")
        self.assertIn("I'll put that in chat for your approval.", xml)
        self.assertIn("I won't send anything or move any money until you tap it.", xml)
        self.assertIn('bargeIn="true"', xml)
        self.assertEqual(self.llm_calls, [])
        session = await (
            await self.client.get(f"/api/sessions/{created['session_id']}", headers=self.auth())
        ).json()
        self.assertEqual(
            session["action_items"],
            [{"instruction": "Refund the deposit to the customer"}],
        )
        pending = await self.client.get("/bridge/questions", headers=self.auth())
        self.assertEqual((await pending.json())["questions"], [])

    async def test_stop_talking_ends_the_thought_and_cancels_a_lookup(self) -> None:
        created = await self.create()
        xml = await self.say(created, "quit talking")
        self.assertIn(">Okay.<", xml)
        self.assertIn("<Gather", xml)
        self.assertIn('bargeIn="true"', xml)
        self.assertNotIn("<Hangup", xml)
        self.assertEqual(self.llm_calls, [])

        self.block_stripe = True
        held = await self.say(created, "What did Stripe charge?")
        self.assertIn("Give me a sec, checking.", held)
        stopped = await self.say(created, "stop talking")
        self.assertIn(">Okay.<", stopped)
        self.gate.set()
        await asyncio.sleep(0.2)
        session = await (
            await self.client.get(f"/api/sessions/{created['session_id']}", headers=self.auth())
        ).json()
        self.assertNotIn("ch_moe", json.dumps(session["turns"]))
        self.assertEqual(session["turns"][-1]["content"], "Okay.")


if __name__ == "__main__":
    unittest.main()
