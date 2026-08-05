#!/usr/bin/env python3
"""No-phone ACTION harness for Lander (voice-agent brain).

Like evals/run.py, it reconstructs the agent's real brain (same system_es.md base +
per-mode role blocks + real tool schemas, model claude-haiku-4-5) and follows the
invisible pasar_a_* handoffs. The difference: this harness exposes the ACTION tools
(log_lead, book_demo, create_ticket) and, when the brain decides to call them,
executes the REAL handlers imported from agent.py — so rows land in the real
crm_companies / landeros_tickets tables. The agent decides; we just run it.

Google Calendar: if GOOGLE_SA_JSON_B64 is present the demo is booked for real;
otherwise the booking intent is collected and printed as BOOKINGS_JSON for the
caller (Claude) to create via the Calendar MCP. Every synthetic entity is prefixed
"TEST ·" so it can be bulk-cleaned afterward.

Run:  .venv/bin/python evals/run_actions.py
Creds: ANTHROPIC_API_KEY (agent .env) + Supabase URL/key (dashboard .env.local).
"""
import json, os, sys, urllib.request, urllib.error

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH_DIR = os.getenv("DASHBOARD_DIR", os.path.join(os.path.dirname(AGENT_DIR), "joblanders-dashboard"))
sys.path.insert(0, AGENT_DIR)


def load_env(path, keys):
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k in keys:
                out[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


env = load_env(os.path.join(AGENT_DIR, ".env"), {"ANTHROPIC_API_KEY"})
denv = load_env(os.path.join(DASH_DIR, ".env.local"),
                {"NEXT_PUBLIC_SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"})
AK = os.getenv("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
SB_URL = os.getenv("SUPABASE_URL") or denv.get("NEXT_PUBLIC_SUPABASE_URL")
SB_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or denv.get("SUPABASE_SERVICE_ROLE_KEY")
if not (AK and SB_URL and SB_KEY):
    sys.exit("Missing creds: ANTHROPIC_API_KEY (agent .env) + Supabase URL/key (dashboard .env.local).")
# config.py (imported by agent.py) reads these at import time — set BEFORE importing agent.
os.environ.setdefault("SUPABASE_URL", SB_URL)
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", SB_KEY)

import agent as A  # real helpers: _crm_log_lead, _ticket_create, _gcal_book_demo

BRAIN_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")


def sb_get(key):
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/landeros_config?key=eq.{key}&select=value",
        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
    data = json.load(urllib.request.urlopen(req))
    return data[0]["value"] if data else None


directory = (sb_get("directory") or {}).get("people", [])
clients = (sb_get("clients") or {}).get("list", [])


def person_dept(person, directory):
    for d in directory:
        if str(d.get("person", "")).strip().lower() == (person or "").strip().lower():
            return d.get("department")
    return None


def caller_context(client, directory):
    if client:
        bits = [f"nombre {client['name']}" if client.get("name") else "",
                f"empresa {client['company']}" if client.get("company") else "",
                f"plan {client['plan']}" if client.get("plan") else "",
                f"estado {client['status']}" if client.get("status") else ""]
        detail = ", ".join(b for b in bits if b) or "sin más datos"
        am = str(client.get("account_manager") or "").strip()
        if am:
            dept = person_dept(am, directory)
            dept_txt = f" (departamento {dept})" if dept else ""
            am_line = (f" Su gestor de cuenta asignado es {am}{dept_txt}: es a quien debes pasarle "
                       "por defecto, salvo que el motivo sea claramente de otro departamento.")
        else:
            am_line = (" No tiene gestor asignado: si hay que pasarle y el motivo no encaja claro, "
                       "usa el departamento «General» (Recepción).")
        return ("LLAMANTE: CLIENTE EXISTENTE reconocido por su número — "
                f"{detail}.{am_line} No vendas: ya es cliente. Entiende POR QUÉ llama y pásale.")
    return ("LLAMANTE: DESCONOCIDO (no está en la base por su número, o vino oculto). Trátalo como "
            "posible cliente NUEVO: descubre necesidad y vende. Pero si dice que ya trabaja con "
            "Joblanders, pídele su email y usa identify_by_email.")


def directory_block(directory):
    if not directory:
        return ""
    lines = ["DIRECTORIO DE TRANSFERENCIA (elige department + person según el motivo):"]
    for d in directory:
        kws = d.get("keywords") or []
        kw = ", ".join(str(k) for k in kws) if isinstance(kws, list) else str(kws)
        lines.append(f"- {d.get('department')} / {d.get('person')} — motivos: {kw}")
    return "\n".join(lines)


def _now_ctx():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    rows = [f"- {dias[(now + timedelta(days=i)).weekday()]} "
            f"{(now + timedelta(days=i)).day} de {meses[(now + timedelta(days=i)).month]} "
            f"= {(now + timedelta(days=i)):%Y-%m-%d}" for i in range(1, 15)]
    return (f"CONTEXTO TEMPORAL: hoy es {dias[now.weekday()]} {now.day} de "
            f"{meses[now.month]} de {now.year} ({now:%Y-%m-%d}).\n"
            "FECHAS EXACTAS — al agendar usa SIEMPRE la fecha de esta tabla, NUNCA "
            "calcules el día de la semana tú:\n" + "\n".join(rows) + "\n"
            "Si dice solo un día ('el martes'), usa el PRIMERO de la tabla (el más próximo).\n"
            "APERTURA YA DADA: al descolgar ya has dicho por voz 'Hola, soy Lander, el "
            "asistente virtual con inteligencia artificial de Joblanders. ¿En qué puedo "
            "ayudarte?'. NO vuelvas a saludar ni a presentarte; responde/enruta directo.")


NOW_CTX = _now_ctx()
BASE = open(os.path.join(AGENT_DIR, "prompts/system_es.md"), encoding="utf-8").read()
ROLE = {m: open(os.path.join(AGENT_DIR, f"prompts/role_{m}_es.md"), encoding="utf-8").read()
        for m in ("recepcion", "ventas", "soporte")}

try:
    from knowledge import get_product_info as kb_product, get_support_info as kb_support, \
        lookup_application_status as kb_lookup
    KB = True
except Exception as e:
    KB = False
    print(f"[warn] knowledge.py import failed ({e!r})")

_T = {
    "identify_by_email": {"name": "identify_by_email", "description": "Identify an existing client by email when the number wasn't recognised.",
        "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}},
    "transfer_to_human": {"name": "transfer_to_human", "description": "Transfer/escalate the caller to a real human from the directory (last-resort fallback).",
        "input_schema": {"type": "object", "properties": {"department": {"type": "string"}, "person": {"type": "string"}, "summary": {"type": "string"}}, "required": ["department", "person", "summary"]}},
    "get_product_info": {"name": "get_product_info", "description": "Factual product/plan info you may quote (prices, plans, how matching works).",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    "get_support_info": {"name": "get_support_info", "description": "Factual support info for an existing client (how it works, common issues, policies).",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    "lookup_application_status": {"name": "lookup_application_status", "description": "Look up status of a candidate application or account.",
        "input_schema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    "log_lead": {"name": "log_lead", "description": "Registra un lead CUALIFICADO en el CRM (empresa nueva interesada). Solo con nombre de empresa e interés real.",
        "input_schema": {"type": "object", "properties": {"empresa": {"type": "string"}, "contacto": {"type": "string"}, "necesidad": {"type": "string"}}, "required": ["empresa"]}},
    "book_demo": {"name": "book_demo", "description": "Agenda una DEMO en el calendario de Comercial. Necesita empresa y una fecha/hora ISO local concreta; pide email para la invitación.",
        "input_schema": {"type": "object", "properties": {"empresa": {"type": "string"}, "fecha_hora_iso": {"type": "string"}, "email": {"type": "string"}, "contacto": {"type": "string"}, "duracion_min": {"type": "integer"}}, "required": ["empresa", "fecha_hora_iso"]}},
    "create_ticket": {"name": "create_ticket", "description": "Abre un TICKET de incidencia de soporte para un cliente existente cuando reporta un problema que no se resuelve en el momento.",
        "input_schema": {"type": "object", "properties": {"asunto": {"type": "string"}, "detalle": {"type": "string"}, "prioridad": {"type": "string"}, "cliente": {"type": "string"}, "contacto": {"type": "string"}}, "required": ["asunto"]}},
    "pasar_a_ventas": {"name": "pasar_a_ventas", "description": "Switch INTERNALLY to Ventas mode, invisibly (same voice). Carries memory.",
        "input_schema": {"type": "object", "properties": {}, "required": []}},
    "pasar_a_soporte": {"name": "pasar_a_soporte", "description": "Switch INTERNALLY to Soporte mode, invisibly (same voice). Carries memory.",
        "input_schema": {"type": "object", "properties": {}, "required": []}},
}
MODE_TOOLS = {
    "recepcion": [_T["identify_by_email"], _T["transfer_to_human"], _T["pasar_a_ventas"], _T["pasar_a_soporte"]],
    "ventas": [_T["identify_by_email"], _T["transfer_to_human"], _T["get_product_info"], _T["log_lead"], _T["book_demo"], _T["pasar_a_soporte"]],
    "soporte": [_T["identify_by_email"], _T["transfer_to_human"], _T["get_support_info"], _T["lookup_application_status"], _T["create_ticket"], _T["pasar_a_ventas"]],
}

# Side-effect ledger for this run.
LEADS, TICKETS, BOOKINGS = [], [], []


def _do_log_lead(inp, caller_phone):
    empresa = (inp.get("empresa") or "").strip()
    if not empresa:
        return {"logged": False, "say": "Pídele el nombre de su empresa."}
    payload = {"name": empresa, "business": "lab", "source": "voice", "relationship": "prospecto",
               "stage": "Contactado", "contact_name": (inp.get("contacto") or "").strip(),
               "contact_phone": caller_phone, "notes": (inp.get("necesidad") or "").strip(),
               "next_action": "TEST · lead automatizado (harness) — retomar desde Comercial"}
    res = A._crm_log_lead(payload)
    if res.get("ok"):
        LEADS.append({"empresa": empresa, "contacto": payload["contact_name"], "necesidad": payload["notes"]})
        return {"logged": True}
    return {"logged": False, "reason": res}


def _do_create_ticket(inp, caller_phone):
    asunto = (inp.get("asunto") or "").strip()
    if not asunto:
        return {"created": False, "say": "Necesito un asunto."}
    prio = (inp.get("prioridad") or "media").strip().lower()
    if prio not in ("baja", "media", "alta", "urgente"):
        prio = "media"
    payload = {"cliente": (inp.get("cliente") or "").strip(), "contacto": (inp.get("contacto") or "").strip(),
               "phone": caller_phone, "asunto": asunto, "detalle": (inp.get("detalle") or "").strip(),
               "prioridad": prio, "source": "voice"}
    res = A._ticket_create(payload)
    if res.get("ok"):
        TICKETS.append({"asunto": asunto, "prioridad": prio, "cliente": payload["cliente"]})
        return {"created": True, "prioridad": prio}
    return {"created": False, "reason": res}


def _do_book_demo(inp, caller_phone):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    empresa = (inp.get("empresa") or "").strip()
    start_raw = (inp.get("fecha_hora_iso") or "").strip()
    if not empresa or not start_raw:
        return {"booked": False, "say": "Necesito empresa y una fecha y hora concreta."}
    try:
        start_dt = datetime.fromisoformat(start_raw)
    except ValueError:
        return {"booked": False, "say": "No entendí la fecha; pídela con día y hora."}
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=ZoneInfo("Europe/Madrid"))
    dur = max(15, int(inp.get("duracion_min") or 30))
    end_dt = start_dt + timedelta(minutes=dur)
    payload = {"summary": f"TEST · Demo joblanders.ai — {empresa}",
               "description": f"Empresa: {empresa}\nContacto: {inp.get('contacto','')}\nTeléfono: {caller_phone}\nTEST · demo agendada por harness.",
               "start_iso": start_dt.isoformat(), "end_iso": end_dt.isoformat(),
               "attendee": (inp.get("email") or "").strip() or None}
    res = A._gcal_book_demo(payload)
    booking = {"empresa": empresa, "contacto": inp.get("contacto", ""), "email": inp.get("email", ""),
               "start_iso": start_dt.isoformat(), "end_iso": end_dt.isoformat(),
               "summary": payload["summary"], "description": payload["description"]}
    if res.get("ok"):
        booking["via"] = "gcal_sa"
        BOOKINGS.append(booking)
        return {"booked": True, "cuando": start_dt.isoformat()}
    if res.get("reason") == "busy":
        return {"booked": False, "motivo": "ocupado", "say": "Ese hueco está ocupado; propón otra hora."}
    # No local SA creds -> bridge: record intent, tell the brain it's booked so the convo closes.
    booking["via"] = "mcp_bridge"
    BOOKINGS.append(booking)
    return {"booked": True, "cuando": start_dt.isoformat()}


def run_tool(name, inp, caller_phone):
    if name in ("pasar_a_ventas", "pasar_a_soporte"):
        return {"_handoff": name.replace("pasar_a_", ""), "result": "(cambio de modo hecho; sigue sin volver a saludar)"}
    if name == "transfer_to_human":
        return {"_transfer": inp, "result": "(traspaso iniciado; te retiras)"}
    if name == "identify_by_email":
        key = (inp.get("email") or "").strip().lower()
        for c in clients:
            if str(c.get("email", "")).strip().lower() == key and key:
                return {"found": True, "company": c.get("company")}
        return {"found": False, "say": "No encuentro ese email; trátalo como cliente nuevo."}
    if name == "get_product_info":
        return kb_product(inp.get("topic", "")) if KB else {"found": False}
    if name == "get_support_info":
        return kb_support(inp.get("topic", "")) if KB else {"found": False}
    if name == "lookup_application_status":
        return kb_lookup(inp.get("reference", "")) if KB else {"found": False}
    if name == "log_lead":
        return _do_log_lead(inp, caller_phone)
    if name == "book_demo":
        return _do_book_demo(inp, caller_phone)
    if name == "create_ticket":
        return _do_create_ticket(inp, caller_phone)
    return {"error": "unknown tool"}


def anthropic_call(model, system, messages, tools=None, max_tokens=700):
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _system_for(mode, client):
    parts = [BASE, ROLE[mode], NOW_CTX, caller_context(client, directory), directory_block(directory)]
    return "\n\n".join(p for p in parts if p and p.strip())


def run_convo(sc):
    """Drive one scripted conversation. Returns (tools_fired, transcript).

    Models the LIVE on_enter gating: if the handing-off mode (Recepción) SPOKE on the
    handoff turn (e.g. asked a qualifying question) the destination mode stays SILENT and
    the floor goes back to the caller — so the pending tool_result(s) are held and folded
    into the NEXT caller turn's user message (as [tool_result..., {text}]). A SILENT handoff
    (tool only, no speech) auto-replies in the destination mode, as before (dead-air avoid).
    """
    client = sc.get("client")           # dict -> existing caller; None -> unknown/new
    mode = "recepcion"
    caller_phone = sc.get("phone", "+34600000000")
    messages, transcript, tools_fired = [], [], []
    pending = None   # tool_result blocks held for the next caller turn (spoke+handoff case)
    for turn in sc["turns"]:
        # A spoke+handoff on the previous turn left tool_results pending: the destination
        # mode was silent, waiting for the caller. Fold them in front of this caller turn.
        if pending:
            messages.append({"role": "user",
                             "content": pending + [{"type": "text", "text": turn}]})
            pending = None
        else:
            messages.append({"role": "user", "content": turn})
        transcript.append(f"CALLER: {turn}")
        for _ in range(6):
            resp = anthropic_call(BRAIN_MODEL, _system_for(mode, client), messages, tools=MODE_TOOLS[mode])
            content = resp.get("content", [])
            messages.append({"role": "assistant", "content": content})
            tool_results, spoke, handed_off = [], False, False
            for block in content:
                if block.get("type") == "text" and block["text"].strip():
                    transcript.append("LANDER: " + block["text"].strip())
                    spoke = True
                elif block.get("type") == "tool_use":
                    tools_fired.append(block["name"])
                    out = run_tool(block["name"], block.get("input", {}), caller_phone)
                    if isinstance(out, dict) and out.get("_handoff"):
                        mode = out["_handoff"]
                        handed_off = True
                        transcript.append(f"[MODO -> {mode}]")
                    else:
                        transcript.append(f"[TOOL {block['name']}({json.dumps(block.get('input', {}), ensure_ascii=False)}) -> {json.dumps({k: v for k, v in out.items() if not k.startswith('_')}, ensure_ascii=False)}]")
                    tool_results.append({"type": "tool_result", "tool_use_id": block["id"],
                                         "content": json.dumps(out, ensure_ascii=False)})
            if resp.get("stop_reason") == "tool_use" and tool_results:
                # Spoke + handed off in the same turn -> destination mode stays SILENT,
                # floor to caller. Hold tool_results for the next caller turn.
                if handed_off and spoke:
                    transcript.append("[DESTINO EN SILENCIO — espera respuesta del llamante]")
                    pending = tool_results
                    break
                messages.append({"role": "user", "content": tool_results})
                continue
            break
        if "transfer_to_human" in tools_fired and sc.get("cat") != "ticket":
            break
    return tools_fired, transcript


# ---------------------------------------------------------------------------
# 30 scenarios: 10 ventas (lead), 10 agenda (demo), 10 ticket. Different data each.
def _client(name, company):
    return {"name": name, "company": company, "plan": "Pro", "status": "activo",
            "account_manager": (directory[0]["person"] if directory else "")}

VENTAS = [
    ("TEST · Voxtel Staffing", "Marina Colomer", "ETT 50 personas, quiere automatizar el screening de candidatos"),
    ("TEST · Brío RRHH", "Álvaro Nieto", "consultora de selección, 20 recruiters, busca agentes de IA para criba"),
    ("TEST · Talentia Global", "Nuria Sáez", "multinacional, alto volumen de vacantes, quiere IA para primer filtro"),
    ("TEST · Empléate Ya", "Diego Ferrer", "portal de empleo regional, quiere un agente de voz para candidatos"),
    ("TEST · Recluta Norte", "Iratxe Bilbao", "ETT industrial, 15 vacantes/mes, interesada en la suite de agentes"),
    ("TEST · HR Partners SL", "Sergio Márquez", "outsourcing de RRHH, quiere entender qué automatiza joblanders.ai"),
    ("TEST · Selecta Talent", "Paula Rincón", "headhunting tech, quiere agente que preentreviste candidatos"),
    ("TEST · Andes Staffing", "Tomás Vidal", "staffing logística, mucho volumen estacional, pide info comercial"),
    ("TEST · Nexo Empleo", "Carla Osuna", "agencia de empleo, quiere reducir tiempo de criba con IA"),
    ("TEST · GrupoLabora", "Hugo Prat", "grupo de 3 ETTs, evalúa proveedores de IA para reclutamiento"),
]
AGENDA_SLOTS = ["2026-08-04T16:00:00", "2026-08-04T17:30:00", "2026-08-05T16:00:00", "2026-08-05T17:30:00",
                "2026-08-06T16:00:00", "2026-08-06T17:30:00", "2026-08-07T16:00:00", "2026-08-07T17:30:00",
                "2026-08-10T16:00:00", "2026-08-10T17:30:00"]
AGENDA_HUMAN = ["el martes a las cuatro de la tarde", "el martes a las cinco y media", "el miércoles a las cuatro",
                "el miércoles a las cinco y media", "el jueves a las cuatro", "el jueves a las cinco y media",
                "el viernes a las cuatro", "el viernes a las cinco y media", "el lunes que viene a las cuatro",
                "el lunes que viene a las cinco y media"]
AGENDA = [
    ("TEST · Cumbre Selección", "Elena Rís", "elena@cumbretest.example"),
    ("TEST · Ágora Talent", "Marc Solà", "marc@agoratest.example"),
    ("TEST · Delta Recruit", "Sonia Vega", "sonia@deltatest.example"),
    ("TEST · Orbe RRHH", "Pau Miralles", "pau@orbetest.example"),
    ("TEST · Zenit Staffing", "Lucía Bravo", "lucia@zenittest.example"),
    ("TEST · Faro Empleo", "Nacho Peña", "nacho@farotest.example"),
    ("TEST · Vértice Talent", "Rita Colom", "rita@verticetest.example"),
    ("TEST · Prisma RRHH", "Gorka Ruiz", "gorka@prismatest.example"),
    ("TEST · Aurora Recruit", "Vera Lima", "vera@auroratest.example"),
    ("TEST · Meridian Staffing", "Bruno Gil", "bruno@meridiantest.example"),
]
TICKETS_SC = [
    ("TEST · Acme Logística", "Laura Gómez", "TEST · No puedo entrar a la plataforma", "Me da error de login desde ayer, credenciales correctas.", "alta"),
    ("TEST · Feroli Group", "Andrés Mota", "TEST · No recibo los candidatos por email", "Las notificaciones de nuevos candidatos no llegan.", "media"),
    ("TEST · Distribuciones Sur", "Pilar Nadal", "TEST · El agente de voz se cuelga", "Al lanzar una llamada el agente se queda mudo a mitad.", "alta"),
    ("TEST · TecnoBar SL", "Raúl Ibáñez", "TEST · Error al exportar informe", "El CSV de candidatos sale vacío al exportar.", "media"),
    ("TEST · Hostal Marina", "Cristina Vera", "TEST · No puedo cambiar mi plan", "Intento subir de plan y da error 500.", "media"),
    ("TEST · Clima Norte", "Jon Aguirre", "TEST · Caída total del servicio", "Nada carga, la plataforma está caída para todo el equipo.", "urgente"),
    ("TEST · Panadería Rú", "Marta Coll", "TEST · Candidato duplicado", "Un candidato aparece dos veces y no puedo borrarlo.", "baja"),
    ("TEST · AutoPiezas SA", "Fer Delgado", "TEST · La integración con mi ATS falló", "La sincronización con nuestro ATS dejó de funcionar hoy.", "alta"),
    ("TEST · EduForma", "Alba Ruano", "TEST · Reclamación por cobro", "Me habéis cobrado dos veces este mes, quiero que se revise.", "alta"),
    ("TEST · Verde Jardín", "Iván Sanz", "TEST · No suena el agente al llamar", "Los candidatos dicen que llaman y no contesta nadie.", "urgente"),
]


def build_scenarios():
    scs = []
    for empresa, contacto, necesidad in VENTAS:
        scs.append({"cat": "ventas", "empresa": empresa, "client": None,
                    "turns": [f"Hola, soy {contacto}, de {empresa}. {necesidad.capitalize()}. ¿Me podéis mandar información y que me llame alguien de comercial?",
                              f"Sí, la empresa es {empresa} y soy {contacto}. De momento solo quiero info, aún no me va bien agendar nada."]})
    for (empresa, contacto, email), slot, human in zip(AGENDA, AGENDA_SLOTS, AGENDA_HUMAN):
        scs.append({"cat": "agenda", "empresa": empresa, "client": None,
                    "turns": [f"Hola, soy {contacto}, de {empresa}. Nos interesa joblanders.ai y queremos ver una demo.",
                              f"¿Podría ser {human}? Mi correo es {email}. La empresa es {empresa}.",
                              "Perfecto, confírmame ese hueco."]})
    for empresa, contacto, asunto, detalle, prioridad in TICKETS_SC:
        scs.append({"cat": "ticket", "empresa": empresa, "client": _client(contacto, empresa),
                    "turns": [f"Hola, soy {contacto}, de {empresa}, ya somos clientes. Tengo una incidencia: {detalle}",
                              f"Sí, es urgente para nosotros. Ábreme un ticket por favor. El asunto es: {asunto}.",
                              "Vale, gracias, dejadlo registrado."]})
    return scs


def main():
    scenarios = build_scenarios()
    print(f"brain={BRAIN_MODEL}  dir={len(directory)} clients={len(clients)}  scenarios={len(scenarios)}\n")
    rows = []
    for i, sc in enumerate(scenarios, 1):
        tools, transcript = run_convo(sc)
        target = {"ventas": "log_lead", "agenda": "book_demo", "ticket": "create_ticket"}[sc["cat"]]
        hit = target in tools
        rows.append({"n": i, "cat": sc["cat"], "empresa": sc["empresa"], "target": target,
                     "hit": hit, "tools": tools})
        print(f"[{i:02d}/{len(scenarios)}] {sc['cat']:<7} {sc['empresa']:<26} target={target:<13} "
              f"{'OK' if hit else 'MISS'}  tools={tools}")
    print("\n=== SUMMARY ===")
    for cat, tgt in (("ventas", "log_lead"), ("agenda", "book_demo"), ("ticket", "create_ticket")):
        got = sum(1 for r in rows if r["cat"] == cat and r["hit"])
        tot = sum(1 for r in rows if r["cat"] == cat)
        print(f"{cat:<7} {tgt:<13} {got}/{tot} disparado")
    print(f"\nLEADS creados: {len(LEADS)} | TICKETS creados: {len(TICKETS)} | BOOKINGS: {len(BOOKINGS)}")
    # Emit bookings for the MCP bridge (Calendar creation done by the caller).
    print("\nBOOKINGS_JSON:")
    print(json.dumps(BOOKINGS, ensure_ascii=False))


if __name__ == "__main__":
    main()
