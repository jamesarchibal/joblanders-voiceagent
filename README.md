# Joblanders Voice Agent

Code-first, provider-swappable inbound voice agent. Cascaded pipeline
(**STT → LLM → TTS**) orchestrated by LiveKit Agents. No dependency on the
CTO's Google ADK tooling — plug into whatever you want later via tools/MCP.

**What it does (MVP):** a call comes in → agent greets + discloses it's an AI
(EU AI Act art. 50) → figures out **support vs sales** → gives basic support,
pitches the product, or **transfers to a human** on request / when needed.

## Stack

| Layer | Choice | Swap to |
|---|---|---|
| Orchestrator | LiveKit Agents | Pipecat |
| Ears (STT) | Deepgram | any plugin |
| Brain (LLM) | Claude Haiku (→ +Sonnet router later) | Gemini Flash, etc. |
| Voice (TTS) | Cartesia (clone brand voice) | Hume, ElevenLabs |
| Phone (Milestone 2) | Twilio SIP | Telnyx |

## Setup

```bash
cd "path/to/JL/agentes/voice-agent"
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in keys
```

Keys needed: **Anthropic** (have it), **Deepgram**, **Cartesia**, and
**LiveKit** (only for `dev`, not for `console`). All three have free tiers.

## Run

```bash
# Local mic + speakers, no phone, no LiveKit keys. Fastest way to test the loop.
python agent.py console

# Connect as a LiveKit worker (needs LIVEKIT_URL/API_KEY/API_SECRET).
python agent.py dev
```

## Files

- `agent.py` — the loop: session wiring + the 3 tools (`transfer_to_human`,
  `get_product_info`, `lookup_application_status`).
- `prompts/system_es.md` — persona, mandatory AI disclosure, GREET→CLASSIFY→
  3-branch logic, anti-hallucination + humanity rules. Edit this to change behavior.
- `knowledge.py` — the only place the agent gets facts. Swap the function bodies
  for real JoblandersOS calls later; the agent never invents prices/statuses.

## Roadmap

- **M0 (this):** local loop — greet, classify, two branches, transfer stub.
- **M1 (this):** 3-branch brain + tools + compliance disclosure. ✅
- **M2:** Twilio SIP → real phone number; warm transfer via SIP REFER + whisper summary; recording notice + opt-out.
- **M3:** humanity polish (backchannels, latency fillers, ES number normalization, barge-in tuning) + per-turn Haiku↔Sonnet router + legal sign-off.

## Compliance notes (baked in)

- **AI disclosure** at call start, in ES — mandatory EU AI Act art. 50 (Aug 2026).
- **Human escape** always available (`transfer_to_human`), honored instantly.
- **No hallucinated facts** — prices/statuses only from `knowledge.py`.
- M2 adds recording notice + opt-out (GDPR/ePrivacy). Inbound only = no Lista
  Robinson / Registro Opposizioni scrubbing needed (that's outbound-only).
