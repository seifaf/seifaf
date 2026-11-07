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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, web
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

import lookups

ROOT = Path(__file__).resolve().parent
PERSONA = (ROOT / "sal_persona.txt").read_text(encoding="utf-8").strip()
LOG = logging.getLogger("sal-live")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_GREETING = "Hey Seif, it's Sal, Chief Operations Manager at NordSpecAuto. What can I handle for you?"
REPROMPT_1 = "I'm still here whenever you're ready, Seif — what would you like me to jump on?"
REPROMPT_2 = "Sounds like you're tied up — everything's in our chat whenever you're ready. Talk soon."
EMPTY_GOODBYE = "Alright Seif, I'll take it from here. Talk soon."
SEIF_E164 = "+14133061640"
TERMINAL_CALL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled", "cancelled"}
DEFAULT_SESSIONS_PATH = Path("/tmp/sal_live_sessions.json")
DEFAULT_BRIDGE_PATH = Path("/tmp/sal_live_bridge.json")
SESSION_KEEP = 100
QUESTION_KEEP = 100
LOOKUP_FILLER = "Give me a sec, checking."
LOOKUP_STILL = "Still checking."
LOOKUP_FOLLOWUP = "I'll follow up on that after this call."
WRITE_REPLY = (
    "I'll put that in chat for your approval. I won't send anything or move any money until you tap it."
)
STOP_REPLY = "Okay."

FALLBACK_SAY = os.environ.get(
    "SAL_LIVE_FALLBACK_SAY",
    "Hey Seif, it's Sal, Chief Operations Manager at NordSpecAuto. The live line did not connect, so here is a short update only. Check the chat for details. Goodbye.",
)

SPOKEN_RULES = """
You are mid-call, speaking out loud to Seif. Twilio will read your reply with text-to-speech and then listen.

Rules for every reply:
- One to three short spoken sentences. Sound natural, warm, and concise.
- Zero markdown, no bullet or numbered lists, and no raw URLs.
- Only state a figure when it is in the live operational briefing or in a tool result from this call. Say the date and the unit with that figure. If neither source has it, use a read-only tool now. Do not invent a number and do not promise to check later when a tool can answer on this call.
- Read-only tools: stripe_recent_payments, admin_requests, and ask_team. Never send, refund, or take a payment with a tool.
- If Seif asks you to send a message, issue a refund, or move money, say you will put it in chat for approval. Do not claim a customer was already contacted or that money already moved.
- If Seif says stop talking or quit talking, reply with only Okay and do not add another thought.
- When Seif gives an instruction or a decision, clearly repeat it back and confirm it so he knows you will execute it right after this call.
- Internal work starts immediately. Any outgoing customer message or money move will place a ready-to-tap approval card in chat. Say that naturally when the instruction needs one.
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
_questions: dict[str, dict] = {}
_bridge_loaded = False
_bridge_lock: asyncio.Lock | None = None
_lookups: dict[str, dict] = {}
_lookup_tasks: dict[str, asyncio.Task] = {}


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


def bridge_path() -> Path:
    raw = (os.environ.get("SAL_BRIDGE_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_BRIDGE_PATH


def ask_team_wait_seconds() -> float:
    raw = (os.environ.get("SAL_ASK_TEAM_WAIT_SECONDS") or "").strip()
    if not raw:
        return 20.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 20.0


def lookup_budget_seconds() -> float:
    return ask_team_wait_seconds() + 8.0


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
    """Drop the in-memory session and bridge caches. Files are left in place."""
    global _loaded, _lock, _seq, _bridge_loaded, _bridge_lock
    for task in list(_lookup_tasks.values()):
        if not task.done():
            task.cancel()
    _lookup_tasks.clear()
    _lookups.clear()
    _sessions.clear()
    _questions.clear()
    _loaded = False
    _bridge_loaded = False
    _lock = None
    _bridge_lock = None
    _seq = 0


def clear_store() -> None:
    """Drop memory and delete the session and bridge files. Used by tests."""
    reset_store()
    for path in (sessions_path(), bridge_path()):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            LOG.exception("could not delete store file")


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
        "action_items": displayed_action_items(session),
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


def render_gather(
    say_text: str,
    action_url: str,
    redirect_url: str | None,
    *,
    timeout: int = 12,
) -> str:
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
        timeout=timeout,
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
    message = await llm_complete(messages, tools=None, timeout=timeout, max_tokens=max_tokens)
    if isinstance(message, str):
        return message.strip()
    return str((message or {}).get("content") or "").strip()


async def llm_complete(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    timeout: float = 12,
    max_tokens: int = 220,
) -> dict:
    """One chat-completions call. Tests that patch chat_complete keep working."""
    if chat_complete is not _ORIGINAL_CHAT_COMPLETE:
        text = await chat_complete(messages, timeout=timeout, max_tokens=max_tokens)
        if isinstance(text, dict):
            return text
        return {"role": "assistant", "content": text, "tool_calls": None}
    return await _llm_http(messages, tools=tools, timeout=timeout, max_tokens=max_tokens)


async def _llm_http(
    messages: list[dict],
    *,
    tools: list[dict] | None,
    timeout: float,
    max_tokens: int,
) -> dict:
    base, key, model = llm_config()
    payload = lookups.chat_payload(model, messages, tools=tools, max_tokens=max_tokens)
    client_timeout = ClientTimeout(total=timeout)
    async with ClientSession(timeout=client_timeout) as session:
        async with session.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
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
                message = body["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("LLM response missing message") from exc
            if not isinstance(message, dict):
                raise RuntimeError("LLM response missing message")
            return message


_ORIGINAL_CHAT_COMPLETE = chat_complete


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


def logged_items(session: dict) -> list[dict]:
    items: list[dict] = []
    for item in session.get("logged_action_items") or []:
        if isinstance(item, dict):
            text = str(item.get("instruction") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
        else:
            continue
        if text:
            items.append({"instruction": text})
    return items


def merge_action_items(*groups: list[dict] | None) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("instruction") or "").strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                merged.append({"instruction": text})
    return merged


def displayed_action_items(session: dict) -> list[dict] | None:
    extracted = session.get("action_items")
    logged = logged_items(session)
    if extracted is None:
        return logged or None
    if not isinstance(extracted, list):
        return logged or None
    return merge_action_items(extracted, logged)


async def log_action_item(session_id: str, instruction: str) -> None:
    text = (instruction or "").strip()
    if not text:
        return

    def apply(session: dict) -> None:
        items = session.setdefault("logged_action_items", [])
        if not any(isinstance(item, dict) and item.get("instruction") == text for item in items):
            items.append({"instruction": text})

    await update_session(session_id, apply)


async def ensure_action_items(session_id: str) -> dict | None:
    existing = await snapshot_session(session_id)
    if existing is None:
        return None
    if not user_utterances(existing):
        return existing
    if existing.get("action_items") is not None:
        merged = merge_action_items(existing.get("action_items"), logged_items(existing))
        if merged != existing.get("action_items"):

            def apply_merge(session: dict) -> None:
                session["action_items"] = merge_action_items(
                    session.get("action_items"), logged_items(session)
                )

            updated = await update_session(session_id, apply_merge)
            return updated or existing
        return existing
    summary, items = await extract_action_items(existing)

    def apply(session: dict) -> None:
        if session.get("action_items") is None:
            session["summary"] = summary
            session["action_items"] = merge_action_items(items, logged_items(session))

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


def relay_system() -> str:
    return (
        f"{persona_text()}\n\n{SPOKEN_RULES}\n\n"
        "Live operational briefing for this call:\n"
        "(No live briefing was attached to this call. Do not guess operational facts.)"
    )


def spoken_from_message(message: dict, *, empty_fallback: str) -> tuple[str, bool]:
    raw = str((message or {}).get("content") or "")
    clean, end_call = split_end_call(raw)
    clean = normalize_spoken(clean)
    if not clean:
        clean = EMPTY_GOODBYE if end_call else empty_fallback
    return clean, end_call


async def record_assistant(session_id: str, clean: str, end_call: bool) -> None:
    def apply(session: dict) -> None:
        session["turns"].append({"role": "assistant", "content": clean, "ts": now_iso()})
        if end_call:
            session["ended_by_assistant"] = True
            session["status"] = "completed"
            session["end_reason"] = "assistant"
            if not session.get("ended_at"):
                session["ended_at"] = now_iso()

    await update_session(session_id, apply)


def speech_twiml(request: web.Request, session_id: str, clean: str, end_call: bool) -> str:
    if end_call:
        return render_say_hangup(clean)
    return render_gather(
        clean,
        signed_url(request, "/voice/turn", session_id),
        signed_url(request, "/voice/reprompt", session_id, n=1),
    )


def hold_twiml(request: web.Request, session_id: str, lookup_id: str, say_text: str) -> str:
    return render_gather(
        say_text,
        signed_url(request, "/voice/turn", session_id),
        signed_url(request, "/voice/resume", session_id, lookup=lookup_id),
        timeout=1,
    )


def twiml_live() -> str:
    base = configured_public_base()
    if not base:
        response = VoiceResponse()
        response.say(FALLBACK_SAY, voice=tts_voice())
        return str(response)
    response = VoiceResponse()
    connect = response.connect(action=f"{base}/voice/fallback", method="POST")
    wss = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    connect.conversation_relay(
        url=wss,
        welcome_greeting=DEFAULT_GREETING,
        language="en-US",
    )
    return str(response)


def twiml_fallback() -> str:
    response = VoiceResponse()
    response.say(FALLBACK_SAY, voice=tts_voice())
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


def get_bridge_lock() -> asyncio.Lock:
    global _bridge_lock
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()
    return _bridge_lock


def _read_bridge() -> dict:
    path = bridge_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.exception("could not read bridge file")
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, dict] = {}
    for value in data.values():
        if isinstance(value, dict) and value.get("id") and value.get("question"):
            clean[str(value["id"])] = value
    return clean


def _ensure_bridge() -> None:
    global _bridge_loaded
    if _bridge_loaded:
        return
    loaded = _read_bridge()
    _questions.clear()
    _questions.update(loaded)
    _bridge_loaded = True


def _persist_bridge() -> None:
    ranked = sorted(
        _questions.values(),
        key=lambda question: question.get("created_at") or "",
        reverse=True,
    )[:QUESTION_KEEP]
    trimmed = {question["id"]: question for question in ranked}
    if len(trimmed) != len(_questions):
        _questions.clear()
        _questions.update(trimmed)
    path = bridge_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".sal-bridge-", suffix=".tmp", dir=str(path.parent))
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


def public_question(question: dict) -> dict:
    return {
        "id": question.get("id"),
        "question": question.get("question"),
        "session_id": question.get("session_id"),
        "status": question.get("status"),
        "answer": question.get("answer"),
        "created_at": question.get("created_at"),
        "answered_at": question.get("answered_at"),
    }


async def enqueue_question(question: str, session_id: str | None) -> dict:
    async with get_bridge_lock():
        _ensure_bridge()
        question_id = str(uuid.uuid4())
        record = {
            "id": question_id,
            "question": question,
            "session_id": session_id or None,
            "status": "pending",
            "answer": None,
            "created_at": now_iso(),
            "answered_at": None,
        }
        _questions[question_id] = record
        _persist_bridge()
        return copy.deepcopy(record)


async def snapshot_question(question_id: str) -> dict | None:
    async with get_bridge_lock():
        _ensure_bridge()
        question = _questions.get(question_id)
        return copy.deepcopy(question) if question else None


async def list_questions(status: str) -> list[dict]:
    async with get_bridge_lock():
        _ensure_bridge()
        rows = [copy.deepcopy(row) for row in _questions.values()]
    if status != "all":
        rows = [row for row in rows if row.get("status") == status]
    rows.sort(key=lambda row: row.get("created_at") or "")
    return rows


async def answer_question(question_id: str, answer: str) -> tuple[dict, int]:
    async with get_bridge_lock():
        _ensure_bridge()
        question = _questions.get(question_id)
        if question is None:
            return {"error": "not_found"}, 404
        existing = (question.get("answer") or "").strip()
        if question.get("status") == "answered":
            if existing == answer:
                return public_question(question), 200
            return {
                "error": "already_answered",
                "id": question["id"],
                "status": "answered",
                "answer": question.get("answer"),
            }, 409
        question["status"] = "answered"
        question["answer"] = answer
        question["answered_at"] = now_iso()
        _persist_bridge()
        return public_question(question), 200


async def ask_team(question: str, *, session_id: str | None, still_current) -> str:
    cleaned = re.sub(r"\s+", " ", (question or "").strip())
    if not cleaned:
        return "No question was provided. Tell Seif you will follow up after the call. Do not invent an answer."
    if len(cleaned) > 1000:
        cleaned = cleaned[:1000]
    record = await enqueue_question(cleaned, session_id)
    deadline = time.monotonic() + ask_team_wait_seconds()
    while time.monotonic() < deadline:
        if not still_current():
            return "The caller moved on. Do not answer this question out loud."
        current = await snapshot_question(record["id"])
        if current and current.get("status") == "answered" and (current.get("answer") or "").strip():
            return f"Team answer: {str(current['answer']).strip()}"
        await asyncio.sleep(0.2)
    waited = int(ask_team_wait_seconds())
    return (
        f"No team answer arrived within {waited} seconds. "
        "Tell Seif you will follow up after the call. Do not invent an answer."
    )


async def execute_tool(name: str, arguments: dict, *, session_id: str | None, still_current) -> str:
    LOG.info("tool %s session=%s", name, session_id or "")
    if name == "ask_team":
        return await ask_team(
            str(arguments.get("question") or ""),
            session_id=session_id,
            still_current=still_current,
        )
    return await lookups.run_read_tool(name, arguments)


def _skipped_message() -> dict:
    return {"role": "assistant", "content": "", "skipped": True}


async def resolve_after_tools(
    messages: list[dict],
    first: dict,
    *,
    session_id: str | None,
    still_current,
) -> dict:
    """Run the tool calls already chosen, then ask the model for speech."""
    thread = list(messages)
    message = first
    for round_index in range(2):
        calls = lookups.tool_calls_of(message)
        if not calls:
            return message
        thread.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            }
        )
        for call in calls:
            if not still_current():
                return _skipped_message()
            name = str((call.get("function") or {}).get("name") or "")
            result = await execute_tool(
                name,
                lookups.tool_arguments(call),
                session_id=session_id,
                still_current=still_current,
            )
            if not still_current():
                return _skipped_message()
            thread.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": result,
                }
            )
        message = await llm_complete(
            thread,
            tools=lookups.READ_TOOLS if round_index == 0 else None,
            timeout=12,
            max_tokens=180,
        )
    return message


def publish_lookup(state: dict, text: str, end_call: bool) -> None:
    if state.get("status") != "running":
        return
    state["text"] = text
    state["end_call"] = end_call
    state["status"] = "ready"


def cancel_lookup(session_id: str) -> None:
    state = _lookups.get(session_id)
    if state and state.get("status") == "running":
        state["status"] = "cancelled"
    task = _lookup_tasks.get(session_id)
    if task and not task.done():
        task.cancel()


def start_lookup(session_id: str, messages: list[dict], first: dict) -> str:
    lookup_id = str(uuid.uuid4())
    state = {
        "id": lookup_id,
        "status": "running",
        "started": time.monotonic(),
        "text": None,
        "end_call": False,
        "delivered": False,
        "held": True,
    }
    _lookups[session_id] = state

    def still_current() -> bool:
        current = _lookups.get(session_id)
        return current is state and state.get("status") == "running"

    async def runner() -> None:
        try:
            final = await resolve_after_tools(
                messages,
                first,
                session_id=session_id,
                still_current=still_current,
            )
            if final.get("skipped") or not still_current():
                return
            text, end_call = spoken_from_message(final, empty_fallback=LOOKUP_FOLLOWUP)
            publish_lookup(state, text, end_call)
        except asyncio.CancelledError:
            if state.get("status") == "running":
                state["status"] = "cancelled"
            raise
        except Exception:
            LOG.exception("lookup failed session=%s", session_id)
            publish_lookup(state, LOOKUP_FOLLOWUP, False)

    _lookup_tasks[session_id] = asyncio.create_task(runner())
    return lookup_id


async def deliver_lookup(request: web.Request, session_id: str, state: dict) -> str:
    text = state.get("text") or LOOKUP_FOLLOWUP
    end_call = bool(state.get("end_call"))
    if not state.get("delivered"):
        state["delivered"] = True
        state["status"] = "spoken"
        await record_assistant(session_id, text, end_call)
    return speech_twiml(request, session_id, text, end_call)


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
            "stripe_read_configured": lookups.stripe_restricted_key().startswith("rk_"),
            "admin_read_configured": bool(lookups.admin_read_token()),
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
    cancel_lookup(session_id)
    if lookups.is_stop_talking(speech):
        if lookups.is_write_request(speech):
            await log_action_item(session_id, speech)
        await record_assistant(session_id, STOP_REPLY, False)
        LOG.info("turn session=%s stop", session_id)
        return xml_response(speech_twiml(request, session_id, STOP_REPLY, False))
    if lookups.is_write_request(speech):
        await log_action_item(session_id, speech)
        await record_assistant(session_id, WRITE_REPLY, False)
        LOG.info("turn session=%s write-approval", session_id)
        return xml_response(speech_twiml(request, session_id, WRITE_REPLY, False))
    messages = conversation_messages(snapshot)
    try:
        first = await llm_complete(messages, tools=lookups.READ_TOOLS, timeout=12, max_tokens=180)
    except Exception:
        LOG.exception("spoken reply failed session=%s", session_id)
        first = {
            "role": "assistant",
            "content": "Sorry Seif, I hit a snag. Say that once more and I'll stay with you.",
        }
    if lookups.tool_calls_of(first):
        lookup_id = start_lookup(session_id, messages, first)
        LOG.info("turn session=%s lookup=%s", session_id, lookup_id)
        return xml_response(hold_twiml(request, session_id, lookup_id, LOOKUP_FILLER))
    clean, end_call = spoken_from_message(
        first,
        empty_fallback="Sorry Seif, I missed that. What would you like me to jump on?",
    )
    await record_assistant(session_id, clean, end_call)
    LOG.info("turn session=%s chars=%d end=%s", session_id, len(speech), end_call)
    return xml_response(speech_twiml(request, session_id, clean, end_call))


async def handle_resume(request: web.Request) -> web.Response:
    denied = authorize_voice(request)
    if denied:
        return denied
    session_id = request.query.get("sid", "")
    if await snapshot_session(session_id) is None:
        return web.Response(status=404, text="not_found")
    lookup_id = request.query.get("lookup", "")
    state = _lookups.get(session_id)
    if state is None or state.get("id") != lookup_id:
        return xml_response(speech_twiml(request, session_id, "Go ahead, Seif.", False))
    if state.get("status") == "running" and time.monotonic() - float(state.get("started") or 0) >= lookup_budget_seconds():
        publish_lookup(state, LOOKUP_FOLLOWUP, False)
    if state.get("status") in {"ready", "spoken"} and (state.get("text") or state.get("delivered")):
        xml = await deliver_lookup(request, session_id, state)
        return xml_response(xml)
    if state.get("status") == "cancelled":
        return xml_response(speech_twiml(request, session_id, "Go ahead, Seif.", False))
    return xml_response(hold_twiml(request, session_id, lookup_id, LOOKUP_STILL))


async def handle_post_question(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    try:
        data = await read_json_object(request)
        question = optional_str(data, "question")
        session_id = optional_str(data, "session_id")
    except web.HTTPBadRequest as exc:
        return web.json_response({"error": "invalid_json", "detail": exc.text}, status=400)
    except ValueError as exc:
        return web.json_response({"error": "invalid_body", "detail": str(exc)}, status=400)
    if question is None or not question.strip():
        return web.json_response({"error": "invalid_body", "detail": "question is required"}, status=400)
    record = await enqueue_question(question.strip(), session_id.strip() if session_id else None)
    LOG.info("bridge question id=%s", record["id"])
    return web.json_response(public_question(record))


async def handle_list_questions(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    status = (request.query.get("status") or "pending").strip().lower()
    if status not in {"pending", "answered", "all"}:
        return web.json_response(
            {"error": "invalid_query", "detail": "status must be pending, answered, or all"},
            status=400,
        )
    questions = await list_questions(status)
    return web.json_response({"questions": [public_question(question) for question in questions]})


async def handle_post_answer(request: web.Request) -> web.Response:
    denied = authorize_api(request)
    if denied:
        return denied
    try:
        data = await read_json_object(request)
        question_id = optional_str(data, "id")
        answer = optional_str(data, "answer")
    except web.HTTPBadRequest as exc:
        return web.json_response({"error": "invalid_json", "detail": exc.text}, status=400)
    except ValueError as exc:
        return web.json_response({"error": "invalid_body", "detail": str(exc)}, status=400)
    if question_id is None or not question_id.strip():
        return web.json_response({"error": "invalid_body", "detail": "id is required"}, status=400)
    if answer is None or not answer.strip():
        return web.json_response({"error": "invalid_body", "detail": "answer is required"}, status=400)
    body, status = await answer_question(question_id.strip(), answer.strip())
    LOG.info("bridge answer id=%s status=%s", question_id.strip(), status)
    return web.json_response(body, status=status)


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
    generation = 0
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
        if etype == "interrupt":
            generation += 1
            continue
        if etype == "error":
            LOG.error("CR error %s", event)
            continue
        if etype != "prompt":
            continue
        user_text = (event.get("voicePrompt") or event.get("transcript") or "").strip()
        if not user_text:
            continue
        generation += 1
        turn_generation = generation
        history.append({"role": "user", "content": user_text})
        messages = [{"role": "system", "content": relay_system()}] + history[-16:]

        def still_current() -> bool:
            return turn_generation == generation

        try:
            if lookups.is_stop_talking(user_text):
                reply = STOP_REPLY
            elif lookups.is_write_request(user_text):
                reply = WRITE_REPLY
            else:
                first = await llm_complete(messages, tools=lookups.READ_TOOLS, timeout=12, max_tokens=180)
                if lookups.tool_calls_of(first):
                    if still_current():
                        await ws.send_json({"type": "text", "token": LOOKUP_FILLER + " ", "last": False})
                    final = await resolve_after_tools(
                        messages,
                        first,
                        session_id=None,
                        still_current=still_current,
                    )
                    if final.get("skipped") or not still_current():
                        continue
                    reply, _end = spoken_from_message(final, empty_fallback=LOOKUP_FOLLOWUP)
                else:
                    reply, _end = spoken_from_message(
                        first,
                        empty_fallback="Sorry Seif, I missed that. What would you like me to jump on?",
                    )
        except Exception as exc:
            LOG.exception("LLM failed")
            reply = f"I could not reach the language model. {exc}"
        if not still_current():
            continue
        history.append({"role": "assistant", "content": reply})
        await ws.send_json({"type": "text", "token": reply, "last": True})
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
    app.router.add_post("/bridge/questions", handle_post_question)
    app.router.add_get("/bridge/questions", handle_list_questions)
    app.router.add_post("/bridge/answers", handle_post_answer)
    app.router.add_get("/voice/gather", handle_gather)
    app.router.add_post("/voice/gather", handle_gather)
    app.router.add_post("/voice/turn", handle_turn)
    app.router.add_post("/voice/resume", handle_resume)
    app.router.add_get("/voice/resume", handle_resume)
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
