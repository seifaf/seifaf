# Sal live voice (Twilio ConversationRelay)

Outbound live two-way voice for Seif via Twilio ConversationRelay + LLM text relay.
Short Polly `Say` remains the default / fallback.

## What runs today
- `server.py` — HTTP TwiML (`/voice/live`, `/voice/fallback`) + WebSocket (`/ws`) for ConversationRelay
- `../scripts/sal_call_seif.py` — `--live` uses ConversationRelay TwiML URL; on failure falls back to Polly Say
- Default (no `--live`) still one-way Polly Matthew Say

## Blockers for a real test (as of Nov 5 2026)
1. **Public WSS** — this Grok Bot computer cannot open a Cloudflare/SSH tunnel (outbound QUIC/TCP 7844 blocked). Deploy `server.py` to a public host (Fly.io / Render / Railway) with TLS, then set `SAL_LIVE_PUBLIC_BASE` / `SAL_LIVE_TWIML_URL`.
2. **ConversationRelay onboarding** — Console → Voice → ConversationRelay → complete provider onboarding (STT/TTS).
3. **LLM API key** — set `XAI_API_KEY` (preferred) or `OPENAI_API_KEY` in the host env (or `/workspace/team/twilio-live/.env`). This is text-in/text-out to the model; Twilio handles STT/TTS. It is **not** the Grok Bot in-app voice agent.

## Env
```
SAL_LIVE_PUBLIC_BASE=https://your-host.example
SAL_LIVE_TWIML_URL=https://your-host.example/voice/live
SAL_LIVE_PORT=8765
XAI_API_KEY=...
# or OPENAI_API_KEY=...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
```

## Local
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SAL_LIVE_PUBLIC_BASE=https://... SAL_LIVE_PORT=8765 .venv/bin/python server.py
# dial (after public URL works):
.venv/bin/python ../scripts/sal_call_seif.py --live "optional fallback text"
```

## Docs
- https://www.twilio.com/docs/voice/twiml/connect/conversationrelay
- https://www.twilio.com/docs/voice/conversationrelay/onboarding
