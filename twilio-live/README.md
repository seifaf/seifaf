# Sal live voice

Two-way spoken phone line for Seif. Twilio gathers speech with `<Gather input="speech">`, this service answers with `<Say voice="Polly.Matthew">`, and when the call ends Sal's caller script reads the verbatim transcript plus the instructions Seif spoke.

`Polly.Matthew` is the default voice because it works on every Twilio account. No ConversationRelay console onboarding is required for this path.

The older `/voice/live`, `/voice/fallback`, and `/ws` ConversationRelay routes stay in place.

## Call flow

1. The caller script `POST`s `/api/sessions` with the morning briefing (or an on-demand note) and a bearer token.
2. It dials Seif with `twiml_url` as the call URL and `status_url` as the status callback.
3. `/voice/gather` speaks the greeting inside `<Gather bargeIn="true">`. Seif can interrupt, or start talking within 12 seconds after Sal finishes.
4. Each `/voice/turn` appends Seif's transcript, asks the LLM, and either listens again or hangs up when the reply contains `[END_CALL]`.
5. Silence is prompted once (`/voice/reprompt?n=1`) and then Sal says goodbye and hangs up (`n=2`).
6. `/voice/status` stores the Twilio call result and, on a finished call that has speech from Seif, extracts `summary` and `action_items`.
7. The caller script `GET`s `/api/sessions/{id}` (add `?summarize=1` if the status callback has not filled the instructions yet) and `POST`s `/api/sessions/{id}/ack` after it has taken the instructions.

## Auth

`SAL_BRIDGE_SECRET` gates the bridge.

- JSON routes under `/api/*` require `Authorization: Bearer <SAL_BRIDGE_SECRET>`. Missing secret returns `503`. A missing or wrong token returns `401`. Comparison uses `hmac.compare_digest`.
- Twilio webhooks `/voice/gather`, `/voice/turn`, `/voice/reprompt`, and `/voice/status` require a query parameter `sig`, the hex HMAC-SHA256 of the `session_id` keyed with `SAL_BRIDGE_SECRET`. A bad signature returns `403`. This holds even when `TWILIO_AUTH_TOKEN` is not set on Render.
- `POST /voice/inbound` is the optional number webhook for calls from Seif (`+14133061640`). When `TWILIO_AUTH_TOKEN` is set, the `X-Twilio-Signature` must validate. When it is not set, `From` must be Seif's number. A signed call with no `From` is accepted. Any other `From` is rejected. A new inbound session is created and the gather greeting is returned.

## Create a session

```bash
curl -s -X POST "$SAL_LIVE_PUBLIC_BASE/api/sessions" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "greeting": "Morning Seif. Three orders are open and Stripe cleared overnight.",
    "context": "Open orders: 14. Stripe available: $1200. Team on today: Sam, Riley.",
    "caller_note": "morning_briefing"
  }'
```

`greeting` defaults to `Hey Seif, it's Sal, Chief Operations Manager at NordSpecAuto. What can I handle for you?`. `context` is injected into the LLM system prompt for that call only. `caller_note` is a tag such as `morning_briefing` or `on_demand`.

Response:

```json
{
  "session_id": "...",
  "sig": "...",
  "twiml_url": "https://sal-live.onrender.com/voice/gather?sid=...&sig=...",
  "status_url": "https://sal-live.onrender.com/voice/status?sid=...&sig=..."
}
```

If `PUBLIC_BASE` and `SAL_LIVE_PUBLIC_BASE` are both empty, those URLs use the request scheme and host (`X-Forwarded-Proto` / `X-Forwarded-Host` when present).

Dial `twiml_url` and set the call status callback to `status_url`. After the call:

```bash
curl -s "$SAL_LIVE_PUBLIC_BASE/api/sessions/$SID?summarize=1" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET"
curl -s -X POST "$SAL_LIVE_PUBLIC_BASE/api/sessions/$SID/ack" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET"
```

`GET /api/sessions?unacked=1` lists sessions that have not been acked and that contain something Seif said.

The session payload includes `turns` (verbatim, with confidence and UTC timestamps), `user_utterances`, `summary`, and `action_items` (`{"instruction": "..."}` per decision Seif spoke). If extraction fails or times out, the action items fall back to those utterances.

Sessions are kept in memory and written atomically to `/tmp/sal_live_sessions.json` (override with `SAL_SESSIONS_PATH`). The latest 100 sessions are retained. Render's disk is ephemeral, so the caller script should read the session when the call ends.

## Spoken behavior

Replies are one to three short sentences. Sal repeats an instruction back before the call ends, starts internal work immediately, and says that a customer message or a money move will show up as an approval card in chat. A sign-off from Seif ("that's all", "thanks Sal", and the same kind of close) makes the model append `[END_CALL]`. That token is removed before it is spoken, the reply is played, and the call hangs up.

`/health` reports `bridge_configured` and `gather_ready` along with the existing fields.

## Env

```
SAL_BRIDGE_SECRET=...
SAL_LIVE_PUBLIC_BASE=https://sal-live.onrender.com
# PUBLIC_BASE is accepted as an alias
SAL_LIVE_PORT=8765
SAL_TTS_VOICE=Polly.Matthew
XAI_API_KEY=...
# or OPENAI_API_KEY=... / OPENAI_API_KEY_P00...
# optional, for /voice/inbound signature checks
TWILIO_AUTH_TOKEN=...
TWILIO_ACCOUNT_SID=...
```

Render start command: `SAL_LIVE_PORT=$PORT python server.py` with root directory `twilio-live`. The process binds `0.0.0.0`.

## Local

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SAL_BRIDGE_SECRET=dev SAL_LIVE_PUBLIC_BASE=https://sal-live.onrender.com \
  SAL_LIVE_PORT=8765 .venv/bin/python server.py
```

Tests (no network; the LLM is mocked):

```bash
python3 -m unittest discover -s tests -v
```

## Legacy ConversationRelay

`/voice/live` still returns `<Connect><ConversationRelay/></Connect>` when a public base URL is set, and `/ws` still relays text to the LLM. That path needs Twilio ConversationRelay onboarding, which this account does not have. New calls should use the gather session URLs above.

## Docs

- https://www.twilio.com/docs/voice/twiml/gather
- https://www.twilio.com/docs/voice/twiml/say
- https://www.twilio.com/docs/voice/twiml/connect/conversationrelay
