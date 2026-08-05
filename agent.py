"""Joblanders inbound voice agent — master receptionist + invisible sub-modes.

Cascaded pipeline: Deepgram (STT) -> Claude (brain) -> ElevenLabs (TTS),
orchestrated by LiveKit Agents with semantic turn detection + barge-in.

One Lander, one voice, one call — three modes. It starts in **Recepción** (the
master/triage mode), understands why the caller is phoning in 1-2 questions, then
hands off INVISIBLY (same voice, no announcement) to **Ventas** or **Soporte**,
carrying the full conversation memory (chat_ctx). The caller never perceives the
switch. A real human warm-transfer (transfer_to_human) stays as the last-resort
fallback. Handoff = a @function_tool returning the next Agent; on_enter then
continues the conversation seamlessly.

Run:
    python agent.py console      # local mic/speakers, no phone, no LiveKit keys
    python agent.py dev          # connect to LiveKit as a worker (needs LIVEKIT_*)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from livekit import api
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
    get_job_context,
    metrics,
)
from livekit.agents.beta.workflows import WarmTransferTask
from livekit.plugins import anthropic, deepgram, elevenlabs, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from calllog import fetch_prior_calls, save_call_log
from config import load_config

load_dotenv(override=True)

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_es.md"
# Per-mode role blocks appended to the shared base. One Lander, three modes:
# Recepción (triage) -> invisible handoff -> Ventas / Soporte, same voice, same call.
ROLE_PROMPT_PATHS = {
    "recepcion": Path(__file__).parent / "prompts" / "role_recepcion_es.md",
    "ventas": Path(__file__).parent / "prompts" / "role_ventas_es.md",
    "soporte": Path(__file__).parent / "prompts" / "role_soporte_es.md",
}
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
# "multi" = code-switching (nova-3 detecta el idioma por turno y lo devuelve).
# Pon STT_LANGUAGE="es" para volver a bloquear el STT a español.
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "multi")
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
# netelip wants the SPANISH NATIONAL 9-digit number (e.g. 623004169), NOT E.164.
# Tested against sip.netelip.com: 0034623004169 -> SIP 503, 34623004169 -> 603 Declined,
# 623004169 -> connects/rings. So strip any +34 / 0034 / 34 country prefix down to national.
def _netelip_national(phone: str) -> str:
    """Normalise a number to netelip's Spanish national 9-digit form (strip +34/0034/34)."""
    t = "".join(ch for ch in (phone or "") if ch.isdigit())
    if t.startswith("0034"):
        t = t[4:]
    elif t.startswith("34") and len(t) > 9:
        t = t[2:]
    return t


# Config can still hold the "+" form; we normalise here.
TRANSFER_NUMBER = _netelip_national(os.getenv("TRANSFER_NUMBER") or "+34623004169")
# Tuned to sound human, not robotic (turbo model + these settings, chosen by A/B on real samples):
# lower stability = more natural intonation; high similarity = closer to the cloned voice; some style.
# These are DEFAULTS; the dashboard (Parámetros tab) can override each at call start.
DEFAULTS = {
    "ELEVENLABS_STABILITY": float(os.getenv("ELEVENLABS_STABILITY", "0.4")),
    "ELEVENLABS_SIMILARITY": float(os.getenv("ELEVENLABS_SIMILARITY", "0.9")),
    "ELEVENLABS_STYLE": float(os.getenv("ELEVENLABS_STYLE", "0.3")),
    "MIN_INTERRUPTION_DURATION": float(os.getenv("MIN_INTERRUPTION_DURATION", "0.4")),
    "MIN_INTERRUPTION_WORDS": float(os.getenv("MIN_INTERRUPTION_WORDS", "1")),
    "FALSE_INTERRUPTION_TIMEOUT": float(os.getenv("FALSE_INTERRUPTION_TIMEOUT", "2.0")),
    "MIN_ENDPOINTING_DELAY": float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4")),
}


def _handoff_turn_spoke(chat_ctx) -> bool:
    """True if the agent that just handed off SPOKE on the handoff turn (e.g. Recepción
    asked a qualifying question) rather than switching silently.

    Used by the destination mode's on_enter to decide whether to auto-reply: if the
    previous mode just asked something, the floor is with the caller, so we stay quiet
    (no talking over). If the handoff was silent, we reply to avoid dead air.
    """
    try:
        for msg in reversed(list(chat_ctx.messages())):
            if msg.role == "assistant":
                return bool(msg.text_content and msg.text_content.strip())
    except Exception:  # noqa: BLE001 — never let this gate break a handoff
        return False
    return False


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


# Palabras vacías (es) + genéricas que no deben disparar un match de KB. Las de <3
# letras se descartan siempre; estas cubren las de 3+ que aún son ruido.
_KB_STOP = {
    "que", "como", "cual", "cuales", "donde", "para", "por", "con", "sin",
    "los", "las", "del", "una", "uno", "unos", "unas", "mis", "tus", "sus",
    "esta", "este", "esto", "eso", "esa", "ese", "mas", "muy", "hay", "son",
    "pero", "aqui", "ahi", "desde", "hacia", "sobre", "cuando", "porque",
    "quiero", "puedo", "puede", "tengo", "necesito", "favor", "porfa", "hola",
    "quisiera", "podria", "vale", "gracias", "sabes", "dime", "algo", "cosa",
}


def _kb_tokens(s: str) -> set[str]:
    """Tokens significativos de un texto: sin acentos, >=3 letras, sin stopwords."""
    import re
    import unicodedata

    norm = unicodedata.normalize("NFKD", (s or "").lower())
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", norm) if len(w) >= 3 and w not in _KB_STOP}


def _match_knowledge(topic: str, knowledge: list[dict], category: str) -> dict | None:
    """Match a caller topic against the editable KB facts of ONE category.

    The dashboard splits knowledge into "sales" (quoted to new clients) and
    "support" (quoted to existing clients). A fact with no category is treated as
    "sales" for backward-compat. Ranks facts by how many MEANINGFUL tokens overlap
    (whole words, accent-insensitive) so short stopwords like "es"/"no" can't
    false-match; the best-scoring fact wins (ties broken by order). Returns the
    tool-result dict on a hit (found or a not-public sentinel), or None if nothing
    overlapped (caller then falls back to the baked-in knowledge module).
    """
    caller = _kb_tokens(topic)
    if not caller:
        return None
    best: dict | None = None
    best_score = 0
    for fact in knowledge:
        if (fact.get("category") or "sales") != category:
            continue
        score = len(caller & _kb_tokens(f"{fact.get('topic', '')} {fact.get('id', '')}"))
        if score > best_score:
            best_score, best = score, fact
    if best is None:
        return None
    answer = str(best.get("answer", ""))
    if "PENDIENTE_DEFINIR" in answer:
        return {
            "found": False,
            "reason": "not_public",
            "say": answer.split("—", 1)[-1].strip() or answer,
        }
    return {"found": True, "topic": best.get("topic"), "answer": answer}


def _crm_log_lead(payload: dict) -> dict:
    """Insert one qualified inbound lead into the dashboard lab CRM (crm_companies,
    business='lab'). Blocking urllib POST — call via asyncio.to_thread. Best-effort:
    returns {"ok": bool, ...}, never raises.
    """
    import urllib.request

    from config import SUPABASE_KEY, SUPABASE_URL

    if not (SUPABASE_URL and SUPABASE_KEY):
        return {"ok": False, "reason": "no_supabase_creds"}
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/crm_companies",
        data=json.dumps([payload]).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 (trusted URL)
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except Exception as exc:  # noqa: BLE001 — degrade gracefully; the call goes on
        return {"ok": False, "reason": repr(exc)}


def _ticket_create(payload: dict) -> dict:
    """Insert one support ticket into the dashboard (landeros_tickets). Blocking
    urllib POST — call via asyncio.to_thread. Best-effort: returns {"ok": bool, ...},
    never raises.
    """
    import urllib.request

    from config import SUPABASE_KEY, SUPABASE_URL

    if not (SUPABASE_URL and SUPABASE_KEY):
        return {"ok": False, "reason": "no_supabase_creds"}
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/landeros_tickets",
        data=json.dumps([payload]).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 (trusted URL)
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except Exception as exc:  # noqa: BLE001 — degrade gracefully; the call goes on
        return {"ok": False, "reason": repr(exc)}


# --- Google Calendar auto-booking (SDR demos) -------------------------------
# The service account uses domain-wide delegation to act as GCAL_SUBJECT and drop
# demo events on the "Entrevistas" calendar. Creds arrive as a base64 secret so we
# never commit the JSON. All best-effort: missing creds -> the tool declines cleanly.
_GCAL_CAL_ID = (
    os.getenv("GCAL_CALENDAR_ID")
    or "c_5c4176e200265f1be45114e83f5f033285b78400281ee40e1639615a4e4a4924@group.calendar.google.com"
)
_GCAL_SUBJECT = os.getenv("GCAL_IMPERSONATE") or "james@joblanders.com"
_GCAL_TZ = "Europe/Madrid"


def _gcal_token() -> str | None:
    """Mint a Calendar access token via the service account (domain-wide delegation,
    impersonating _GCAL_SUBJECT). Returns None if creds/libs are missing.
    """
    raw = os.getenv("GOOGLE_SA_JSON_B64")
    if not raw:
        return None
    try:
        import base64

        from google.auth.transport.requests import Request as _GReq
        from google.oauth2 import service_account

        info = json.loads(base64.b64decode(raw))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"], subject=_GCAL_SUBJECT,
        )
        creds.refresh(_GReq())
        return creds.token
    except Exception as exc:  # noqa: BLE001
        print(f"[CAL] token error: {exc!r}")
        return None


def _gcal_book_demo(payload: dict) -> dict:
    """Book a demo on the Entrevistas calendar (impersonated). payload keys:
    summary, description, start_iso, end_iso, attendee (optional email).
    Checks freebusy for the window first and refuses if busy. Never raises.
    """
    import urllib.parse
    import urllib.request

    token = _gcal_token()
    if not token:
        return {"ok": False, "reason": "no_gcal_creds"}
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Is the requested window free?
    try:
        fb_body = {
            "timeMin": payload["start_iso"],
            "timeMax": payload["end_iso"],
            "timeZone": _GCAL_TZ,
            "items": [{"id": _GCAL_CAL_ID}],
        }
        fb_req = urllib.request.Request(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            data=json.dumps(fb_body).encode("utf-8"),
            method="POST",
            headers=hdr,
        )
        with urllib.request.urlopen(fb_req, timeout=8) as resp:  # noqa: S310
            fb = json.loads(resp.read())
        if fb.get("calendars", {}).get(_GCAL_CAL_ID, {}).get("busy", []):
            return {"ok": False, "reason": "busy"}
    except Exception as exc:  # noqa: BLE001 — fail closed, never double-book blindly
        print(f"[CAL] freebusy error: {exc!r}")
        return {"ok": False, "reason": f"freebusy:{exc!r}"}

    # 2) Insert the event.
    event = {
        "summary": payload["summary"],
        "description": payload.get("description", ""),
        "start": {"dateTime": payload["start_iso"], "timeZone": _GCAL_TZ},
        "end": {"dateTime": payload["end_iso"], "timeZone": _GCAL_TZ},
    }
    if payload.get("attendee"):
        event["attendees"] = [{"email": payload["attendee"]}]
    try:
        cal = urllib.parse.quote(_GCAL_CAL_ID)
        ins_req = urllib.request.Request(
            f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events?sendUpdates=all",
            data=json.dumps(event).encode("utf-8"),
            method="POST",
            headers=hdr,
        )
        with urllib.request.urlopen(ins_req, timeout=8) as resp:  # noqa: S310
            created = json.loads(resp.read())
        return {"ok": True, "id": created.get("id"), "htmlLink": created.get("htmlLink")}
    except Exception as exc:  # noqa: BLE001
        print(f"[CAL] insert error: {exc!r}")
        return {"ok": False, "reason": f"insert:{exc!r}"}


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

    Numbers are normalised to netelip's Spanish national 9-digit form (it rejects +34/0034).
    Today every directory row points at the same number, so all transfers still ring
    James — but this real-life-tests that Lander picks the right person/department.
    """
    for d in directory:
        if str(d.get("person", "")).strip().lower() == (person or "").strip().lower():
            ph = str(d.get("phone") or "").strip()
            if ph:
                return _netelip_national(ph)
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
            await asyncio.sleep(0.3)
            # Frase fija e instantánea (arranca TTS en ~75ms) para no dejar silencio
            # muerto mientras el LLM genera el resumen. El humano acaba de descolgar
            # una llamada que le hemos hecho, así que espera oír algo de inmediato.
            with contextlib.suppress(Exception):
                await self._human_agent_sess.say(
                    "Hola, soy Lander, de Joblanders. Tengo una llamada para pasarte, "
                    "dame un segundo."
                )
            # El resumen real lo genera el LLM; le decimos que YA ha saludado para que
            # no repita el saludo y vaya directo al resumen y a pedir permiso.
            with contextlib.suppress(Exception):
                self._human_agent_sess.generate_reply(
                    instructions=(
                        "Ya has saludado y dicho que tienes una llamada para pasar. "
                        "Ahora, sin volver a saludar, resume en una frase de qué ha "
                        "hablado el llamante y pídele permiso para pasarle la llamada."
                    )
                )

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


@dataclass
class _CallState:
    """Estado mutable compartido por los tres modos durante UNA llamada.

    Como el modo (Recepción → Ventas/Soporte) puede cambiar a mitad de llamada,
    la rama detectada y el guard de traspaso viven aquí, no en una instancia de
    agente concreta: así el log de la llamada lee la rama final sin importar en
    qué modo terminó, y el guard de idempotencia del traspaso a humano se respeta
    aunque el traspaso se pida desde un modo distinto al inicial.
    """

    branch: str | None = None
    transferring: bool = False
    # True cuando la llamada arranca directamente en Ventas/Soporte por su propio
    # número (routing_mode="separate"): sin Recepción, sin triaje y sin cambio de
    # modo. Los agentes usan este flag para no re-saludar ni cruzar de modo.
    standalone: bool = False


@dataclass
class _Bundle:
    """Todo lo que cada modo necesita para construirse (prompt + datos de la llamada).

    Se crea una sola vez en el entrypoint y se comparte entre los tres modos, de
    modo que un cambio de modo (handoff) reconstruye el prompt del nuevo modo sin
    volver a leer config ni CRM: solo cambia el bloque «# Tu rol ahora».
    """

    base_prompt: str
    role_prompts: dict  # {"recepcion": str, "ventas": str, "soporte": str}
    caller_context: str
    recall_block: str
    directory_block: str
    knowledge: list = field(default_factory=list)
    transfer_script: str | None = None
    directory: list = field(default_factory=list)
    clients: list = field(default_factory=list)
    state: _CallState = field(default_factory=_CallState)
    # Enrutado de la llamada: "unified" (Recepción + subagentes) | "separate".
    routing_mode: str = "unified"
    # Nombre con el que abre el agente en el saludo (por defecto Lander). En modo
    # "separate" cada agente puede tener su propio nombre configurado.
    agent_name: str = "Lander"
    # Caller-ID entrante (para registrar el teléfono al loguear un lead en el CRM).
    caller: str | None = None

    def instructions_for(self, role: str) -> str:
        now = datetime.now(ZoneInfo("Europe/Madrid"))
        _DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        _MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                  "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        # Tabla de fechas YA resuelta: Haiku calcula mal el día de la semana, así que
        # NO le pedimos que compute — le damos día->fecha exacta de los próximos 14 días.
        rows = []
        for i in range(1, 15):
            d = now + timedelta(days=i)
            rows.append(f"- {_DIAS[d.weekday()]} {d.day} de {_MESES[d.month]} = {d:%Y-%m-%d}")
        date_line = (
            f"Contexto temporal: hoy es {_DIAS[now.weekday()]} {now.day} de "
            f"{_MESES[now.month]} de {now.year} ({now:%Y-%m-%d}), hora local "
            f"{now:%H:%M} (Europe/Madrid).\n"
            "FECHAS EXACTAS de los próximos días — al agendar usa SIEMPRE la fecha de "
            "esta tabla, NUNCA calcules el día de la semana tú mismo:\n"
            + "\n".join(rows) + "\n"
            "Si el llamante dice solo un día ('el martes') sin más, usa el PRIMERO de "
            "la tabla (el más próximo en el futuro)."
        )
        parts = [self.base_prompt, self.role_prompts.get(role, ""), self.caller_context, date_line]
        if self.recall_block:
            parts.append(self.recall_block)
        if self.directory_block:
            parts.append(self.directory_block)
        return "\n\n".join(p for p in parts if p and p.strip())


class _BaseLanderAgent(Agent):
    """Base común de los tres modos de Lander.

    Un solo agente, una sola voz, una sola llamada: empieza en Recepción y cambia
    de modo de forma invisible a Ventas/Soporte llevándose la memoria de la
    conversación (chat_ctx). Aquí viven las tools compartidas por todos los modos:
    identificar a un cliente por email y el traspaso REAL a un humano (fallback).
    Cada modo fija su `role` y añade sus propias tools.
    """

    role: str = "recepcion"

    def __init__(self, bundle: _Bundle, chat_ctx=None) -> None:
        # chat_ctx: al cambiar de modo pasamos el histórico del modo anterior para
        # que el nuevo modo continúe la conversación sin que el llamante note nada.
        super().__init__(instructions=bundle.instructions_for(self.role), chat_ctx=chat_ctx)
        self._bundle = bundle
        # Optional per-call knowledge from the dashboard; falls back to knowledge.py.
        self._knowledge = bundle.knowledge or []
        # Guion del traspaso editable desde el dashboard; si no, el default de arriba.
        self._transfer_script = bundle.transfer_script or DEFAULT_TRANSFER_SCRIPT
        # Directorio de transferencia (departamentos/personas/teléfonos) para enrutar.
        self._directory = bundle.directory or []
        # Lista de clientes (CRM del dashboard) para el fallback de identificación por
        # email cuando el número no coincide o llega oculto.
        self._clients = bundle.clients or []
        # Estado mutable compartido (rama detectada + guard de traspaso).
        self._state = bundle.state

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

    # --- Cierre: despedirse y colgar ----------------------------------------
    @function_tool
    async def finalizar_llamada(self, context: RunContext) -> str:
        """End the call: say a short goodbye and hang up.

        Use this ONLY when the caller has clearly finished — they say "adiós",
        "nada más", "gracias, eso es todo", "hasta luego" — or when the reason for
        the call is fully resolved and nothing is pending. Do NOT use it while the
        caller still has questions or mid-conversation. After you call this, say a
        single warm farewell sentence and the line hangs up on its own.
        """
        job_ctx = get_job_context()

        async def _end() -> None:
            # Margen para que suene la despedida completa antes de colgar.
            await asyncio.sleep(4.0)
            # Borrar la sala cuelga la pata SIP (PSTN) del llamante; aclose desconecta
            # al agente. Ambos con suppress: colgar nunca debe petar la llamada.
            with contextlib.suppress(Exception):
                await job_ctx.api.room.delete_room(
                    api.DeleteRoomRequest(room=job_ctx.room.name)
                )
            with contextlib.suppress(Exception):
                await context.session.aclose()

        asyncio.create_task(_end())
        return (
            "Di UNA sola frase corta y cálida de despedida (p.ej. 'Gracias por llamar "
            "a Joblanders, ¡que tengas muy buen día!') y NADA más. La llamada se "
            "colgará sola tras la despedida; no hagas más preguntas."
        )

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
        # Idempotencia: si ya hay un traspaso en marcha, no vuelvas a marcar al humano.
        # (El LLM a veces repite la tool-call durante los ~30s de tono → 2-3 llamadas.)
        if self._state.transferring:
            print("[TRANSFER] ignorada llamada duplicada a transfer_to_human (una ya en curso)")
            return (
                "Ya estás conectando al llamante con una persona ahora mismo. NO vuelvas "
                "a llamar a transfer_to_human; espera en silencio a que se complete."
            )
        self._state.transferring = True
        self._state.branch = department or "humano"
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
            # Falló (no contestan / buzón / rechazo / SIP): liberamos el guard para que,
            # si el llamante lo vuelve a pedir explícitamente, se pueda reintentar.
            self._state.transferring = False
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


class VentasAgent(_BaseLanderAgent):
    """Modo VENTAS: descubre necesidad, presenta el producto y cierra con Comercial.

    Se entra aquí desde Recepción (o corrigiendo desde Soporte) llevándose el
    histórico; para el llamante es la misma persona sin cortes.
    """

    role = "ventas"

    async def on_enter(self) -> None:
        # Modo "separate": este agente es el PRIMER contacto (su propio número), así
        # que el saludo estático del entrypoint (nombre + aviso de IA) abre la
        # llamada; no generamos respuesta aquí para no duplicar la apertura.
        if self._state.standalone:
            return
        # Transición invisible: en cuanto tomamos el control, respondemos al último
        # turno del llamante ya en modo Ventas (con las tools/mensaje de ventas),
        # sin volver a saludar. generate_reply() usa el chat_ctx heredado.
        # PERO si Recepción acaba de hablar en el turno del handoff (p.ej. hizo una
        # pregunta de calificación mientras se hacía el cambio), el turno es del
        # llamante: nos callamos y esperamos su respuesta para no hablar encima.
        if _handoff_turn_spoke(self.chat_ctx):
            return
        self.session.generate_reply()

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

    # --- Sales: registrar un lead cualificado en el lab CRM -----------------
    @function_tool
    async def log_lead(
        self, context: RunContext, empresa: str, contacto: str = "", necesidad: str = "",
    ) -> dict:
        """Registra un lead CUALIFICADO en el CRM (empresa nueva interesada en joblanders.ai).

        Llámala SOLO cuando: es una empresa nueva (no un cliente existente), te ha
        dado el NOMBRE de su empresa, y muestra interés real (quiere info, una demo,
        o avanzar). NO la llames sin interés, ni sin el nombre de la empresa. Una
        sola vez por llamada. No le anuncies al llamante que lo apuntas en un CRM;
        simplemente continúa la conversación con normalidad.

        Args:
            empresa: Nombre de la empresa del llamante. Obligatorio.
            contacto: Nombre de la persona que llama, si lo da.
            necesidad: Una frase (en español) con lo que busca / su caso —tipo de
                agencia, volumen, qué quiere automatizar— para que Comercial lo retome.
        """
        empresa = (empresa or "").strip()
        if not empresa:
            return {"logged": False, "say": "Pídele el nombre de su empresa antes de registrar."}
        caller_phone = getattr(self._bundle, "caller", None) or ""
        payload = {
            "name": empresa,
            "business": "lab",
            "source": "voice",
            "relationship": "prospecto",
            "stage": "Contactado",
            "contact_name": (contacto or "").strip(),
            "contact_phone": caller_phone,
            "notes": (necesidad or "").strip(),
            "next_action": "Llamada entrante cualificada por Lander — retomar desde Comercial",
        }
        res = await asyncio.to_thread(_crm_log_lead, payload)
        if res.get("ok"):
            print(f"[LEAD] logged empresa={empresa!r} phone={caller_phone!r}")
            return {"logged": True}
        print(f"[LEAD] FAILED empresa={empresa!r} -> {res}")
        return {"logged": False}

    # --- Sales: agendar una demo en el calendario de Comercial --------------
    @function_tool
    async def book_demo(
        self,
        context: RunContext,
        empresa: str,
        fecha_hora_iso: str,
        email: str = "",
        contacto: str = "",
        duracion_min: int = 30,
    ) -> dict:
        """Agenda una DEMO de joblanders.ai en el calendario de Comercial.

        Llámala cuando la persona quiere una demo y ya tienes: nombre de su EMPRESA
        y una FECHA/HORA concreta que prefiera (pídele también un email para enviarle
        la invitación). Convierte la hora que te diga a formato ISO local, p.ej.
        "el martes a las cuatro" -> "2026-08-05T16:00:00", usando la fecha de HOY del
        contexto temporal. Confirma en voz SOLO el hueco que devuelva la herramienta;
        si devuelve ocupado, propón otra hora y vuelve a llamarla. Nunca inventes ni
        confirmes un hueco sin llamar a la herramienta. Una demo por llamada.

        Args:
            empresa: Nombre de la empresa. Obligatorio.
            fecha_hora_iso: Inicio en ISO local "YYYY-MM-DDTHH:MM:SS". Obligatorio.
            email: Email de contacto para enviarle la invitación.
            contacto: Nombre de la persona que llama, si lo da.
            duracion_min: Duración en minutos (por defecto 30).
        """
        empresa = (empresa or "").strip()
        start_raw = (fecha_hora_iso or "").strip()
        if not empresa or not start_raw:
            return {"booked": False, "say": "Necesito el nombre de la empresa y una fecha y hora concreta."}
        try:
            start_dt = datetime.fromisoformat(start_raw)
        except ValueError:
            return {"booked": False, "say": "No entendí la fecha; pídela de nuevo con día y hora."}
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=ZoneInfo("Europe/Madrid"))
        end_dt = start_dt + timedelta(minutes=max(15, int(duracion_min or 30)))
        caller_phone = getattr(self._bundle, "caller", None) or ""
        desc = [f"Empresa: {empresa}"]
        if contacto:
            desc.append(f"Contacto: {contacto}")
        if caller_phone:
            desc.append(f"Teléfono: {caller_phone}")
        desc.append("Demo agendada por Lander (agente de voz de entrada).")
        payload = {
            "summary": f"Demo joblanders.ai — {empresa}",
            "description": "\n".join(desc),
            "start_iso": start_dt.isoformat(),
            "end_iso": end_dt.isoformat(),
            "attendee": (email or "").strip() or None,
        }
        res = await asyncio.to_thread(_gcal_book_demo, payload)
        if res.get("ok"):
            print(f"[DEMO] booked empresa={empresa!r} at={start_dt.isoformat()}")
            return {"booked": True, "cuando": start_dt.isoformat()}
        if res.get("reason") == "busy":
            return {"booked": False, "motivo": "ocupado", "say": "Ese hueco está ocupado; propón otra hora."}
        print(f"[DEMO] FAILED empresa={empresa!r} -> {res}")
        return {"booked": False, "say": "No he podido agendarlo ahora; ofrécele que Comercial le llame."}

    # --- Corrección: en realidad era soporte --------------------------------
    @function_tool
    async def pasar_a_soporte(self, context: RunContext) -> Agent:
        """Cambia INTERNAMENTE al modo Soporte, de forma invisible para el llamante.

        Úsala solo si, ya en Ventas, descubres que en realidad es un cliente
        existente con una duda de soporte (uso, acceso, cuenta, incidencia). NO
        anuncies el cambio ni te vuelvas a presentar: el llamante debe percibir la
        misma conversación. Se lleva la memoria de lo hablado.
        """
        if self._state.standalone:
            return (
                "Eres un agente independiente de Ventas; no cambies de modo. Ayuda "
                "en lo que puedas o pasa con una persona con transfer_to_human."
            )
        self._state.branch = "soporte"
        print("[HANDOFF] ventas -> soporte")
        return SoporteAgent(self._bundle, chat_ctx=self.chat_ctx)


class SoporteAgent(_BaseLanderAgent):
    """Modo SOPORTE: resuelve dudas de clientes existentes con hechos aprobados,
    consulta estados y, si hace falta, escala a la persona/gestor correcto.
    """

    role = "soporte"

    async def on_enter(self) -> None:
        # Modo "separate": primer contacto por su propio número; el saludo estático
        # abre la llamada (ver VentasAgent.on_enter).
        if self._state.standalone:
            return
        # Transición invisible (ver VentasAgent.on_enter). Si Recepción acaba de hablar
        # en el turno del handoff, el turno es del llamante: nos callamos y esperamos.
        if _handoff_turn_spoke(self.chat_ctx):
            return
        self.session.generate_reply()

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

    # --- Support: abrir un ticket de incidencia -----------------------------
    @function_tool
    async def create_ticket(
        self,
        context: RunContext,
        asunto: str,
        detalle: str = "",
        prioridad: str = "media",
        cliente: str = "",
        contacto: str = "",
    ) -> dict:
        """Abre un TICKET de incidencia de soporte para un cliente existente.

        Llámala cuando el cliente reporta un problema/incidencia que NO puedes
        resolver en el momento con get_support_info y hay que dejar constancia para
        que el equipo lo revise (un fallo, un error, algo no funciona, una queja).
        Una sola vez por incidencia. Confirma en voz que has abierto el ticket SOLO
        si la herramienta devuelve created=true. No la uses para dudas ya resueltas
        ni para consultar estados (usa lookup_application_status para eso).

        Args:
            asunto: Título corto de la incidencia. Obligatorio.
            detalle: Descripción de lo que falla, en español.
            prioridad: baja | media | alta | urgente (por defecto media; usa alta o
                urgente si es un bloqueo o una caída crítica).
            cliente: Nombre de la empresa/cliente, si lo tienes.
            contacto: Nombre de la persona que llama, si lo da.
        """
        asunto = (asunto or "").strip()
        if not asunto:
            return {"created": False, "say": "Necesito un asunto breve para abrir el ticket."}
        prio = (prioridad or "media").strip().lower()
        if prio not in ("baja", "media", "alta", "urgente"):
            prio = "media"
        caller_phone = getattr(self._bundle, "caller", None) or ""
        payload = {
            "cliente": (cliente or "").strip(),
            "contacto": (contacto or "").strip(),
            "phone": caller_phone,
            "asunto": asunto,
            "detalle": (detalle or "").strip(),
            "prioridad": prio,
            "source": "voice",
        }
        res = await asyncio.to_thread(_ticket_create, payload)
        if res.get("ok"):
            print(f"[TICKET] created asunto={asunto!r} prio={prio!r} phone={caller_phone!r}")
            return {"created": True, "prioridad": prio}
        print(f"[TICKET] FAILED asunto={asunto!r} -> {res}")
        return {"created": False, "say": "No he podido abrir el ticket ahora; ofrécele que le llamemos."}

    # --- Corrección: en realidad era ventas ---------------------------------
    @function_tool
    async def pasar_a_ventas(self, context: RunContext) -> Agent:
        """Cambia INTERNAMENTE al modo Ventas, de forma invisible para el llamante.

        Úsala solo si, ya en Soporte, descubres que en realidad es un cliente
        nuevo con interés comercial. NO anuncies el cambio ni te vuelvas a
        presentar. Se lleva la memoria de lo hablado.
        """
        if self._state.standalone:
            return (
                "Eres un agente independiente de Soporte; no cambies de modo. Ayuda "
                "en lo que puedas o pasa con una persona con transfer_to_human."
            )
        self._state.branch = "ventas"
        print("[HANDOFF] soporte -> ventas")
        return VentasAgent(self._bundle, chat_ctx=self.chat_ctx)


class RecepcionistaAgent(_BaseLanderAgent):
    """Modo RECEPCIÓN (maestro): saluda, triajea el motivo en 1-2 preguntas y
    cambia de modo de forma INVISIBLE a Ventas o Soporte. No responde el caso él
    mismo; enruta. Mantiene el traspaso a humano como fallback (heredado).
    """

    role = "recepcion"

    @function_tool
    async def pasar_a_ventas(self, context: RunContext) -> Agent:
        """Cambia INTERNAMENTE al modo Ventas, de forma invisible para el llamante.

        Úsala en cuanto detectes interés comercial: posible cliente nuevo, precios,
        producto, quiere contratar o ver una demo. NO digas que le pasas con nadie
        ni te vuelvas a presentar: para el llamante sigues siendo la misma persona.
        Se lleva toda la memoria de lo hablado.
        """
        self._state.branch = "ventas"
        print("[HANDOFF] recepcion -> ventas")
        return VentasAgent(self._bundle, chat_ctx=self.chat_ctx)

    @function_tool
    async def pasar_a_soporte(self, context: RunContext) -> Agent:
        """Cambia INTERNAMENTE al modo Soporte, de forma invisible para el llamante.

        Úsala en cuanto detectes soporte: cliente existente, no puede entrar, duda
        de uso, cómo funciona, cuenta, facturación, estado de una candidatura o una
        incidencia. NO digas que le pasas con nadie ni te vuelvas a presentar: para
        el llamante sigues siendo la misma persona. Se lleva toda la memoria.
        """
        self._state.branch = "soporte"
        print("[HANDOFF] recepcion -> soporte")
        return SoporteAgent(self._bundle, chat_ctx=self.chat_ctx)


def _load_role_prompts(cfg) -> dict:
    """Bloque de rol por modo: override del dashboard si existe, si no el fichero local."""
    overrides = {
        "recepcion": getattr(cfg, "prompt_recepcion", None),
        "ventas": getattr(cfg, "prompt_ventas", None),
        "soporte": getattr(cfg, "prompt_soporte", None),
    }
    out: dict[str, str] = {}
    for role, path in ROLE_PROMPT_PATHS.items():
        text = overrides.get(role)
        if not (isinstance(text, str) and text.strip()):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — a role file missing must never break the call
                text = ""
        out[role] = text
    return out


_ROLE_AGENTS = {
    "recepcion": lambda b: RecepcionistaAgent(b),
    "ventas": lambda b: VentasAgent(b),
    "soporte": lambda b: SoporteAgent(b),
}


def _dialed_number(ctx) -> str | None:
    """The DID (which of OUR numbers) the caller dialled, for separate-mode routing.

    Read best-effort from the inbound SIP participant attributes; returns None if
    unavailable (then we fall back to Recepción, i.e. unified behaviour).
    """
    try:
        room = getattr(ctx, "room", None)
        parts = list(getattr(room, "remote_participants", {}).values()) if room else []
        for pt in parts:
            attrs = getattr(pt, "attributes", {}) or {}
            for k in ("sip.trunkPhoneNumber", "sip.trunk_phone_number", "sip.dialedNumber"):
                v = attrs.get(k)
                if v:
                    return str(v)
    except Exception:  # noqa: BLE001 — never let routing detection break the call
        pass
    return None


def _starting_role(cfg, dialed: str | None) -> str:
    """Which mode the call starts in.

    "unified" (or anything unknown) always starts in Recepción. "separate" starts
    directly in the mode whose configured number matches the dialled DID (last 9
    digits); if nothing matches or it's disabled, fall back to Recepción.
    """
    if getattr(cfg, "routing_mode", "unified") != "separate":
        return "recepcion"
    if not dialed:
        return "recepcion"
    target = _last9(dialed)
    if not target:
        return "recepcion"
    for role in ("ventas", "soporte", "recepcion"):
        a = cfg.agents.get(role) or {}
        ph = a.get("phone")
        if ph and _last9(ph) == target and a.get("enabled", True):
            return role
    return "recepcion"


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

    # One Lander, three modes: build a shared bundle (base prompt + per-mode role
    # blocks + this call's context) that every mode reuses when we hand off invisibly.
    base_prompt = cfg.prompt or PROMPT_PATH.read_text(encoding="utf-8")
    role_prompts = _load_role_prompts(cfg)
    caller_context = _caller_context(client, cfg.directory)
    recall_block = _recall_block(prior_calls)
    directory_block = _directory_block(cfg.directory)

    # Enrutado: en "separate", el número marcado (DID) arranca directamente en
    # Ventas/Soporte (independiente, sin triaje). En "unified" siempre Recepción.
    dialed = _dialed_number(ctx)
    start_role = _starting_role(cfg, dialed)
    standalone = cfg.routing_mode == "separate" and start_role != "recepcion"
    agent_name = (cfg.agents.get(start_role) or {}).get("name") or "Lander"
    print(
        f"[ROUTING] mode={cfg.routing_mode} dialed={dialed!r} -> start_role={start_role} "
        f"standalone={standalone} name={agent_name!r}"
    )

    state = _CallState(standalone=standalone, branch=start_role if standalone else None)
    bundle = _Bundle(
        base_prompt=base_prompt,
        role_prompts=role_prompts,
        caller_context=caller_context,
        recall_block=recall_block,
        directory_block=directory_block,
        knowledge=cfg.knowledge,
        transfer_script=cfg.transfer_script,
        directory=cfg.directory,
        clients=cfg.clients,
        state=state,
        routing_mode=cfg.routing_mode,
        agent_name=agent_name,
        caller=caller,
    )
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
        stt=deepgram.STT(model=STT_MODEL, language=STT_LANGUAGE),
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

    # Start in the routed mode. "unified" -> Recepción (master/triage), which hands
    # off invisibly to Ventas/Soporte carrying the conversation memory (chat_ctx).
    # "separate" -> straight into Ventas/Soporte for that number (standalone).
    agent = _ROLE_AGENTS.get(start_role, _ROLE_AGENTS["recepcion"])(bundle)

    # Idioma: STT en modo code-switching (STT_LANGUAGE="multi"). Cuando el llamante
    # habla en otro idioma, cambiamos la voz clonada de James a la de ese idioma
    # (VOICE_BY_LANG: en/ca; ES usa la voz por defecto). Guardas anti-falso-cambio en
    # línea ruidosa (fue el motivo de bloquearlo antes): SOLO turnos FINALES y de
    # >= MIN_LANG_SWITCH_WORDS palabras, y solo si el idioma cambia de verdad. Si aún
    # flapea con el ruido, sube MIN_LANG_SWITCH_WORDS o pon STT_LANGUAGE="es".
    _MIN_LANG_WORDS = int(os.getenv("MIN_LANG_SWITCH_WORDS", "3"))

    @session.on("user_input_transcribed")
    def _on_user_lang(ev) -> None:  # noqa: ANN001 (livekit event payload)
        try:
            if not getattr(ev, "is_final", False):
                return
            lang = (getattr(ev, "language", None) or "").split("-")[0].lower()
            words = len((getattr(ev, "transcript", "") or "").split())
            if not lang or words < _MIN_LANG_WORDS:
                return
            target = VOICE_BY_LANG.get(lang, ELEVEN_VOICE_ID)
            if target and target != tts.voice_id:
                tts.update_options(voice_id=target)
                print(f"[LANG] caller={lang!r} words={words} -> voice={target!r}")
        except Exception as exc:  # noqa: BLE001 — nunca dejar que el swap rompa la llamada
            print(f"[LANG] swap error: {exc!r}")

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

    # Per-turn latency: emit one `[LATENCY]` JSON line per metrics event so a log
    # parser (evals/latency.py) can report p50/p95 per phase and flag the transfer
    # whisper turn. Print-only, fully guarded — never touches the call flow.
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:  # noqa: ANN001 (livekit event payload)
        try:
            m = ev.metrics
            fields = {
                k: round(v, 4)
                for k in ("end_of_utterance_delay", "transcription_delay",
                          "ttft", "ttfb", "duration")
                if isinstance((v := getattr(m, k, None)), (int, float))
            }
            if fields:
                print("[LATENCY] " + json.dumps(
                    {"kind": type(m).__name__, **fields}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 — metrics are a nice-to-have, never break the call
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
            branch=state.branch,
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
            f"Hola, soy {agent_name}, el asistente virtual con inteligencia artificial de "
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
