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
import contextlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.beta.workflows import WarmTransferTask
from livekit.plugins import anthropic, deepgram, elevenlabs, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from calllog import fetch_prior_calls, save_call_log
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
# netelip rejects the E.164 "+" with a SIP 404; it wants the international "00" prefix
# (e.g. +34623004169 -> 0034623004169). Normalise so config can still hold the "+" form.
TRANSFER_NUMBER = (os.getenv("TRANSFER_NUMBER") or "+34623004169").strip()
if TRANSFER_NUMBER.startswith("+"):
    TRANSFER_NUMBER = "00" + TRANSFER_NUMBER[1:]
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


def _format_history_es(chat_ctx) -> str:
    """Render the caller conversation as 'Llamante:' / 'Lander:' lines (Spanish labels)."""
    lines: list[str] = []
    try:
        for msg in chat_ctx.messages():
            if msg.role not in ("user", "assistant") or not msg.text_content:
                continue
            who = "Llamante" if msg.role == "user" else "Lander"
            lines.append(f"{who}: {msg.text_content}")
    except Exception:  # noqa: BLE001 — never let history formatting break a transfer
        return ""
    return "\n".join(lines)


def _last9(phone: str) -> str:
    """Last 9 digits of a phone (ignores +/00/country prefix + separators)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits


def _match_client(caller: str | None, clients: list[dict]) -> dict | None:
    """Match the inbound caller-ID against the fake client CRM (existing vs new)."""
    if not caller or not clients:
        return None
    key = _last9(caller)
    for c in clients:
        if _last9(str(c.get("phone", ""))) == key and key:
            return c
    return None


def _recall_block(prior: list[dict]) -> str:
    """Compact 'previous interactions' block from this caller's past call rows, so
    Lander can give continuity to a returning caller. Kept short to protect the
    context budget: date + detected branch + a trimmed gist of what the caller said.
    """
    if not prior:
        return ""
    lines = [
        "INTERACCIONES PREVIAS CON ESTE LLAMANTE (contexto para dar continuidad; "
        "NO lo leas en voz alta literalmente, úsalo solo si es relevante):"
    ]
    for row in prior:
        when = str(row.get("started_at") or "")[:10]
        branch = row.get("branch") or "—"
        turns = row.get("transcript") or []
        user_bits = [
            str(t.get("text", "")).strip()
            for t in turns
            if str(t.get("role", "")).startswith("user") and str(t.get("text", "")).strip()
        ]
        gist = " · ".join(user_bits)[:220]
        lines.append(f"- {when} (motivo: {branch}): {gist or 'sin transcripción'}")
    return "\n".join(lines)


def _match_knowledge(topic: str, knowledge: list[dict], category: str) -> dict | None:
    """Match a caller topic against the editable KB facts of ONE category.

    The dashboard splits knowledge into "sales" (quoted to new clients) and
    "support" (quoted to existing clients). A fact with no category is treated as
    "sales" for backward-compat. Returns the tool-result dict on a hit (found or a
    not-public sentinel), or None if nothing in this category matched (caller then
    falls back to the baked-in knowledge module).
    """
    topic_l = (topic or "").lower()
    for fact in knowledge:
        if (fact.get("category") or "sales") != category:
            continue
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
    return None


def _person_dept(person: str | None, directory: list[dict]) -> str | None:
    """Department a directory person belongs to (e.g. account manager -> «Cuentas»)."""
    if not person:
        return None
    for d in directory:
        if str(d.get("person", "")).strip().lower() == person.strip().lower():
            return str(d.get("department") or "") or None
    return None


def _caller_context(client: dict | None, directory: list[dict] | None = None) -> str:
    """One paragraph appended to the prompt telling Lander who is calling."""
    directory = directory or []
    if client:
        bits = [
            f"nombre {client['name']}" if client.get("name") else "",
            f"empresa {client['company']}" if client.get("company") else "",
            f"plan {client['plan']}" if client.get("plan") else "",
            f"estado {client['status']}" if client.get("status") else "",
        ]
        detail = ", ".join(b for b in bits if b) or "sin más datos"
        notes = client.get("notes")
        note_line = f" Notas internas (NO las leas en voz alta): {notes}." if notes else ""
        # Gestor de cuenta asignado: resuelto al instante del registro del cliente (sin
        # latencia). Es el destino por defecto del traspaso; si no hay, el dept General.
        am = str(client.get("account_manager") or "").strip()
        if am:
            am_dept = _person_dept(am, directory)
            dept_txt = f" (departamento {am_dept})" if am_dept else ""
            am_line = (
                f" Su gestor de cuenta asignado es {am}{dept_txt}: es a quien debes pasarle "
                "por defecto, salvo que el motivo sea claramente de otro departamento del directorio."
            )
        else:
            am_line = (
                " No tiene gestor de cuenta asignado: si hay que pasarle con alguien y el motivo "
                "no encaja claramente en un departamento, usa el departamento «General» (Recepción)."
            )
        return (
            "LLAMANTE: CLIENTE EXISTENTE reconocido por su número — "
            f"{detail}.{note_line}{am_line} No vendas: ya es cliente. Entiende POR QUÉ llama y "
            "pásale con la persona adecuada del directorio."
        )
    return (
        "LLAMANTE: DESCONOCIDO (no está en la base de clientes por su número, o el número "
        "llegó oculto). Trátalo como posible cliente NUEVO: descubre necesidad y vende. Pero "
        "si dice que ya trabaja con Joblanders (o el número venía oculto), pídele su email en "
        "una frase y usa la herramienta identify_by_email para reconocerlo; si lo encuentra, "
        "pasa a modo cliente existente con los datos que devuelva."
    )


def _directory_block(directory: list[dict]) -> str:
    """Format the transfer directory (departments + people + reason keywords)."""
    if not directory:
        return ""
    lines = ["DIRECTORIO DE TRANSFERENCIA (elige department + person según el motivo de la llamada):"]
    for d in directory:
        kws = d.get("keywords") or []
        kw = ", ".join(str(k) for k in kws) if isinstance(kws, list) else str(kws)
        lines.append(
            f"- department «{d.get('department', '')}» / person «{d.get('person', '')}» "
            f"({d.get('role', '')}) — motivos: {kw}"
        )
    return "\n".join(lines)


def _resolve_dest(person: str | None, directory: list[dict]) -> str:
    """Phone to dial for the chosen person (from the directory); fallback = TRANSFER_NUMBER.

    Numbers are normalised to netelip's "00" prefix (it rejects the "+" with a 404).
    Today every directory row points at the same number, so all transfers still ring
    James — but this real-life-tests that Lander picks the right person/department.
    """
    for d in directory:
        if str(d.get("person", "")).strip().lower() == (person or "").strip().lower():
            ph = str(d.get("phone") or "").strip()
            if ph:
                return "00" + ph[1:] if ph.startswith("+") else ph
            break
    return TRANSFER_NUMBER


# Guion del agente-susurro (lo que Lander le dice al HUMANO antes de unir las llamadas).
# Editable desde el dashboard (LanderOS → Traspaso, key `transfer_script` en Supabase);
# si no hay nada guardado se usa este texto. Tokens que se sustituyen en tiempo real:
#   {{department}} → "Comercial" | "Cuentas" | "General"
#   {{person}}     → persona destinataria del directorio (p.ej. "Ana")
#   {{summary}}    → resumen en una frase del caso
#   {{history}}    → conversación previa con el llamante (o aviso de que no la hubo)
DEFAULT_TRANSFER_SCRIPT = (
    "Eres Lander, el asistente de voz de Joblanders. Acabas de atender una llamada y "
    "la persona ha pedido hablar con un humano. AHORA hablas por teléfono con esa "
    "persona del equipo (el humano), NO con el llamante: el llamante NO te oye. "
    "Frases cortas, tono natural, como una persona al teléfono.\n\n"
    "Departamento: {{department}} — persona destinataria: {{person}}.\n"
    "Resumen en una frase: {{summary}}.\n\n"
    "Conversación previa con el llamante:\n"
    "{{history}}\n\n"
    "Actúa en este orden:\n"
    "1. Salúdale y dile en una frase que tienes una llamada para pasar a {{person}} "
    "(del equipo de {{department}}).\n"
    "2. Resúmele de qué ha hablado el llamante. Si no ha habido conversación real "
    "(solo quiere hablar con una persona), dilo claramente: no me ha contado nada, "
    "solo quiere hablar con alguien.\n"
    "3. Pídele permiso para pasarle la llamada.\n"
    "4. Si dice que SÍ: llama a la herramienta connect_to_caller para unir las "
    "llamadas (tú te retiras y cuelgas).\n"
    "5. Si dice que NO: pregúntale por qué o qué mensaje quiere que le dé al llamante. "
    "Cuando te lo diga, llama a decline_transfer con ese motivo como argumento. Si no "
    "da ningún motivo, usa decline_transfer con 'ocupado, que llame más tarde'."
)


class _LanderWarmTransfer(WarmTransferTask):
    """WarmTransferTask que avisa al LLAMANTE antes de unir las llamadas.

    La tarea de fábrica mantiene al llamante en espera (IO desactivada) hasta que
    hace el merge en un solo paso, así que el llamante nunca se entera de que le
    van a pasar. James quiere que AMBAS partes estén avisadas: al humano le avisa
    el agente-susurro (dice que sí = está avisado), y aquí, justo antes del merge,
    avisamos al llamante.
    """

    async def on_enter(self) -> None:
        # La tarea de fábrica marca al humano y luego "let the human speak first":
        # el agente-susurro se queda callado esperando a que el humano hable. Pero el
        # humano acaba de descolgar una llamada que LE hemos hecho nosotros, así que
        # espera que le saludemos primero — ambos se quedan en silencio y el humano
        # cuelga a los ~20s (CLIENT_INITIATED -> ROOM_DELETED). Arreglo: en cuanto el
        # humano contesta (super().on_enter() vuelve con wait_until_answered=True),
        # hacemos que el susurro hable primero con su guion (saludo + resumen + permiso).
        await super().on_enter()
        if self._human_agent_sess is not None:
            # Margen breve para que el track de audio del humano quede suscrito antes
            # de hablar (si no, se pierde el arranque de la frase).
            await asyncio.sleep(0.6)
            with contextlib.suppress(Exception):
                self._human_agent_sess.generate_reply()

    async def _merge_calls(self) -> None:
        # Paramos la música de espera y reactivamos el audio del llamante para que
        # nos oiga (el merge posterior y _set_result vuelven a hacerlo, es idempotente).
        if self._hold_audio_handle:
            self._hold_audio_handle.stop()
            self._hold_audio_handle = None
        self._set_io_enabled(True)
        with contextlib.suppress(Exception):
            await self.session.generate_reply(
                instructions=(
                    "Avisa al llamante en UNA sola frase corta y cálida: le pasas "
                    "ahora mismo con una persona del equipo. No hagas preguntas ni "
                    "añadas nada más."
                )
            )
        await super()._merge_calls()


class LanderAgent(Agent):
    def __init__(
        self,
        instructions: str,
        knowledge: list[dict] | None = None,
        transfer_script: str | None = None,
        directory: list[dict] | None = None,
        clients: list[dict] | None = None,
    ) -> None:
        super().__init__(instructions=instructions)
        # Optional per-call knowledge from the dashboard; falls back to knowledge.py.
        self._knowledge = knowledge or []
        # Guion del traspaso editable desde el dashboard; si no, el default de arriba.
        self._transfer_script = transfer_script or DEFAULT_TRANSFER_SCRIPT
        # Directorio de transferencia (departamentos/personas/teléfonos) para enrutar.
        self._directory = directory or []
        # Lista de clientes (CRM del dashboard) para el fallback de identificación por
        # email cuando el número no coincide o llega oculto.
        self._clients = clients or []
        # Detected branch for the call log; set when a transfer happens.
        self.branch: str | None = None

    # --- Fallback: identificar a un cliente por email -----------------------
    @function_tool
    async def identify_by_email(self, context: RunContext, email: str) -> dict:
        """Identify an existing client by email when their phone number wasn't
        recognised or came in withheld/hidden.

        Use this ONLY when the LLAMANTE block says the caller is unknown BUT the
        person indicates they are already a client (or the number came hidden).
        Ask them for their email in one short sentence, then call this. If it
        returns found=true, switch to existing-client mode using the returned
        context (don't sell; route them to their account manager). If found=false,
        read the `say` intent and treat them as a new client or offer a human.

        Args:
            email: The email address the caller gives, e.g. "laura@empresa.com".
        """
        key = (email or "").strip().lower()
        if not key:
            return {"found": False, "say": "No he entendido el email; pídeselo de nuevo."}
        for c in self._clients:
            if str(c.get("email", "")).strip().lower() == key and key:
                print(f"[CLIENT] identified by email={key!r} -> {c.get('company')}")
                return {"found": True, "context": _caller_context(c, self._directory)}
        print(f"[CLIENT] email={key!r} not found in CRM")
        return {
            "found": False,
            "say": (
                "No encuentro ese email en el sistema. Trátalo como cliente nuevo; si "
                "insiste en que ya es cliente, ofrécele pasar con una persona."
            ),
        }

    # --- Escape hatch: warm transfer to a human -----------------------------
    @function_tool
    async def transfer_to_human(
        self, context: RunContext, department: str, person: str, summary: str
    ) -> str:
        """Transfer the caller to the right human, chosen from the directory.

        Call this when the caller asks for a person, refuses to talk to an AI,
        is frustrated, or the case is beyond what you can resolve — and, for an
        existing client, once you understand why they are calling. Pick the
        department + person from the DIRECTORIO block whose reason keywords best
        match the caller's need.

        Args:
            department: The department from the directory, e.g. "Soporte" or "Comercial".
            person: The person from the directory to route to, e.g. "Ana".
            summary: One-sentence summary of the caller's need, in Spanish, so the
                human doesn't have to ask them to repeat. E.g. "Empresa interesada
                en el plan para 15 vacantes, urgencia alta."
        """
        self.branch = department or "humano"
        dest = _resolve_dest(person, self._directory)
        print(f"[TRANSFER] -> {department}/{person} | dial {dest} | summary: {summary}")

        # Conversación previa con el llamante, para que el humano tenga contexto real.
        history = _format_history_es(self.chat_ctx)
        history_block = history or (
            "(No ha habido conversación de fondo: el llamante solo ha pedido hablar con una persona.)"
        )

        # Guion del agente-susurro (el que habla con el HUMANO antes de unir las llamadas).
        # Plantilla editable desde el dashboard (self._transfer_script); sustituimos los
        # tokens {{department}}/{{summary}}/{{history}} con .replace (robusto ante llaves
        # sueltas que el usuario pueda escribir, a diferencia de str.format).
        whisper_instructions = (
            self._transfer_script
            .replace("{{department}}", department)
            .replace("{{person}}", person or "el equipo")
            .replace("{{summary}}", summary)
            .replace("{{history}}", history_block)
        )

        # Real warm transfer: dial the human over the netelip SIP outbound trunk, play
        # hold music to the caller, whisper them the summary, then merge the two calls.
        # Awaiting the task returns on success or RAISES on no-answer/voicemail/decline.
        try:
            await _LanderWarmTransfer(
                sip_call_to=dest,
                sip_trunk_id=OUTBOUND_TRUNK,
                ringing_timeout=30,
                chat_ctx=self.chat_ctx,
                instructions=whisper_instructions,
            )
        except Exception as exc:  # noqa: BLE001 — no contestan / buzón / rechazo / error SIP
            msg = str(exc)
            print(f"[TRANSFER] failed: {exc!r}")
            if "declined to connect" in msg:
                reason = msg.split("declined to connect:", 1)[-1].strip() or "está ocupado"
                return (
                    f"La persona no puede atenderte ahora. Motivo: {reason}. Trasládaselo al "
                    "llamante con tacto en una frase, discúlpate, y ofrece tomar sus datos para "
                    "devolverle la llamada o seguir ayudando tú."
                )
            if "voicemail" in msg:
                return (
                    "Ha saltado el buzón de voz. Dilo en una frase, discúlpate y ofrece seguir "
                    "ayudando tú o tomar sus datos para devolverle la llamada."
                )
            return (
                "La transferencia no ha salido (ahora mismo no contestan). Discúlpate en una "
                "frase y ofrece seguir ayudando tú o tomar sus datos para devolverle la llamada."
            )
        # Éxito: la persona y el llamante ya están unidos. Flujo de cierre: el agente
        # dice UNA frase breve de despedida a ambos y SALE de la llamada (si no, se
        # queda ahí de oyente). El texto devuelto abajo es lo que el LLM convierte en
        # esa despedida (una sola voz, sin doble reply). Luego cerramos la sesión: la
        # sala NO se borra (delete_room_on_close=False por defecto), así que llamante +
        # humano siguen hablando; solo se desconecta la IA.
        session = context.session

        async def _leave_call() -> None:
            # Margen para que termine la despedida antes de colgar (frase corta ~2-3s).
            await asyncio.sleep(4.0)
            with contextlib.suppress(Exception):
                await session.aclose()  # el agente cuelga; la llamada sigue entre ellos

        asyncio.create_task(_leave_call())
        return (
            "Hecho: ya están conectados. Di UNA sola frase muy corta y cálida avisando "
            "de que les dejas conectados y despidiéndote (p.ej. 'os dejo conectados, "
            "¡que vaya muy bien!'). No hagas preguntas ni añadas nada más."
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
        # Prefer dashboard-managed knowledge (LanderOS → Knowledge tab): match the
        # topic against the editable SALES facts only. Never quote a price sentinel.
        hit = _match_knowledge(topic, self._knowledge, "sales")
        if hit is not None:
            return hit

        # Fallback to the baked-in knowledge module.
        from knowledge import get_product_info

        return get_product_info(topic)

    # --- Support: knowledge for existing clients ----------------------------
    @function_tool
    async def get_support_info(self, context: RunContext, topic: str) -> dict:
        """Get factual support info you are allowed to quote to an EXISTING client
        who is fine talking to you (how the product works, common issues, policies).

        Use this instead of improvising. If it returns found=false, offer to pass
        them with their account person — never invent an answer or a policy.

        Args:
            topic: What the caller asked about, e.g. "no puedo entrar",
                "cómo recibo a los candidatos", "cambiar mi plan".
        """
        hit = _match_knowledge(topic, self._knowledge, "support")
        if hit is not None:
            return hit

        # Fallback to the baked-in knowledge module.
        from knowledge import get_support_info

        return get_support_info(topic)

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
    # WarmTransferTask spins its OWN whisper-agent session in a sub-room named
    # "<caller_room>-human-agent" (it summarises the case to the human, then bridges).
    # Our worker uses automatic dispatch, so LiveKit ALSO dispatches THIS entrypoint
    # to that sub-room — which would run the normal greeting agent on top of the
    # whisper agent, so the human hears "hola, soy Lander…" with zero transfer context
    # (exactly the bug seen 2026-07-16). Skip those rooms: let WarmTransferTask own them.
    room_name = ""
    try:
        room_name = (ctx.job.room.name if ctx.job and ctx.job.room else "") or ""
    except Exception:
        room_name = getattr(getattr(ctx, "room", None), "name", "") or ""
    if room_name.endswith("-human-agent"):
        print(f"[TRANSFER] skip auto-dispatch to warm-transfer sub-room {room_name!r}")
        return

    # Pre-warm rooms: the web "call" button creates a "warmup-*" room a few seconds before
    # the real PSTN call connects, which auto-dispatches THIS entrypoint and thus cold-starts
    # the worker ahead of time. We only need the process awake — do NO work (no config read,
    # no session, no TTS/LLM), so the warm-up costs ~nothing. By the time the caller dials,
    # the worker is already hot and answers instantly.
    if room_name.startswith("warmup-"):
        print(f"[PREWARM] worker warmed via {room_name!r}; returning without a session")
        return

    # Read dashboard config (prompt / knowledge / params) once at call start.
    # Blocking stdlib HTTP read, so run it off the event loop. Never raises.
    cfg = await asyncio.to_thread(load_config)

    def p(name: str) -> float:
        """Effective value: dashboard override if present, else baked-in default."""
        return cfg.params.get(name, DEFAULTS[name])

    # SIP caller number lands in the room name for inbound calls. LiveKit names the
    # room "call_<phone>_<random>" (e.g. "call_+34623004169_H9CcJpBaDFF5"), so we must
    # take the part BETWEEN "call_" and the trailing random id — NOT rsplit's last
    # chunk (that grabbed the random id and made every caller look unknown).
    caller = None
    call_room_name = getattr(ctx.room, "name", None)
    if call_room_name and call_room_name.startswith("call_"):
        caller = call_room_name[len("call_"):].rsplit("_", 1)[0] or None
    elif call_room_name and "_" in call_room_name:
        caller = call_room_name.rsplit("_", 1)[-1] or None

    # New vs existing: match the caller-ID against the fake client CRM, then tell Lander
    # who is calling and give it the transfer directory so it can route to the right person.
    client = _match_client(caller, cfg.clients)
    if client:
        print(
            f"[CLIENT] caller={caller!r} -> EXISTING {client.get('company')} "
            f"| gestor={client.get('account_manager') or '—'}"
        )
    else:
        print(f"[CLIENT] caller={caller!r} -> NEW/unknown")

    # Recall: past calls from this same caller become context for continuity.
    # Best-effort read (never raises); matched by last-9-digits like the CRM.
    prior_calls = await asyncio.to_thread(fetch_prior_calls, caller)
    print(f"[RECALL] caller={caller!r} -> {len(prior_calls)} prior call(s)")

    instructions = cfg.prompt or PROMPT_PATH.read_text(encoding="utf-8")
    instructions = instructions + "\n\n" + _caller_context(client, cfg.directory)
    recall_block = _recall_block(prior_calls)
    if recall_block:
        instructions = instructions + "\n\n" + recall_block
    directory_block = _directory_block(cfg.directory)
    if directory_block:
        instructions = instructions + "\n\n" + directory_block
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
        stt=deepgram.STT(model=STT_MODEL, language="es"),
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

    agent = LanderAgent(
        instructions=instructions,
        knowledge=cfg.knowledge,
        transfer_script=cfg.transfer_script,
        directory=cfg.directory,
        clients=cfg.clients,
    )

    # Idioma bloqueado a español: Deepgram STT en language="es" y voz ES fija.
    # (Se retiró el auto-cambio de voz por idioma: en línea telefónica con ruido
    # detectaba mal el español y saltaba a otro idioma/voz a mitad de llamada.)

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

    async def _write_call_log() -> None:
        # Browser test sessions (Probar Lander) run the full agent but must NOT pollute
        # the real-call metrics in the Estado tab — skip persisting them.
        if (call_room_name or "").startswith("webtest-"):
            print(f"[CALLLOG] skip webtest room {call_room_name!r}")
            return

        # Real per-provider usage (not a transcript-length estimate) — see
        # AgentSession.usage in livekit-agents' metrics/usage.py.
        tts_characters = tts_audio_seconds = stt_audio_seconds = 0
        llm_input_tokens = llm_output_tokens = 0
        try:
            for u in session.usage.model_usage:
                if u.type == "tts_usage":
                    tts_characters += u.characters_count
                    tts_audio_seconds += u.audio_duration
                elif u.type == "stt_usage":
                    stt_audio_seconds += u.audio_duration
                elif u.type == "llm_usage":
                    llm_input_tokens += u.input_tokens
                    llm_output_tokens += u.output_tokens
        except Exception:  # noqa: BLE001 — usage is a nice-to-have, never break shutdown
            pass

        await asyncio.to_thread(
            save_call_log,
            room=call_room_name,
            caller=caller,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=int(time.monotonic() - started_mono),
            turns=len(transcript),
            branch=agent.branch,
            voice_id=ELEVEN_VOICE_ID,
            transcript=transcript,
            tts_characters=tts_characters,
            tts_audio_seconds=tts_audio_seconds,
            stt_audio_seconds=stt_audio_seconds,
            llm_input_tokens=llm_input_tokens,
            llm_output_tokens=llm_output_tokens,
        )

    ctx.add_shutdown_callback(_write_call_log)

    await session.start(agent=agent, room=ctx.room)

    # Mandatory opening turn: STATIC greeting via TTS (no LLM) so the caller ALWAYS
    # hears Lander instantly, even if the first Anthropic completion is slow or times
    # out on a cold start. Covers name + AI disclosure + offer to help. The LLM only
    # kicks in from the caller's first reply onward (with client/recall context).
    try:
        await session.say(
            "Hola, soy Lander, el asistente virtual con inteligencia artificial de "
            "Joblanders. ¿En qué puedo ayudarte?",
            allow_interruptions=True,
        )
    except Exception as exc:  # noqa: BLE001 — never let the greeting break the call
        print(f"[GREETING] static say failed ({exc!r}); falling back to LLM reply")
        await session.generate_reply(
            instructions=(
                "Da tu primer turno: saluda, di tu nombre, revela claramente que eres "
                "una inteligencia artificial, y pregunta en qué puedes ayudar. Una sola "
                "frase natural, cálida y breve."
            )
        )

    # Ambient background sound (call-center feel). Started AFTER the greeting so it can
    # never delay or hijack the mandatory opening turn — that ordering broke calls once.
    # Fully guarded: if the audio player fails, the conversation continues unaffected.
    try:
        background_audio = BackgroundAudioPlayer(
            ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.15),
        )
        await background_audio.start(room=ctx.room, agent_session=session)
    except Exception as exc:  # noqa: BLE001 — background audio is cosmetic, never fatal
        print(f"[BGAUDIO] background audio failed to start ({exc!r}); continuing silent")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
