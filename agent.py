"""Joblanders inbound voice agent — Milestone 0/1.

Cascaded pipeline: Deepgram (STT) -> Claude (brain) -> Cartesia (TTS),
orchestrated by LiveKit Agents with semantic turn detection + barge-in.

One brain (Haiku by default) handles GREET -> CLASSIFY -> {support, sales, human}.
The per-turn router (Haiku <-> Sonnet) comes in a later milestone; keeping it to
one model first so the loop is simple and easy to make feel human.

Run:
    python agent.py console      # local mic/speakers, no phone, no LiveKit keys
    python agent.py dev          # connect to LiveKit as a worker (needs LIVEKIT_*)
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.beta.workflows import WarmTransferTask
from livekit.plugins import anthropic, deepgram, elevenlabs, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from calllog import save_call_log
from config import load_config

load_dotenv(override=True)

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_es.md"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
TTS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
# Cloned "James" voice. Hardcoded fallback so the agent NEVER silently falls back to a
# default ElevenLabs voice if the secret is missing/empty.
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or "wEQE8Agx2GxUkKIOCNny"
# The plugin looks for ELEVEN_API_KEY; we store it as ELEVENLABS_API_KEY, so pass it explicitly.
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY")
# Multilingual: leave the TTS language UNSET so ElevenLabs (turbo_v2_5) auto-detects the
# language from the text Claude produces — Lander mirrors whatever language the caller uses
# (ES / CA / EN / …). Set ELEVENLABS_LANGUAGE to a code (e.g. "es") only to force one language.
TTS_LANGUAGE = os.getenv("ELEVENLABS_LANGUAGE") or None
# Per-language cloned voices of James. Lander swaps to the matching clone when Deepgram
# detects the caller speaking Catalan or English; any other language uses the ES clone.
ELEVEN_VOICE_ID_CA = os.getenv("ELEVENLABS_VOICE_ID_CA") or "x5Jilxt6jZdQy7DBFXLh"
ELEVEN_VOICE_ID_EN = os.getenv("ELEVENLABS_VOICE_ID_EN") or "sXhK0yDKboLJMlkKubg3"
VOICE_BY_LANG = {"ca": ELEVEN_VOICE_ID_CA, "en": ELEVEN_VOICE_ID_EN}
# Warm transfer: dial a human via the netelip SIP outbound trunk (created in LiveKit Cloud).
OUTBOUND_TRUNK = os.getenv("LIVEKIT_SIP_OUTBOUND_TRUNK") or "ST_sk7yUi8zGoGz"
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER") or "+34623004169"
# Tuned to sound human, not robotic (turbo model + these settings, chosen by A/B on real samples):
# lower stability = more natural intonation; high similarity = closer to the cloned voice; some style.
# These are DEFAULTS; the dashboard (Parámetros tab) can override each at call start.
DEFAULTS = {
    "ELEVENLABS_STABILITY": float(os.getenv("ELEVENLABS_STABILITY", "0.4")),
    "ELEVENLABS_SIMILARITY": float(os.getenv("ELEVENLABS_SIMILARITY", "0.9")),
    "ELEVENLABS_STYLE": float(os.getenv("ELEVENLABS_STYLE", "0.3")),
    "MIN_INTERRUPTION_DURATION": float(os.getenv("MIN_INTERRUPTION_DURATION", "0.5")),
    "MIN_INTERRUPTION_WORDS": float(os.getenv("MIN_INTERRUPTION_WORDS", "2")),
    "FALSE_INTERRUPTION_TIMEOUT": float(os.getenv("FALSE_INTERRUPTION_TIMEOUT", "2.0")),
    "MIN_ENDPOINTING_DELAY": float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4")),
}


class LanderAgent(Agent):
    def __init__(self, instructions: str, knowledge: list[dict] | None = None) -> None:
        super().__init__(instructions=instructions)
        # Optional per-call knowledge from the dashboard; falls back to knowledge.py.
        self._knowledge = knowledge or []
        # Detected branch for the call log; set when a transfer happens.
        self.branch: str | None = None

    # --- Escape hatch: warm transfer to a human -----------------------------
    @function_tool
    async def transfer_to_human(self, context: RunContext, department: str, summary: str) -> str:
        """Transfer the caller to a human agent.

        Call this when the caller asks for a person, refuses to talk to an AI,
        is frustrated, or the case is beyond what you can resolve.

        Args:
            department: "ventas" (sales) or "soporte" (support).
            summary: One-sentence summary of the caller's need, in Spanish, so the
                human doesn't have to ask them to repeat. E.g. "Empresa interesada
                en el plan para 15 vacantes, urgencia alta."
        """
        self.branch = "humano" if department not in ("ventas", "soporte") else department
        print(f"[TRANSFER] -> {department} | summary: {summary}")
        # Real warm transfer: dial the human over the netelip SIP outbound trunk, play
        # hold music to the caller, whisper them a summary, then merge the two calls.
        # Awaiting the task returns on success or RAISES on no-answer/voicemail/declined.
        try:
            await WarmTransferTask(
                sip_call_to=TRANSFER_NUMBER,
                sip_trunk_id=OUTBOUND_TRUNK,
                ringing_timeout=30,
                chat_ctx=self.chat_ctx,
                extra_instructions=(
                    f"Departamento: {department}. El caso, en una frase: {summary}. "
                    "Resume en español, con naturalidad, para que la persona no tenga que "
                    "preguntar de nuevo."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — no contestan / buzón / rechazo / error SIP
            print(f"[TRANSFER] failed: {exc!r}")
            return (
                "La transferencia no ha salido (ahora mismo no contestan). Discúlpate en una "
                "frase y ofrece seguir ayudando tú o tomar sus datos para devolverle la llamada."
            )
        # Éxito: el llamante ya está con una persona. No hables por encima de ellos.
        return "Ya está con una persona del equipo. Cierra tu intervención sin añadir nada más."

    # --- Sales: product/plan facts ------------------------------------------
    @function_tool
    async def get_product_info(self, context: RunContext, topic: str) -> dict:
        """Get factual product/plan info you are allowed to quote.

        Use this instead of improvising features or prices. If it returns
        found=false, read the `say` field's intent to the caller (in your own
        words) and offer a human — never invent the missing fact.

        Args:
            topic: What the caller asked about, e.g. "precio", "planes",
                "cómo funciona el matching".
        """
        # Prefer dashboard-managed knowledge (LanderOS → Knowledge tab): match the
        # topic against the editable facts. Never quote a price sentinel.
        topic_l = (topic or "").lower()
        for fact in self._knowledge:
            hay = f"{fact.get('topic', '')} {fact.get('id', '')}".lower()
            if any(w and w in hay for w in topic_l.split()):
                answer = str(fact.get("answer", ""))
                if "PENDIENTE_DEFINIR" in answer:
                    return {
                        "found": False,
                        "reason": "not_public",
                        "say": answer.split("—", 1)[-1].strip() or answer,
                    }
                return {"found": True, "topic": fact.get("topic"), "answer": answer}

        # Fallback to the baked-in knowledge module.
        from knowledge import get_product_info

        return get_product_info(topic)

    # --- Support: application/account lookup --------------------------------
    @function_tool
    async def lookup_application_status(self, context: RunContext, reference: str) -> dict:
        """Look up the status of a candidate application or account.

        Only state a status that comes back from here. If found=false, be honest
        and offer to transfer — do not guess.

        Args:
            reference: Email, phone, or reference the caller gives to identify their case.
        """
        from knowledge import lookup_application_status

        return lookup_application_status(reference)


async def entrypoint(ctx: JobContext) -> None:
    # Read dashboard config (prompt / knowledge / params) once at call start.
    # Blocking stdlib HTTP read, so run it off the event loop. Never raises.
    cfg = await asyncio.to_thread(load_config)

    def p(name: str) -> float:
        """Effective value: dashboard override if present, else baked-in default."""
        return cfg.params.get(name, DEFAULTS[name])

    instructions = cfg.prompt or PROMPT_PATH.read_text(encoding="utf-8")
    voice_settings = elevenlabs.VoiceSettings(
        stability=p("ELEVENLABS_STABILITY"),
        similarity_boost=p("ELEVENLABS_SIMILARITY"),
        style=p("ELEVENLABS_STYLE"),
        use_speaker_boost=True,
    )

    print(
        f"[VOICE] voice_id={ELEVEN_VOICE_ID!r} model={TTS_MODEL!r} api_key_set={bool(ELEVEN_API_KEY)} "
        f"prompt={'dashboard' if cfg.prompt else 'local'} knowledge={len(cfg.knowledge)}"
    )
    # Build the ElevenLabs TTS. `language` is only passed when forced (else auto-detect).
    tts_kwargs: dict = dict(model=TTS_MODEL, api_key=ELEVEN_API_KEY, voice_settings=voice_settings)
    if ELEVEN_VOICE_ID:
        tts_kwargs["voice_id"] = ELEVEN_VOICE_ID
    if TTS_LANGUAGE:
        tts_kwargs["language"] = TTS_LANGUAGE
    tts = elevenlabs.TTS(**tts_kwargs)

    session = AgentSession(
        stt=deepgram.STT(model=STT_MODEL, language="multi"),
        llm=anthropic.LLM(model=LLM_MODEL),
        tts=tts,
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
        # (preemptive_generation ya viene activado por defecto → menos latencia.)
        # --- Anti-corte (barge-in) sobre línea telefónica con ruido -----------
        # Exigir voz real (duración + palabras) antes de dar por interrumpido a
        # Lander, y retomar la frase si la interrupción fue en falso.
        min_interruption_duration=p("MIN_INTERRUPTION_DURATION"),
        min_interruption_words=int(p("MIN_INTERRUPTION_WORDS")),
        resume_false_interruption=True,
        false_interruption_timeout=p("FALSE_INTERRUPTION_TIMEOUT"),
        # --- Latencia: responder antes tras detectar fin de turno -------------
        min_endpointing_delay=p("MIN_ENDPOINTING_DELAY"),
    )

    agent = LanderAgent(instructions=instructions, knowledge=cfg.knowledge)

    # --- Multilingüe: cambia a la voz clonada del idioma del llamante ----------
    # Deepgram (multi) detecta el idioma de cada turno; si es catalán o inglés,
    # usamos la voz clonada correspondiente; cualquier otro idioma usa la voz ES.
    current_voice = {"id": ELEVEN_VOICE_ID}

    @session.on("user_input_transcribed")
    def _on_lang(ev) -> None:  # noqa: ANN001 (livekit event payload)
        try:
            if not getattr(ev, "is_final", False) or not getattr(ev, "language", None):
                return
            lang = str(ev.language).split("-")[0].lower()
            vid = VOICE_BY_LANG.get(lang, ELEVEN_VOICE_ID)
            if vid and vid != current_voice["id"]:
                current_voice["id"] = vid
                tts.update_options(voice_id=vid)
                print(f"[VOICE] lang={lang} -> voice={vid}")
        except Exception:  # noqa: BLE001 — nunca romper la llamada por esto
            pass

    # --- Call log: capture transcript + write one row on shutdown -------------
    transcript: list[dict] = []

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:  # noqa: ANN001 (livekit event payload)
        try:
            item = ev.item
            text = getattr(item, "text_content", None) or ""
            role = getattr(item, "role", "") or ""
            if text:
                transcript.append({"role": str(role), "text": str(text)})
        except Exception:  # noqa: BLE001 — never let logging break the call
            pass

    started_mono = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    # SIP caller number lands in the room name (e.g. "call_+34...") for inbound calls.
    caller = None
    room_name = getattr(ctx.room, "name", None)
    if room_name and "_" in room_name:
        caller = room_name.rsplit("_", 1)[-1] or None

    async def _write_call_log() -> None:
        await asyncio.to_thread(
            save_call_log,
            room=room_name,
            caller=caller,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=int(time.monotonic() - started_mono),
            turns=len(transcript),
            branch=agent.branch,
            voice_id=ELEVEN_VOICE_ID,
            transcript=transcript,
        )

    ctx.add_shutdown_callback(_write_call_log)

    await session.start(agent=agent, room=ctx.room)

    # Mandatory opening turn: greet + AI disclosure + offer to help.
    await session.generate_reply(
        instructions=(
            "Da tu primer turno: saluda, di tu nombre, revela claramente que eres "
            "una inteligencia artificial, y pregunta en qué puedes ayudar. Una sola "
            "frase natural, cálida y breve."
        )
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
