# Sal live voice

Two-way spoken phone line for Seif. Twilio gathers speech with `<Gather input="speech">`, this service answers with `<Say voice="Polly.Matthew">`, and when the call ends Sal's caller script reads the verbatim transcript plus the instructions Seif spoke.

`Polly.Matthew` is the default voice because it works on every Twilio account. No ConversationRelay console onboarding is required for this path.

The older `/voice/live`, `/voice/fallback`, and `/ws` ConversationRelay routes stay in place.

## Call flow

1. The caller script `POST`s `/api/sessions` with the morning briefing (or an on-demand note) and a bearer token.
2. It dials Seif with `twiml_url` as the call URL and `status_url` as the status callback.
3. `/voice/gather` speaks the greeting inside `<Gather bargeIn="true">`. Seif can interrupt, or start talking within 12 seconds after Sal finishes.
4. Each `/voice/turn` appends Seif's transcript and asks the LLM with read-only tools. A normal reply is spoken inside `<Gather>`. A tool call is not spoken yet: Sal says "Give me a sec, checking." inside `<Gather bargeIn="true" timeout="1">`, and a `<Redirect>` polls `/voice/resume` until the lookup is ready. Seif can barge in during that hold; the lookup is dropped and the new utterance is answered instead. If the team has not answered within about 20 seconds, Sal says it will follow up after the call.
5. Silence is prompted once (`/voice/reprompt?n=1`) and then Sal says goodbye and hangs up (`n=2`).
6. `/voice/status` stores the Twilio call result and, on a finished call that has speech from Seif, extracts `summary` and `action_items`.
7. The caller script `GET`s `/api/sessions/{id}` (add `?summarize=1` if the status callback has not filled the instructions yet) and `POST`s `/api/sessions/{id}/ack` after it has taken the instructions.

## Auth

`SAL_BRIDGE_SECRET` gates the bridge.

- JSON routes under `/api/*` and `/bridge/*` require `Authorization: Bearer <SAL_BRIDGE_SECRET>`. Missing secret returns `503`. A missing or wrong token returns `401`. Comparison uses `hmac.compare_digest`.
- Twilio webhooks `/voice/gather`, `/voice/turn`, `/voice/resume`, `/voice/reprompt`, and `/voice/status` require a query parameter `sig`, the hex HMAC-SHA256 of the `session_id` keyed with `SAL_BRIDGE_SECRET`. A bad signature returns `403`. This holds even when `TWILIO_AUTH_TOKEN` is not set on Render.
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

Sal only states a figure that came from the call briefing or from a tool result, and says the date and the unit with it. If Seif says stop talking or quit talking, Sal says "Okay." and waits. Sal never sends, refunds, or takes a payment. If Seif asks for one of those, Sal says it will put that in chat for approval and logs an `action_item` immediately (`{"instruction": "<what Seif said>"}`), merged into the post-call `action_items` list.

`/health` reports `bridge_configured`, `gather_ready`, `stripe_read_configured`, and `admin_read_configured` along with the existing fields.

## Live lookups

The chat-completions call includes `tools` and `tool_choice: "auto"`. The tools are read-only:

| Tool | What it reads |
| --- | --- |
| `stripe_recent_payments` | Newest Stripe charges and invoices, using `STRIPE_RESTRICTED_KEY` (`rk_...` only). Amounts are converted from cents to dollars. Dates are labeled in America/New_York. |
| `admin_requests` | Newest website requests from `GET https://nordspecauto.com/api/admin` (override with `ADMIN_API_URL`). Sends `Authorization: Bearer <ADMIN_READ_TOKEN>` and `X-Admin-Read-Token`. The admin payload's `inquiries` list is the source. Deleted rows are skipped. |
| `ask_team` | Enqueues one question on this server and waits up to 20 seconds (`SAL_ASK_TEAM_WAIT_SECONDS`) for an external worker to answer it. |

There is no tool that sends, refunds, or creates a payment. A model request for any other tool name is refused.

While a lookup runs, the caller hears a short hold line inside `<Gather bargeIn="true">`, so speech still interrupts Sal. `/voice/resume` speaks the result when it is ready, or another short "Still checking." line, or the follow-up line once the wait is over.

## Team bridge

`ask_team` writes the same queue as `POST /bridge/questions`. An external worker polls `GET /bridge/questions` and answers with `POST /bridge/answers`. All three routes use `Authorization: Bearer <SAL_BRIDGE_SECRET>`.

Create a question:

```bash
curl -s -X POST "$SAL_LIVE_PUBLIC_BASE/bridge/questions" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"question":"Did the G30 bumper ship?","session_id":"optional-session-uuid"}'
```

Request JSON:

| Field | Required | Notes |
| --- | --- | --- |
| `question` | yes | Non-empty string. |
| `session_id` | no | Call session id, or omit / `null`. |

Response `200`:

```json
{
  "id": "uuid",
  "question": "Did the G30 bumper ship?",
  "session_id": "optional-session-uuid",
  "status": "pending",
  "answer": null,
  "created_at": "2026-11-07T05:09:00.000000Z",
  "answered_at": null
}
```

Poll pending questions (oldest first). `status` query is `pending` (default), `answered`, or `all`.

```bash
curl -s "$SAL_LIVE_PUBLIC_BASE/bridge/questions" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET"
```

Response `200`:

```json
{
  "questions": [
    {
      "id": "uuid",
      "question": "Did the G30 bumper ship?",
      "session_id": "optional-session-uuid",
      "status": "pending",
      "answer": null,
      "created_at": "2026-11-07T05:09:00.000000Z",
      "answered_at": null
    }
  ]
}
```

Answer one question:

```bash
curl -s -X POST "$SAL_LIVE_PUBLIC_BASE/bridge/answers" \
  -H "Authorization: Bearer $SAL_BRIDGE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"id":"uuid","answer":"It shipped yesterday."}'
```

Request JSON:

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | Question id from the poll. |
| `answer` | yes | Non-empty plain text. Sal reads this back. |

Response `200`:

```json
{
  "id": "uuid",
  "question": "Did the G30 bumper ship?",
  "session_id": "optional-session-uuid",
  "status": "answered",
  "answer": "It shipped yesterday.",
  "created_at": "2026-11-07T05:09:00.000000Z",
  "answered_at": "2026-11-07T05:09:08.000000Z"
}
```

Posting the same answer again returns `200` with that object. A different answer for an id that is already answered returns `409`:

```json
{"error": "already_answered", "id": "uuid", "status": "answered", "answer": "It shipped yesterday."}
```

Unknown id returns `404` `{"error": "not_found"}`. A missing question or answer returns `400` `{"error": "invalid_body", "detail": "..."}`. Missing secret returns `503` `{"error": "bridge_not_configured"}`. A bad bearer token returns `401` `{"error": "unauthorized"}`.

Questions are kept in memory and written to `/tmp/sal_live_bridge.json` (override with `SAL_BRIDGE_PATH`). The latest 100 are retained. Render's disk is ephemeral; answer a pending question on the instance that is serving the call.

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
# read-only Stripe restricted key (rk_live_... or rk_test_...). Charges and invoices read only.
STRIPE_RESTRICTED_KEY=rk_...
# bearer token the nordspecauto.com admin API accepts for GET /api/admin
ADMIN_READ_TOKEN=...
# optional override of the admin read URL
ADMIN_API_URL=https://nordspecauto.com/api/admin
# optional, seconds Sal waits for an ask_team answer (default 20)
SAL_ASK_TEAM_WAIT_SECONDS=20
# optional bridge queue file
SAL_BRIDGE_PATH=/tmp/sal_live_bridge.json
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
