#!/usr/bin/env python3
"""Sal live voice via Twilio ConversationRelay + LLM text relay."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from pathlib import Path

from aiohttp import ClientSession, web
from twilio.twiml.voice_response import Connect, ConversationRelay, VoiceResponse

ROOT = Path(__file__).resolve().parent
PERSONA = (ROOT / "sal_persona.txt").read_text().strip()
LOG = logging.getLogger("sal-live")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PUBLIC_BASE = os.environ.get("SAL_LIVE_PUBLIC_BASE", "").rstrip("/")  # https://xxx.trycloudflare.com
FALLBACK_SAY = os.environ.get(
    "SAL_LIVE_FALLBACK_SAY",
    "Hi Seif, this is Sal. The live line did not connect, so here is a short update only. Check the chat for details. Goodbye.",
)


def load_dotenv() -> None:
    for path in (Path("/home/box/.secrets/twilio.env"), ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _assemble_openai_key_from_parts() -> None:
    """Rebuild OPENAI_API_KEY from OPENAI_API_KEY_P00, P01, ... when it is missing or too short."""
    current = os.environ.get("OPENAI_API_KEY", "")
    if len(current) > 20:
        return
    parts: list[str] = []
    index = 0
    while True:
        name = f"OPENAI_API_KEY_P{index:02d}"
        if name not in os.environ:
            break
        parts.append(os.environ[name])
        index += 1
    if parts:
        os.environ["OPENAI_API_KEY"] = "".join(parts)


def llm_config() -> tuple[str, str, str]:
    """Return (base_url, api_key, model). Prefers xAI Grok, then OpenAI."""
    _assemble_openai_key_from_parts()
    xai_key = os.environ.get("XAI_API_KEY", "")
    if len(xai_key) > 20:
        return (
            os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
            xai_key,
            os.environ.get("XAI_MODEL", "grok-2-latest"),
        )
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if len(openai_key) > 20:
        return (
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_key,
            os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    raise RuntimeError("No XAI_API_KEY or OPENAI_API_KEY set for Sal live voice")


async def llm_reply(session: ClientSession, history: list[dict]) -> str:
    base, key, model = llm_config()
    messages = [{"role": "system", "content": PERSONA}] + history
    async with session.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 220},
        timeout=60,
    ) as resp:
        body = await resp.json()
        if resp.status >= 400:
            LOG.error("LLM error %s %s", resp.status, body)
            return "Sorry Seif, I hit a snag talking to the model. Try again in a moment."
        return body["choices"][0]["message"]["content"].strip()


def twiml_live() -> str:
    if not PUBLIC_BASE:
        # Fallback-only TwiML if public URL unknown
        r = VoiceResponse()
        r.say(FALLBACK_SAY, voice="Polly.Matthew")
        return str(r)
    r = VoiceResponse()
    connect = Connect(action=f"{PUBLIC_BASE}/voice/fallback", method="POST")
    # ConversationRelay: Twilio does STT/TTS; we get text over wss
    wss = PUBLIC_BASE.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    connect.conversation_relay(
        url=wss,
        welcome_greeting="Hi Seif, this is Sal on the live line. What do you need?",
        language="en-US",
    )
    r.append(connect)
    return str(r)


def twiml_fallback() -> str:
    r = VoiceResponse()
    r.say(FALLBACK_SAY, voice="Polly.Matthew")
    return str(r)


async def handle_twiml_live(request: web.Request) -> web.Response:
    xml = twiml_live()
    LOG.info("TwiML live served (%d chars)", len(xml))
    return web.Response(text=xml, content_type="text/xml")


async def handle_fallback(request: web.Request) -> web.Response:
    # Called when Connect ends or fails — speak fallback then hang up
    form = await request.post()
    LOG.info("Fallback action status=%s error=%s", form.get("CallStatus"), form.get("ErrorCode"))
    return web.Response(text=twiml_fallback(), content_type="text/xml")


async def handle_health(request: web.Request) -> web.Response:
    _assemble_openai_key_from_parts()
    xai_key = os.environ.get("XAI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    ok_llm = len(xai_key) > 20 or len(openai_key) > 20
    return web.json_response(
        {
            "ok": True,
            "public_base": PUBLIC_BASE or None,
            "llm_configured": ok_llm,
            "mode": "conversation-relay",
        }
    )


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(autoclose=False, autoping=True, heartbeat=20)
    await ws.prepare(request)
    LOG.info("WebSocket connected from %s", request.remote)
    history: list[dict] = []
    async with ClientSession() as session:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
                continue
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            LOG.info("WS event %s", etype)
            if etype == "setup":
                # optional session start
                continue
            if etype == "prompt":
                user_text = event.get("voicePrompt") or event.get("transcript") or ""
                if not user_text.strip():
                    continue
                history.append({"role": "user", "content": user_text})
                try:
                    reply = await llm_reply(session, history[-12:])
                except Exception as e:
                    LOG.exception("LLM failed")
                    reply = f"I could not reach the language model. {e}"
                history.append({"role": "assistant", "content": reply})
                # Stream as one token with last=true (ConversationRelay protocol)
                await ws.send_json({"type": "text", "token": reply, "last": True})
            elif etype == "interrupt":
                continue
            elif etype == "error":
                LOG.error("CR error %s", event)
    LOG.info("WebSocket closed")
    return ws


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/voice/live", handle_twiml_live)
    app.router.add_get("/voice/live", handle_twiml_live)
    app.router.add_post("/voice/fallback", handle_fallback)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ws/", ws_handler)
    return app


def main() -> None:
    load_dotenv()
    global PUBLIC_BASE
    PUBLIC_BASE = os.environ.get("SAL_LIVE_PUBLIC_BASE", "").rstrip("/")
    port = int(os.environ.get("SAL_LIVE_PORT", "8765"))
    LOG.info("Starting Sal live server on %s public=%s", port, PUBLIC_BASE or "(unset)")
    web.run_app(make_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
