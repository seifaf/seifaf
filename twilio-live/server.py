#!/usr/bin/env python3
"""Sal live voice: Gather speech bridge plus legacy ConversationRelay."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, web
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

ROOT = Path(__file__).resolve().parent
PERSONA = (ROOT / "sal_persona.txt").read_text(encoding="utf-8").strip()
LOG = logging.getLogger("sal-live")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_GREETING = "Hey Seif, it's Sal on the live line. What can I handle for you?"
REPROMPT_1 = "I'm still here whenever you're ready, Seif — what would you like me to jump on?"
REPROMPT_2 = "Sounds like you're tied up — everything's in our chat whenever you're ready. Talk soon."
EMPTY_GOODBYE = "Alright Seif, I'll take it from here. Talk soon."
SEIF_E164 = "+14133061640"
TERMINAL_CALL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled", "cancelled"}
DEFAULT_SESSIONS_PATH = Path("/tmp/sal_live_sessions.json")
SESSION_KEEP = 100

FALLBACK_SAY = os.environ.get(
    "SAL_LIVE_FALLBACK_SAY",
    "Hi Seif, this is Sal. The live line did not connect, so here is a short update only. Check the chat for details. Goodbye.",
)

SPOKEN_RULES = """
You are mid-call, speaking out loud to Seif. Twilio will read your reply with text-to-speech and then listen.

Rules for every reply:
- One to three short spoken sentences. Sound natural, warm, and concise.
- Zero markdown, no bullet or numbered lists, and no raw URLs.
- Never invent facts that are missing from the live operational briefing. If the briefing does not contain the answer, say you will check.
- When Seif gives an instruction or a decision, clearly repeat it back and confirm it so he knows you will execute it right after this call.
- Internal work starts immediately. Any outgoing customer message or money move will place a ready-to-tap approval card in chat. Say that naturally when the instruction needs one. Do not claim a customer was already contacted or that money already moved.
- If Seif indicates he is done or hanging up, for example "that's all", "nothing else", "go ahead and do that, bye", or "thanks Sal", finish with a warm sign-off and append the literal token [END_CALL] at the very end of your reply. Do not speak the token as words; it is a control marker.
""".strip()

EXTRACT_SYSTEM = """
You extract a post-call record from a spoken phone call between Sal and Seif.
Return ONLY a JSON object with this exact shape and no markdown fences:
{"summary": "short plain-text recap of the call", "action_items": [{"instruction": "one instruction or decision Seif stated, in plain text"}]}
Rules:
- Each action item is one instruction or decision Seif actually spoke, in plain text.
- Do not invent tasks, numbers, or names.
- If Seif gave no instruction or decision, return an empty action_items list.
- The summary is one or two plain sentences.
""".strip()

_END_CALL_RE = re.compile(r"\[\s*end_call\s*\]", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_sessions: dict[str, dict] = {}
_loaded = False
_lock: asyncio.Lock | None = None
_seq = 0


def load_dotenv() -> None:
    for path in (Path("/home/box/.secrets/twilio.env"), ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


def persona_text() -> str:
    try:
        return (ROOT / "sal_persona.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return PERSONA


def configured_public_base() -> str:
    return (os.environ.get("PUBLIC_BASE") or os.environ.get("SAL_LIVE_PUBLIC_BASE") or "").strip().rstrip("/")


def public_base(request: web.Request | None = None) -> str:
    configured = configured_public_base()
    if configured:
        return configured
    if request is None:
        return ""
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    return f"{proto}://{host}".rstrip("/")


def tts_voice() -> str:
    return (os.environ.get("SAL_TTS_VOICE") or "").strip() or "Polly.Matthew"


def sessions_path() -> Path:
    raw = (os.environ.get("SAL_SESSIONS_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_SESSIONS_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def session_sig(session_id: str) -> str:
    secret = os.environ.get("SAL_BRIDGE_SECRET", "")
    digest = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def bridge_secret() -> str:
    return os.environ.get("SAL_BRIDGE_SECRET", "")


def reset_store() -> None:
    """Drop the in-memory session cache. The sessions file is left in place."""
    global _loaded, _lock, _seq
    _sessions.clear()
    _loaded = False
    _lock = None
    _seq = 0


def clear_store() -> None:
    """Drop memory and delete the sessions file. Used by tests."""
    reset_store()
    path = sessions_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        LOG.exception("could not delete sessions file")


def get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _read_disk() -> dict:
    path = sessions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.exception("could not read sessions file")
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, dict] = {}
    for value in data.values():
        if isinstance(value, dict) and value.get("session_id"):
            clean[str(value["session_id"])] = value
    return clean


def _ensure_loaded() -> None:
    global _loaded, _seq
    if _loaded:
        return
    loaded = _read_disk()
    _sessions.clear()
    _sessions.update(loaded)
    highest = 0
    for session in _sessions.values():
        try:
            highest = max(highest, int(session.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    _seq = max(_seq, highest)
    _loaded = True


def _persist() -> None:
    ranked = sorted(
        _sessions.values(),
        key=lambda session: (session.get("created_at") or "", int(session.get("seq") or 0)),
        reverse=True,
    )[:SESSION_KEEP]
    trimmed = {session["session_id"]: session for session in ranked}
    if len(trimmed) != len(_sessions):
        _sessions.clear()
        _sessions.update(trimmed)
    path = sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".sal-sessions-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(trimmed, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def create_session(
    *,
    greeting: str | None,
    context: str | None,
    caller_note: str | None,
    direction: str = "outbound",
    from_number: str | None = None,
) -> dict:
    async with get_lock():
        _ensure_loaded()
        session_id = str(uuid.uuid4())
        timestamp = now_iso()
        session = {
            "session_id": session_id,
            "seq": _next_seq(),
            "greeting": (greeting or "").strip() or DEFAULT_GREETING,
            "context": context or "",
            "caller_note": (caller_note or "").strip(),
            "direction": direction,
            "from_number": from_number or None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "ended_at": None,
            "status": "created",
            "call_sid": None,
            "call_status": None,
            "call_duration": None,
            "acked": False,
            "ended_by_assistant": False,
            "end_reason": None,
            "greeting_recorded": False,
            "turns": [],
            "summary": None,
            "action_items": None,
        }
        _sessions[session_id] = session
        _persist()
        return copy.deepcopy(session)


async def snapshot_session(session_id: str) -> dict | None:
    async with get_lock():
        _ensure_loaded()
        session = _sessions.get(session_id)
        return copy.deepcopy(session) if session else None


async def snapshot_sessions() -> list[dict]:
    async with get_lock():
        _ensure_loaded()
        ranked = sorted(
            _sessions.values(),
            key=lambda session: (session.get("created_at") or "", int(session.get("seq") or 0)),
            reverse=True,
        )
        return copy.deepcopy(ranked)


async def update_session(session_id: str, mutate) -> dict | None:
    async with get_lock():
        _ensure_loaded()
        session = _sessions.get(session_id)
        if session is None:
            return None
        mutate(session)
        session["updated_at"] = now_iso()
        _persist()
        return copy.deepcopy(session)


def user_utterances(session: dict) -> list[str]:
    lines: list[str] = []
    for turn in session.get("turns") or []:
        if turn.get("role") != "user":
            continue
        text = (turn.get("content") or "").strip()
        if text:
            lines.append(text)
    return lines


def public_view(session: dict) -> dict:
    return {
        "session_id": session.get("session_id"),
        "caller_note": session.get("caller_note") or "",
        "direction": session.get("direction") or "outbound",
        "from_number": session.get("from_number"),
        "call_sid": session.get("call_sid"),
        "call_status": session.get("call_status"),
        "call_duration": session.get("call_duration"),
        "status": session.get("status"),
        "created_at": session.get("created_at"),
        "ended_at": session.get("ended_at"),
        "acked": bool(session.get("acked")),
        "ended_by_assistant": bool(session.get("ended_by_assistant")),
        "end_reason": session.get("end_reason"),
        "greeting": session.get("greeting"),
        "context": session.get("context") or "",
        "turns": session.get("turns") or [],
        "user_utterances": user_utterances(session),
        "summary": session.get("summary"),
        "action_items": session.get("action_items"),
    }


def signed_url(request: web.Request, path: str, session_id: str, **extra: object) -> str:
    params = {"sid": session_id, "sig": session_sig(session_id)}
    for key, value in extra.items():
        params[key] = str(value)
    return f"{public_base(request)}{path}?{urlencode(params)}"


def say_chunks(text: str, limit: int = 3500) -> list[str]:
    spoken = re.sub(r"\s+", " ", (text or "").strip())
    if not spoken:
        return ["Okay."]
    chunks: list[str] = []
    rest = spoken
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        piece = rest[:cut].strip()
        rest = rest[cut:].strip()
        if piece:
            chunks.append(piece)
    return chunks or ["Okay."]


def render_gather(say_text: str, action_url: str, redirect_url: str | None) -> str:
    response = VoiceResponse()
    gather = response.gather(
        input="speech",
        barge_in=True,
        speech_timeout="auto",
        speech_model="phone_call",
        enhanced=True,
        language="en-US",
        action=action_url,
        method="POST",
        timeout=12,
    )
    voice = tts_voice()
    for chunk in say_chunks(say_text):
        gather.say(chunk, voice=voice)
    if redirect_url:
        response.redirect(redirect_url, method="POST")
    return str(response)


def render_say_hangup(say_text: str) -> str:
    response = VoiceResponse()
    voice = tts_voice()
    for chunk in say_chunks(say_text):
        response.say(chunk, voice=voice)
    response.hangup()
    return str(response)


def split_end_call(text: str) -> tuple[str, bool]:
    found = _END_CALL_RE.search(text or "") is not None
    clean = _END_CALL_RE.sub(" ", text or "")
    return clean, found


def normalize_spoken(text: str) -> str:
    """Collapse whitespace and drop markdown or raw URLs before text-to-speech."""
    cleaned = _URL_RE.sub("", text or "")
    cleaned = cleaned.replace("`", "").replace("*", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def build_system(session: dict) -> str:
    briefing = (session.get("context") or "").strip()
    if not briefing:
        briefing = "(No live briefing was attached to this call. Do not guess operational facts.)"
    note = (session.get("caller_note") or "").strip()
    note_line = f"Call tag: {note}\n" if note else ""
    return f"{persona_text()}\n\n{SPOKEN_RULES}\n\n{note_line}Live operational briefing for this call:\n{briefing}"


def parse_extraction(raw: str) -> tuple[str, list[dict]]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("extraction was not an object")
    summary = data.get("summary") or ""
    if not isinstance(summary, str):
        summary = str(summary)
    items_raw = data.get("action_items") or []
    if not isinstance(items_raw, list):
        raise ValueError("action_items was not a list")
    items: list[dict] = []
    for item in items_raw:
        if isinstance(item, str):
            instruction = item.strip()
        elif isinstance(item, dict):
            instruction = str(
                item.get("instruction") or item.get("text") or item.get("decision") or ""
            ).strip()
        else:
            continue
        if instruction:
            items.append({"instruction": instruction})
    return summary.strip(), items


def fallback_extraction(session: dict) -> tuple[str, list[dict]]:
    utterances = user_utterances(session)
    return " ".join(utterances).strip(), [{"instruction": line} for line in utterances]


def parse_confidence(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_duration(value: object) -> int | str | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return str(value)


async def chat_complete(messages: list[dict], *, timeout: float = 12, max_tokens: int = 220) -> str:
    base, key, model = llm_config()
    client_timeout = ClientTimeout(total=timeout)
    async with ClientSession(timeout=client_timeout) as session:
        async with session.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": max_tokens,
            },
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception as exc:
                LOG.error("LLM returned a non-JSON body status=%s", resp.status)
                raise RuntimeError(f"LLM HTTP {resp.status}") from exc
            if resp.status >= 400:
                LOG.error("LLM error status=%s", resp.status)
                raise RuntimeError(f"LLM HTTP {resp.status}")
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("LLM response missing content") from exc
            return str(content or "").strip()


async def extract_action_items(session: dict) -> tuple[str, list[dict]]:
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": transcript_text(session)},
    ]
    try:
        raw = await chat_complete(messages, timeout=12, max_tokens=500)
        summary, items = parse_extraction(raw)
        if not summary:
            summary, _ignored = fallback_extraction(session)
        return summary, items
    except Exception:
        LOG.exception("action item extraction failed session=%s", session.get("session_id"))
        return fallback_extraction(session)


def transcript_text(session: dict) -> str:
    lines: list[str] = []
    for turn in session.get("turns") or []:
        role = turn.get("role")
        speaker = "Seif" if role == "user" else "Sal"
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if role == "user" and turn.get("confidence") is not None:
            lines.append(f"{speaker} (confidence {turn['confidence']}): {content}")
        else:
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


async def ensure_action_items(session_id: str) -> dict | None:
    existing = await snapshot_session(session_id)
    if existing is None:
        return None
    if existing.get("action_items") is not None or not user_utterances(existing):
        return existing
    summary, items = await extract_action_items(existing)

    def apply(session: dict) -> None:
        if session.get("action_items") is None:
            session["summary"] = summary
            session["action_items"] = items

    updated = await update_session(session_id, apply)
    return updated or existing


def conversation_messages(session: dict) -> list[dict]:
    messages = [{"role": "system", "content": build_system(session)}]
    for turn in session.get("turns") or []:
        if turn.get("role") not in {"user", "assistant"}:
            continue
        content = (turn.get("content") or "").strip()
        if content:
            messages.append({"role": turn["role"], "content": content})
    if len(messages) > 17:
        messages = [messages[0]] + messages[-16:]
    return messages


async def llm_reply(session: ClientSession, history: list[dict]) -> str:
    base, key, model = llm_config()
    messages = [{"role": "system", "content": persona_text()}] + history
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
    base = configured_public_base()
    if not base:
        response = VoiceResponse()
        response.say(FALLBACK_SAY, voice="Polly.Matthew")
        return str(response)
    response = VoiceResponse()
    connect = response.connect(action=f"{base}/voice/fallback", method="POST")
    wss = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    connect.conversation_relay(
        url=wss,
        welcome_greeting="Hi Seif, this is Sal on the live line. What do you need?",
        language="en-US",
    )
    return str(response)


def twiml_fallback() -> str:
    response = VoiceResponse()
    response.say(FALLBACK_SAY, voice="Polly.Matthew")
    return str(response)


def xml_response(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/xml", status=status)


def authorize_api(request: web.Request) -> web.Response | None:
    secret = bridge_secret()
    if not secret:
        return web.json_response({"error": "bridge_not_configured"}, status=503)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token or not constant_time_equal(token, secret):
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


def authorize_voice(request: web.Request) -> web.Response | None:
    if not bridge_secret():
        return web.Response(status=503, text="bridge_not_configured")
    session_id = request.query.get("sid", "")
    provided = request.query.get("sig", "")
    if not session_sig_ok(session_id, provided):
        return web.Response(status=403, text="forbidden")
    return None


def session_sig_ok(session_id: str, provided: str) -> bool:
    if not bridge_secret() or not session_id or not provided:
        return False
    return constant_time_equal(provided, session_sig(session_id))


async def read_json_object(request: web.Request) -> dict:
    raw = await request.read()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected json object")
    return data


def optional_str(data: dict, key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def query_flag(request: web.Request, name: str) -> bool:
    return (request.query.get(name) or "").strip().lower() in {"1", "true", "yes"}


async def twilio_params(request: web.Request) -> dict[str, str]:
    if request.method != "POST":
        return {}
    try:
        form = await request.post()
    except Exception:
        LOG.warning("unreadable Twilio form")
        return {}
    params: dict[str, str] = {}
    for key in form.keys():
        value = form.get(key)
        if value is not None:
            params[str(key)] = str(value)
    return params


def digits_e164(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def candidate_signature_urls(request: web.Request) -> list[str]:
    path = str(request.rel_url)
    if not path.startswith("/"):
        path = "/" + path
    urls: list[str] = []
    base = configured_public_base()
    if base:
        urls.append(base + path)
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "").split(",")[0].strip()
    if proto and host:
        urls.append(f"{proto}://{host}{path}")
    urls.append(str(request.url))
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def twilio_signature_ok(request: web.Request, params: dict[str, str], token: str) -> bool:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not token:
        return False
    validator = RequestValidator(token)
    for url in candidate_signature_urls(request):
        try:
            if validator.validate(url, params, signature):
                return True
        except Exception:
            LOG.exception("Twilio signature check failed")
    return False


def inbound_allowed(request: web.Request, params: dict[str, str]) -> bool:
    expected = digits_e164(os.environ.get("SAL_INBOUND_FROM", SEIF_E164)) or SEIF_E164
    from_raw = (params.get("From") or request.query.get("From") or "").strip()
    from_ok = bool(from_raw) and digits_e164(from_raw) == expected
    token = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    if token and not twilio_signature_ok(request, params, token):
        return False
    if from_raw and not from_ok:
        return False
    if token:
        return True
    return from_ok


async def handle_health(request: web.Request) -> web.Response:
    _assemble_openai_key_from_parts()
    xai_key = os.environ.get("XAI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    base = configured_public_base()
    return web.json_response(
        {
            "ok": True,
            "public_base": base or None,
            "llm_configured": len(xai_key) > 20 or len(openai_key) > 20,
            "mode": "conversation-relay",
            "bridge_configured": bool(os.environ.get("SAL_BRIDGE_SECRET")),
            "gather_ready": True,
        }
    )


async def handle_twiml_live(request: web.Request) -> web.Response:
    xml = twiml_live()
    LOG.info("TwiML live served (%d chars)", len(xml))
    return xml_response(xml)


async def handle_fallback(request: web.Request) -> web.Response:
    form = await request.post()
    LOG.info("Fallback action status=%s error=%s", form.get("CallStatus"), form.get("ErrorCode"))
    return xml_response(twiml_fallback())


async def handle_create_session(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    try:
        data = await read_json_object(request)
        greeting = optional_str(data, "greeting")
        context = optional_str(data, "context")
        caller_note = optional_str(data, "caller_note")
    except web.HTTPBadRequest as exc:
        return web.json_response({"error": "invalid_json", "detail": exc.text}, status=400)
    except ValueError as exc:
        return web.json_response({"error": "invalid_body", "detail": str(exc)}, status=400)
    session = await create_session(greeting=greeting, context=context, caller_note=caller_note)
    session_id = session["session_id"]
    LOG.info("session created id=%s note=%s", session_id, session.get("caller_note") or "")
    return web.json_response(
        {
            "session_id": session_id,
            "sig": session_sig(session_id),
            "twiml_url": signed_url(request, "/voice/gather", session_id),
            "status_url": signed_url(request, "/voice/status", session_id),
        }
    )


async def handle_list_sessions(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    unacked = query_flag(request, "unacked")
    published = []
    for session in await snapshot_sessions():
        view = public_view(session)
        if unacked and (view["acked"] or not view["user_utterances"]):
            continue
        published.append(view)
    return web.json_response({"sessions": published})


async def handle_get_session(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    session_id = request.match_info["sid"]
    if query_flag(request, "summarize"):
        session = await ensure_action_items(session_id)
    else:
        session = await snapshot_session(session_id)
    if session is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(public_view(session))


async def handle_ack_session(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    session_id = request.match_info["sid"]

    def apply(session: dict) -> None:
        session["acked"] = True

    updated = await update_session(session_id, apply)
    if updated is None:
        return web.json_response({"error": "not_found"}, status=404)
    LOG.info("session acked id=%s", session_id)
    return web.json_response({"ok": True, "session_id": session_id, "acked": True})


async def open_gather(request: web.Request, session_id: str, call_sid: str) -> str:
    def apply(session: dict) -> None:
        if session.get("status") != "completed":
            session["status"] = "in-progress"
        if call_sid:
            session["call_sid"] = call_sid
        if not session.get("greeting_recorded"):
            session["turns"].append(
                {
                    "role": "assistant",
                    "content": session.get("greeting") or DEFAULT_GREETING,
                    "ts": now_iso(),
                }
            )
            session["greeting_recorded"] = True

    session = await update_session(session_id, apply)
    if session is None:
        return ""
    return render_gather(
        session.get("greeting") or DEFAULT_GREETING,
        signed_url(request, "/voice/turn", session_id),
        signed_url(request, "/voice/reprompt", session_id, n=1),
    )


async def handle_gather(request: web.Request) -> web.Response:
    denied = authorize_voice(request)
    if denied:
        return denied
    session_id = request.query.get("sid", "")
    params = await twilio_params(request)
    xml = await open_gather(request, session_id, params.get("CallSid", ""))
    if not xml:
        return web.Response(status=404, text="not_found")
    LOG.info("gather opened session=%s", session_id)
    return xml_response(xml)


async def handle_turn(request: web.Request) -> web.Response:
    denied = authorize_voice(request)
    if denied:
        return denied
    session_id = request.query.get("sid", "")
    if await snapshot_session(session_id) is None:
        return web.Response(status=404, text="not_found")
    params = await twilio_params(request)
    speech = (params.get("SpeechResult") or "").strip()
    if not speech:
        response = VoiceResponse()
        response.redirect(signed_url(request, "/voice/reprompt", session_id, n=1), method="POST")
        return xml_response(str(response))

    confidence = parse_confidence(params.get("Confidence"))
    call_sid = params.get("CallSid") or ""

    def apply_user(session: dict) -> None:
        if call_sid:
            session["call_sid"] = call_sid
        if session.get("status") != "completed":
            session["status"] = "in-progress"
        session["turns"].append(
            {
                "role": "user",
                "content": speech,
                "confidence": confidence,
                "ts": now_iso(),
            }
        )

    snapshot = await update_session(session_id, apply_user)
    if snapshot is None:
        return web.Response(status=404, text="not_found")
    try:
        raw_reply = await chat_complete(conversation_messages(snapshot), timeout=12, max_tokens=180)
    except Exception:
        LOG.exception("spoken reply failed session=%s", session_id)
        raw_reply = "Sorry Seif, I hit a snag. Say that once more and I'll stay with you."
    clean, end_call = split_end_call(raw_reply)
    clean = normalize_spoken(clean)
    if not clean:
        clean = EMPTY_GOODBYE if end_call else "Sorry Seif, I missed that. What would you like me to jump on?"

    def apply_assistant(session: dict) -> None:
        session["turns"].append({"role": "assistant", "content": clean, "ts": now_iso()})
        if end_call:
            session["ended_by_assistant"] = True
            session["status"] = "completed"
            session["end_reason"] = "assistant"
            if not session.get("ended_at"):
                session["ended_at"] = now_iso()

    await update_session(session_id, apply_assistant)
    LOG.info("turn session=%s chars=%d end=%s", session_id, len(speech), end_call)
    if end_call:
        return xml_response(render_say_hangup(clean))
    return xml_response(
        render_gather(
            clean,
            signed_url(request, "/voice/turn", session_id),
            signed_url(request, "/voice/reprompt", session_id, n=1),
        )
    )


async def handle_reprompt(request: web.Request) -> web.Response:
    denied = authorize_voice(request)
    if denied:
        return denied
    session_id = request.query.get("sid", "")
    if await snapshot_session(session_id) is None:
        return web.Response(status=404, text="not_found")
    try:
        count = int(request.query.get("n", "1"))
    except ValueError:
        count = 1
    if count < 1:
        count = 1
    if count == 1:
        return xml_response(
            render_gather(
                REPROMPT_1,
                signed_url(request, "/voice/turn", session_id),
                signed_url(request, "/voice/reprompt", session_id, n=2),
            )
        )

    def apply(session: dict) -> None:
        session["status"] = "completed"
        if not session.get("ended_at"):
            session["ended_at"] = now_iso()
        if not session.get("end_reason"):
            session["end_reason"] = "no-speech"

    await update_session(session_id, apply)
    LOG.info("reprompt hangup session=%s n=%s", session_id, count)
    return xml_response(render_say_hangup(REPROMPT_2))


async def handle_status(request: web.Request) -> web.Response:
    denied = authorize_voice(request)
    if denied:
        return denied
    session_id = request.query.get("sid", "")
    if await snapshot_session(session_id) is None:
        return web.Response(status=404, text="not_found")
    params = await twilio_params(request)
    call_sid = params.get("CallSid") or ""
    call_status = params.get("CallStatus") or ""
    duration = parse_duration(params.get("CallDuration"))
    terminal = call_status.lower() in TERMINAL_CALL_STATUSES

    def apply(session: dict) -> None:
        if call_sid:
            session["call_sid"] = call_sid
        if call_status:
            session["call_status"] = call_status
        if duration is not None:
            session["call_duration"] = duration
        if terminal:
            session["status"] = "completed"
            if not session.get("ended_at"):
                session["ended_at"] = now_iso()
            if not session.get("end_reason"):
                session["end_reason"] = call_status.lower()

    await update_session(session_id, apply)
    if terminal:
        await ensure_action_items(session_id)
    LOG.info("status session=%s call_status=%s", session_id, call_status or "(none)")
    return xml_response(str(VoiceResponse()))


async def handle_inbound(request: web.Request) -> web.Response:
    if not bridge_secret():
        return web.Response(status=503, text="bridge_not_configured")
    params = await twilio_params(request)
    if not inbound_allowed(request, params):
        LOG.info("inbound rejected from=%s", params.get("From") or "")
        return web.Response(status=403, text="forbidden")
    session = await create_session(
        greeting=None,
        context="",
        caller_note="inbound",
        direction="inbound",
        from_number=params.get("From") or None,
    )
    xml = await open_gather(request, session["session_id"], params.get("CallSid", ""))
    LOG.info("inbound session=%s", session["session_id"])
    return xml_response(xml)


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
                continue
            if etype == "prompt":
                user_text = event.get("voicePrompt") or event.get("transcript") or ""
                if not user_text.strip():
                    continue
                history.append({"role": "user", "content": user_text})
                try:
                    reply = await llm_reply(session, history[-12:])
                except Exception as exc:
                    LOG.exception("LLM failed")
                    reply = f"I could not reach the language model. {exc}"
                history.append({"role": "assistant", "content": reply})
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
    app.router.add_post("/api/sessions", handle_create_session)
    app.router.add_get("/api/sessions", handle_list_sessions)
    app.router.add_get("/api/sessions/{sid}", handle_get_session)
    app.router.add_post("/api/sessions/{sid}/ack", handle_ack_session)
    app.router.add_get("/voice/gather", handle_gather)
    app.router.add_post("/voice/gather", handle_gather)
    app.router.add_post("/voice/turn", handle_turn)
    app.router.add_post("/voice/reprompt", handle_reprompt)
    app.router.add_post("/voice/status", handle_status)
    app.router.add_get("/voice/inbound", handle_inbound)
    app.router.add_post("/voice/inbound", handle_inbound)
    return app


def main() -> None:
    load_dotenv()
    port = int(os.environ.get("SAL_LIVE_PORT", "8765"))
    LOG.info(
        "Starting Sal live server on %s public=%s bridge=%s",
        port,
        configured_public_base() or "(unset)",
        bool(os.environ.get("SAL_BRIDGE_SECRET")),
    )
    web.run_app(make_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
