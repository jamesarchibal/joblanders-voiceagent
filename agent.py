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

import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, RunContext, WorkerOptions, cli, function_tool
from livekit.plugins import anthropic, cartesia, deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(override=True)

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_es.md"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID") or None


class LanderAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=PROMPT_PATH.read_text(encoding="utf-8"))

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
        # Milestone 2 will do a real SIP REFER + whisper summary here. For now we
        # log the intent and tell the caller what's happening.
        print(f"[TRANSFER] -> {department} | summary: {summary}")
        return (
            f"Transferencia iniciada al equipo de {department}. Di al llamante en una "
            "frase cálida que le pasas con una persona y que ya le has resumido el caso."
        )

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
    session = AgentSession(
        stt=deepgram.STT(model=STT_MODEL, language="multi"),
        llm=anthropic.LLM(model=LLM_MODEL),
        tts=cartesia.TTS(voice=CARTESIA_VOICE_ID) if CARTESIA_VOICE_ID else cartesia.TTS(),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    await session.start(agent=LanderAgent(), room=ctx.room)

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
