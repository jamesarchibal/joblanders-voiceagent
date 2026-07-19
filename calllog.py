"""Write one call record to Supabase (landeros_calls) at session shutdown.

Mirrors config.py: standard library only, best-effort, never raises. The
dashboard (LanderOS → Llamadas) reads these rows to show transcript, duration,
detected branch and cost inputs, so failures here must never break a live call.
"""

from __future__ import annotations

import json
import os
import urllib.request

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

# Approximate provider rates, USD — review/adjust if pricing plans change.
# ElevenLabs Turbo v2.5 (Creator plan-ish), Deepgram Nova-2 streaming,
# Anthropic Claude Haiku 4.5 input/output.
RATE_TTS_PER_1K_CHARS_USD = 0.10
RATE_STT_PER_MIN_USD = 0.0043
RATE_LLM_INPUT_PER_MTOK_USD = 1.00
RATE_LLM_OUTPUT_PER_MTOK_USD = 5.00


def _estimate_cost_usd(
    tts_characters: int | None,
    stt_audio_seconds: float | None,
    llm_input_tokens: int | None,
    llm_output_tokens: int | None,
) -> float | None:
    if not any([tts_characters, stt_audio_seconds, llm_input_tokens, llm_output_tokens]):
        return None
    cost = 0.0
    cost += (tts_characters or 0) / 1000 * RATE_TTS_PER_1K_CHARS_USD
    cost += (stt_audio_seconds or 0) / 60 * RATE_STT_PER_MIN_USD
    cost += (llm_input_tokens or 0) / 1_000_000 * RATE_LLM_INPUT_PER_MTOK_USD
    cost += (llm_output_tokens or 0) / 1_000_000 * RATE_LLM_OUTPUT_PER_MTOK_USD
    return round(cost, 4)


def _post(payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/landeros_calls",
        data=data,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (trusted URL)
        return resp.status, ""


def save_call_log(
    *,
    room: str | None,
    caller: str | None,
    started_at: str | None,
    ended_at: str | None,
    duration_seconds: int | None,
    turns: int | None,
    branch: str | None,
    voice_id: str | None,
    transcript: list[dict],
    tts_characters: int | None = None,
    tts_audio_seconds: float | None = None,
    stt_audio_seconds: float | None = None,
    llm_input_tokens: int | None = None,
    llm_output_tokens: int | None = None,
) -> None:
    """Best-effort insert of a call record. Never raises."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("[CALLLOG] SUPABASE_URL/KEY not set; skipping call log")
        return

    base_payload = {
        "room": room,
        "caller": caller,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "turns": turns,
        "branch": branch,
        "voice_id": voice_id,
        "transcript": transcript,
    }
    cost_usd = _estimate_cost_usd(tts_characters, stt_audio_seconds, llm_input_tokens, llm_output_tokens)
    extended_payload = {
        **base_payload,
        "tts_characters": tts_characters,
        "tts_audio_seconds": tts_audio_seconds,
        "stt_audio_seconds": stt_audio_seconds,
        "llm_input_tokens": llm_input_tokens,
        "llm_output_tokens": llm_output_tokens,
        "cost_usd": cost_usd,
    }

    try:
        status, _ = _post(extended_payload)
        print(f"[CALLLOG] saved call ({status}) room={room} turns={turns} dur={duration_seconds}s cost=${cost_usd}")
        return
    except Exception as exc:  # noqa: BLE001 — usage columns may not exist yet; fall back below
        print(f"[CALLLOG] extended insert failed ({exc!r}), retrying with base fields")

    try:
        status, _ = _post(base_payload)
        print(f"[CALLLOG] saved call ({status}) room={room} turns={turns} dur={duration_seconds}s")
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        print(f"[CALLLOG] save failed ({exc!r})")
