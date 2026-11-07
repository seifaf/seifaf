"""Read-only lookups Sal can speak during a live call.

Stripe amounts arrive in the smallest currency unit. US dollar amounts are
converted from cents. Dates are labeled in America/New_York.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

EASTERN = ZoneInfo("America/New_York")
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2026-09-30.endive"
STRIPE_READ_PATHS = {"/v1/charges", "/v1/invoices"}
DEFAULT_ADMIN_API = "https://nordspecauto.com/api/admin"

# Currencies Stripe bills with a zero exponent. Everything else we handle is
# two-decimal, which is the cents-to-dollars case this line needs.
ZERO_DECIMAL = {
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
}

STATUS_LABELS = {
    "new": "New request",
    "reviewing_vin": "Reviewing VIN",
    "waiting_customer": "Waiting on customer",
    "quote_ready": "Quote ready",
    "invoice_sent": "Invoice sent",
    "deposit_paid": "Partial payment received",
    "sourcing": "Sourcing parts",
    "assembly": "Assembly in progress",
    "ready_to_ship": "Ready to ship",
    "balance_due": "Balance due",
    "paid": "Paid in full",
    "shipped": "Shipped",
    "delivered": "Delivered",
    "completed": "Completed",
    "canceled": "Canceled",
    "cancelled": "Canceled",
}

READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "stripe_recent_payments",
            "description": (
                "Read-only. List the newest Stripe charges and invoices. "
                "Amounts are converted from cents to dollars. Dates are labeled "
                "in America/New_York. Use this when Seif asks what was paid, "
                "charged, or invoiced. Never refunds, never sends, and never "
                "creates a payment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent charges and invoices to include, from 1 to 10. Default 5.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "admin_requests",
            "description": (
                "Read-only. Read the newest website requests and orders from the "
                "nordspecauto.com admin workspace. Use this when Seif asks about "
                "a website request, an order, or a VIN on a request. Does not "
                "change, send, or delete anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many newest requests to return, from 1 to 10. Default 5.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_team",
            "description": (
                "Ask Seif's bot team one short question and wait up to about 20 "
                "seconds for their answer. Use this only for a fact Stripe and "
                "the admin requests do not contain. Do not use this to send a "
                "message, refund, or take a payment. A hold line already plays "
                "while the wait runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The single question for the team, in plain text.",
                    }
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
]

_STOP_PHRASE = re.compile(r"\b(stop talking|quit talking|stop speaking)\b", re.IGNORECASE)
_STOP_ONLY = re.compile(
    r"^(?:(?:hey |ok |okay |sal |please )*)(?:stop|hush|shut up|be quiet|quiet)[.! ]*$",
    re.IGNORECASE,
)
_WRITE_REQUEST = re.compile(
    r"(?:"
    r"(?:^|\b(?:please|go ahead and|can you|could you|i need you to|let's|lets)\s+)refund\b"
    r"|^\s*refund\b"
    r"|\b(?:send|text|email|message)\s+(?:the\s+)?(?:customer|him|her|them|buyer)\b"
    r"|\b(?:send|text|email)\s+(?:a|an|that|this|him|her|them)\b"
    r"|\b(?:charge|pay)\s+(?:the\s+)?(?:customer|him|her|them|card|vendor)\b"
    r"|\b(?:issue|process|make)\s+(?:a\s+)?(?:refund|payment|payout)\b"
    r"|\bmove\s+(?:the\s+)?money\b"
    r"|\btake\s+(?:the\s+)?payment\b"
    r")",
    re.IGNORECASE,
)


class LookupError(RuntimeError):
    pass


class LookupNotConfigured(LookupError):
    pass


def clamp_limit(value: object, default: int = 5) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 10))


def is_stop_talking(text: str) -> bool:
    spoken = (text or "").strip()
    if not spoken:
        return False
    return _STOP_PHRASE.search(spoken) is not None or _STOP_ONLY.match(spoken) is not None


def is_write_request(text: str) -> bool:
    return _WRITE_REQUEST.search(text or "") is not None


def chat_payload(
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    max_tokens: int,
    temperature: float = 0.3,
) -> dict:
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def cents_to_dollars(cents: int) -> str:
    """Format a signed integer number of cents as a dollar amount."""
    minor = int(cents)
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}${minor // 100}.{minor % 100:02d}"


def format_money(amount: object, currency: str = "usd") -> str:
    code = (currency or "usd").strip().lower() or "usd"
    try:
        minor = int(amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown amount"
    if code in ZERO_DECIMAL:
        sign = "-" if minor < 0 else ""
        return f"{sign}{abs(minor)} {code.upper()}"
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    major = f"{minor // 100}.{minor % 100:02d}"
    if code == "usd":
        return f"{sign}${major} USD"
    return f"{sign}{major} {code.upper()}"


def format_eastern(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(EASTERN)
    hour = str(int(local.strftime("%I")))
    return f"{local.strftime('%B')} {local.day}, {local.year}, {hour}:{local.strftime('%M %p')} ET"


def format_unix_eastern(timestamp: object) -> str:
    try:
        moment = datetime.fromtimestamp(int(timestamp), timezone.utc)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return "unknown date"
    return format_eastern(moment)


def parse_admin_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
        if not text.endswith("Z") and "+" not in text[10:] and "-" not in text[10:]:
            text += "Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def dollars_from_major(value: object) -> str | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    quantized = abs(quantized)
    return f"{sign}${quantized:.2f} USD"


def _rows(payload: object, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    return []


def _charge_line(charge: dict) -> str:
    billing = charge.get("billing_details") if isinstance(charge.get("billing_details"), dict) else {}
    name = (billing.get("name") or "").strip()
    email = (charge.get("receipt_email") or "").strip()
    who = ", ".join(part for part in (name, email) if part)
    description = (charge.get("description") or "").strip()
    parts = [
        str(charge.get("id") or "charge"),
        format_money(charge.get("amount"), str(charge.get("currency") or "usd")),
        f"status {charge.get('status') or 'unknown'}",
        f"created {format_unix_eastern(charge.get('created'))}",
    ]
    refunded = charge.get("amount_refunded")
    try:
        if int(refunded or 0) > 0:
            parts.append(
                "amount refunded "
                + format_money(refunded, str(charge.get("currency") or "usd"))
            )
    except (TypeError, ValueError):
        pass
    if description:
        parts.append(description)
    if who:
        parts.append(who)
    return "- " + "; ".join(parts)


def _custom_fields(invoice: dict) -> str:
    fields = invoice.get("custom_fields")
    if not isinstance(fields, list):
        return ""
    bits: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        value = str(field.get("value") or "").strip()
        if name and value and len(value) <= 80:
            bits.append(f"{name} {value}")
    return "; ".join(bits)


def _invoice_line(invoice: dict) -> str:
    currency = str(invoice.get("currency") or "usd")
    number = (invoice.get("number") or "").strip()
    label = number or str(invoice.get("id") or "invoice")
    parts = [
        label,
        f"id {invoice.get('id')}" if number else "",
        f"amount due {format_money(invoice.get('amount_due'), currency)}",
        f"amount paid {format_money(invoice.get('amount_paid'), currency)}",
        f"amount remaining {format_money(invoice.get('amount_remaining'), currency)}",
        f"status {invoice.get('status') or 'unknown'}",
        f"created {format_unix_eastern(invoice.get('created'))}",
    ]
    if invoice.get("due_date"):
        parts.append(f"due {format_unix_eastern(invoice.get('due_date'))}")
    who = ", ".join(
        part
        for part in (
            str(invoice.get("customer_name") or "").strip(),
            str(invoice.get("customer_email") or "").strip(),
        )
        if part
    )
    if who:
        parts.append(who)
    extra = _custom_fields(invoice)
    if extra:
        parts.append(extra)
    return "- " + "; ".join(part for part in parts if part)


def format_stripe_lists(charges_payload: object, invoices_payload: object, *, limit: int = 5) -> str:
    limit = clamp_limit(limit)
    charges = _rows(charges_payload, "data")[:limit]
    invoices = _rows(invoices_payload, "data")[:limit]
    lines = [
        "Stripe read-only result. Amounts are converted from cents and shown in dollars. Dates are America/New_York.",
        "Charges:",
    ]
    if charges:
        lines.extend(_charge_line(row) for row in charges)
    else:
        lines.append("- none")
    lines.append("Invoices:")
    if invoices:
        lines.extend(_invoice_line(row) for row in invoices)
    else:
        lines.append("- none")
    if not charges and not invoices:
        lines.append("Stripe returned no recent charges or invoices. Do not invent any.")
    return "\n".join(lines)


def _inquiry_sort_key(row: dict) -> tuple:
    moment = parse_admin_time(row.get("createdAt") or row.get("created_at"))
    stamp = moment.timestamp() if moment else 0
    try:
        number = int(row.get("id") or 0)
    except (TypeError, ValueError):
        number = 0
    return (stamp, number)


def _inquiry_line(row: dict) -> str:
    status = str(row.get("status") or "").strip()
    label = STATUS_LABELS.get(status, status or "unknown status")
    moment = parse_admin_time(row.get("createdAt") or row.get("created_at"))
    when = format_eastern(moment) if moment else "unknown date"
    price = dollars_from_major(row.get("sellingPrice"))
    if price is None and row.get("priceCents") not in (None, ""):
        try:
            price = format_money(row.get("priceCents"), "usd")
        except (TypeError, ValueError):
            price = None
    part = re.sub(r"\s+", " ", str(row.get("requestedPart") or row.get("part") or "")).strip()
    if len(part) > 80:
        part = part[:77].rstrip() + "..."
    parts = [
        f"Request #{row.get('id')}",
        str(row.get("name") or "unknown customer").strip(),
        part or "part not listed",
        f"status {label}",
        f"received {when}",
    ]
    if price:
        parts.append(f"customer price {price}")
    vin = str(row.get("vin") or "").strip()
    if vin:
        parts.append(f"VIN {vin}")
    phone = str(row.get("phone") or "").strip()
    email = str(row.get("email") or "").strip()
    if phone:
        parts.append(f"phone {phone}")
    if email:
        parts.append(f"email {email}")
    return "- " + "; ".join(parts)


def format_admin_inquiries(payload: object, *, limit: int = 5) -> str:
    limit = clamp_limit(limit)
    if isinstance(payload, dict) and payload.get("error"):
        return (
            "Admin read was refused. Do not invent website requests. "
            f"The site said: {payload.get('error')}"
        )
    rows = _rows(payload, "inquiries")
    if not rows and isinstance(payload, dict):
        for key in ("requests", "orders"):
            rows = _rows(payload, key)
            if rows:
                break
    visible = [row for row in rows if not row.get("deletedAt") and not row.get("deleted_at")]
    visible.sort(key=_inquiry_sort_key, reverse=True)
    chosen = visible[:limit]
    lines = [
        "nordspecauto.com admin read-only result. Newest website requests. Dates are America/New_York.",
    ]
    if not chosen:
        lines.append("No website requests were returned. Do not invent any.")
        return "\n".join(lines)
    lines.extend(_inquiry_line(row) for row in chosen)
    return "\n".join(lines)


def stripe_restricted_key() -> str:
    return (os.environ.get("STRIPE_RESTRICTED_KEY") or "").strip()


def admin_read_token() -> str:
    return (os.environ.get("ADMIN_READ_TOKEN") or "").strip()


def admin_api_url() -> str:
    return (os.environ.get("ADMIN_API_URL") or DEFAULT_ADMIN_API).strip() or DEFAULT_ADMIN_API


async def stripe_get(path: str, params: dict) -> dict:
    key = stripe_restricted_key()
    if not key.startswith("rk_"):
        raise LookupNotConfigured("stripe")
    if path not in STRIPE_READ_PATHS:
        raise LookupError("refused non-read stripe path")
    timeout = ClientTimeout(total=8)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(
            STRIPE_API + path,
            params={key: str(value) for key, value in params.items()},
            headers={
                "Authorization": f"Bearer {key}",
                "Stripe-Version": STRIPE_VERSION,
            },
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception as exc:
                raise LookupError(f"Stripe HTTP {resp.status}") from exc
            if resp.status >= 400 or not isinstance(body, dict):
                raise LookupError(f"Stripe HTTP {resp.status}")
            return body


async def admin_get() -> dict:
    token = admin_read_token()
    if not token:
        raise LookupNotConfigured("admin")
    timeout = ClientTimeout(total=8)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(
            admin_api_url(),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Admin-Read-Token": token,
                "Accept": "application/json",
            },
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception as exc:
                raise LookupError(f"Admin HTTP {resp.status}") from exc
            if not isinstance(body, dict):
                raise LookupError(f"Admin HTTP {resp.status}")
            if resp.status >= 400:
                message = body.get("error") or f"HTTP {resp.status}"
                return {"error": str(message)}
            return body


async def stripe_recent_payments(limit: object = 5) -> str:
    count = clamp_limit(limit)
    if not stripe_restricted_key().startswith("rk_"):
        return (
            "Stripe read key is not configured. Tell Seif you will follow up after the call. "
            "Do not invent amounts."
        )
    try:
        charges = await stripe_get("/v1/charges", {"limit": count})
        invoices = await stripe_get("/v1/invoices", {"limit": count})
    except LookupNotConfigured:
        return (
            "Stripe read key is not configured. Tell Seif you will follow up after the call. "
            "Do not invent amounts."
        )
    except LookupError as exc:
        return f"Stripe lookup failed ({exc}). Do not invent amounts. Say you will follow up after the call."
    return format_stripe_lists(charges, invoices, limit=count)


async def admin_requests(limit: object = 5) -> str:
    count = clamp_limit(limit)
    if not admin_read_token():
        return (
            "Admin read token is not configured. Tell Seif you will follow up after the call. "
            "Do not invent website requests."
        )
    try:
        payload = await admin_get()
    except LookupNotConfigured:
        return (
            "Admin read token is not configured. Tell Seif you will follow up after the call. "
            "Do not invent website requests."
        )
    except LookupError as exc:
        return (
            f"Admin lookup failed ({exc}). Do not invent website requests. "
            "Say you will follow up after the call."
        )
    return format_admin_inquiries(payload, limit=count)


def tool_calls_of(message: dict) -> list[dict]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    usable: list[dict] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if call.get("id") and function.get("name"):
            usable.append(call)
    return usable


def tool_arguments(call: dict) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def run_read_tool(name: str, arguments: dict) -> str:
    if name == "stripe_recent_payments":
        return await stripe_recent_payments(arguments.get("limit"))
    if name == "admin_requests":
        return await admin_requests(arguments.get("limit"))
    return (
        "That action is not available. Sal can only read Stripe payments, read admin "
        "requests, or ask the team. If Seif asked to send, refund, or pay, say you will "
        "put it in chat for approval and do not claim it was done."
    )
