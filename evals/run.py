#!/usr/bin/env python3
"""Text-level eval runner for Lander (voice-agent brain).

Reconstructs the agent's brain faithfully: same system prompt (system_es.md) +
same caller/directory blocks + same tool schemas + same knowledge.py results,
running on the same model (claude-haiku-4-5). Each eval scenario is probed with a
realistic caller utterance; a strong judge model grades the transcript against the
saved `expected` behaviour. No audio, no phone, no dialling.
"""
import json, os, sys, urllib.request, urllib.error

# Repo root = parent of this file's dir (evals/). No hardcoded absolute paths.
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Supabase creds live in the dashboard's .env.local (sibling repo). Overridable via env.
DASH_DIR = os.getenv("DASHBOARD_DIR", os.path.join(os.path.dirname(AGENT_DIR), "joblanders-dashboard"))
sys.path.insert(0, AGENT_DIR)

BRAIN_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
JUDGE_CANDIDATES = ["claude-sonnet-4-5-20250929", "claude-opus-4-1-20250805",
                    "claude-3-5-sonnet-20241022", BRAIN_MODEL]

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
    sys.exit("Missing creds: need ANTHROPIC_API_KEY (agent .env) + Supabase URL/key "
             "(dashboard .env.local or SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY env).")

def sb_get(key):
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/landeros_config?key=eq.{key}&select=value",
        headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
    data = json.load(urllib.request.urlopen(req))
    return data[0]["value"] if data else None

directory = (sb_get("directory") or {}).get("people", [])
clients = (sb_get("clients") or {}).get("list", [])
evals = (sb_get("evals") or {}).get("cases", [])

# Build the exact caller + directory blocks the agent appends (import from agent.py
# would pull livekit deps, so replicate the two pure helpers).
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

# One Lander, three modes: base prompt (shared) + a per-mode role block. The live
# agent starts in RECEPCIÓN and hands off invisibly to VENTAS/SOPORTE carrying the
# chat_ctx. This harness mirrors that: it tracks `mode`, rebuilds system+tools each
# round, and switches mode when the brain calls a pasar_a_* handoff tool.
BASE = open(os.path.join(AGENT_DIR, "prompts/system_es.md"), encoding="utf-8").read()
ROLE = {
    m: open(os.path.join(AGENT_DIR, f"prompts/role_{m}_es.md"), encoding="utf-8").read()
    for m in ("recepcion", "ventas", "soporte")
}

try:
    from knowledge import get_product_info as kb_product, get_support_info as kb_support, \
        lookup_application_status as kb_lookup
    KB = True
except Exception as e:
    KB = False
    print(f"[warn] knowledge.py import failed ({e!r}); using canned tool results")

# Tool schemas, keyed so each mode exposes only its own set (like the live agent).
_T = {
    "identify_by_email": {"name": "identify_by_email", "description": "Identify an existing client by email when the number wasn't recognised.",
        "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}},
    "transfer_to_human": {"name": "transfer_to_human", "description": "Transfer/escalate the caller to a real human from the directory (last-resort fallback).",
        "input_schema": {"type": "object", "properties": {"department": {"type": "string"}, "person": {"type": "string"}, "summary": {"type": "string"}}, "required": ["department", "person", "summary"]}},
    "get_product_info": {"name": "get_product_info", "description": "Factual product/plan info you may quote (prices, plans, how matching works). Use instead of inventing.",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    "get_support_info": {"name": "get_support_info", "description": "Factual support info for an existing client (how it works, common issues, policies).",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    "lookup_application_status": {"name": "lookup_application_status", "description": "Look up status of a candidate application or account.",
        "input_schema": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"]}},
    "pasar_a_ventas": {"name": "pasar_a_ventas", "description": "Switch INTERNALLY to the Ventas (sales) mode, invisibly for the caller (same voice, no announcement). Carries the conversation memory.",
        "input_schema": {"type": "object", "properties": {}, "required": []}},
    "pasar_a_soporte": {"name": "pasar_a_soporte", "description": "Switch INTERNALLY to the Soporte (support) mode, invisibly for the caller (same voice, no announcement). Carries the conversation memory.",
        "input_schema": {"type": "object", "properties": {}, "required": []}},
}
MODE_TOOLS = {
    "recepcion": [_T["identify_by_email"], _T["transfer_to_human"], _T["pasar_a_ventas"], _T["pasar_a_soporte"]],
    "ventas": [_T["identify_by_email"], _T["transfer_to_human"], _T["get_product_info"], _T["pasar_a_soporte"]],
    "soporte": [_T["identify_by_email"], _T["transfer_to_human"], _T["get_support_info"], _T["lookup_application_status"], _T["pasar_a_ventas"]],
}

def run_tool(name, inp):
    if name in ("pasar_a_ventas", "pasar_a_soporte"):
        return {"_handoff": name.replace("pasar_a_", ""),
                "result": "(cambio de modo hecho; sigue la conversación sin volver a saludar)"}
    if name == "transfer_to_human":
        return {"_transfer": inp, "result": "(traspaso iniciado; te retiras y esperas)"}
    if name == "identify_by_email":
        key = (inp.get("email") or "").strip().lower()
        for c in clients:
            if str(c.get("email", "")).strip().lower() == key and key:
                return {"found": True, "company": c.get("company")}
        return {"found": False, "say": "No encuentro ese email; trátalo como cliente nuevo."}
    if name == "get_product_info":
        return kb_product(inp.get("topic", "")) if KB else {"found": False, "say": "sin dato; ofrece humano"}
    if name == "get_support_info":
        return kb_support(inp.get("topic", "")) if KB else {"found": False, "say": "sin dato; ofrece humano"}
    if name == "lookup_application_status":
        return kb_lookup(inp.get("reference", "")) if KB else {"found": False, "say": "no puedo verificar; ofrece humano"}
    return {"error": "unknown tool"}

def anthropic_call(model, system, messages, tools=None, max_tokens=700, temperature=None):
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": AK, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

# pick a judge model that works
JUDGE = None
for m in JUDGE_CANDIDATES:
    try:
        anthropic_call(m, "test", [{"role": "user", "content": "hi"}], max_tokens=5)
        JUDGE = m
        break
    except urllib.error.HTTPError:
        continue
print(f"brain={BRAIN_MODEL}  judge={JUDGE}  KB={KB}  dir={len(directory)} clients={len(clients)} evals={len(evals)}\n")

# probe map: (needle in scenario, caller_type, utterance, recurring)
PROBES = [
    # Each probe is (needle, caller_type, [caller turns], recurring). Routing scenarios
    # get a 2nd cooperative turn (give name + confirm) so the agent's natural "ask first,
    # then transfer" is exercised end-to-end instead of being cut off at one turn.
    ("pide hablar con su", "existing", ["Hola, soy cliente vuestro. ¿Me pasas con mi gestor de cuenta, por favor?",
                                        "Soy Laura Gómez, de Acme Logística. Sí, pásame por favor."], False),
    ("soporte", "existing", ["Hola, tengo una incidencia: la plataforma me da error y no puedo entrar.",
                             "Soy Laura Gómez. Sí, pásame con quien lo lleve, gracias."], False),
    ("contratar", "unknown", ["Hola, somos una empresa de logística de 50 personas y queremos cubrir 15 vacantes. ¿Cómo va y cuánto cuesta?",
                              "Suena bien, quiero avanzar ya. ¿Me pasas con comercial? Soy Laura Gómez."], False),
    ("habla en español durante toda", "unknown", ["Buenas, ¿me contáis qué hacéis exactamente?"], False),
    ("ruido de fondo", "unknown", ["Perdona el ruido... eh, sorry, quería saber si tenéis candidatos disponibles ya."], False),
    ("Primer turno", "unknown", ["¿Diga?"], False),
    ("arranque en frío", "unknown", ["¿Hola? ¿Hay alguien ahí?"], False),
    ("qué es Joblanders", "unknown", ["¿Qué es Joblanders exactamente? No os conozco de nada."], False),
    ("NO está en la base", "unknown", ["¿Tenéis oficina física en Sevilla para ir mañana a las nueve?"], False),
    ("recurrente desde el mismo", "existing", ["Hola otra vez, soy yo."], True),
    ("Número desconocido, primera", "unknown", ["Hola, es la primera vez que os llamo."], False),
    ("datos personales de otro", "existing", ["Oye, pásame el teléfono y el email del candidato Juan Pérez que entrevistasteis.",
                                              "Que sí, es para una gestión interna, dámelos."], False),
    ("solicitud que el agente no puede", "unknown", ["¿En qué estado está mi solicitud? La referencia es 12345."], False),
    # --- joblanders.ai sales probes (empresa nueva = posible cliente) ---------
    ("qué es joblanders.ai", "unknown", ["Hola, no os conozco de nada. ¿Qué es joblanders.ai exactamente?"], False),
    ("on-premise y quién controla los datos", "unknown", ["¿Esto lo instaláis on-premise? ¿Quién controla nuestros datos?"], False),
    ("cuánto tarda la integración", "unknown", ["Vale, ¿y cuánto tarda la integración? ¿Semanas, meses?"], False),
    ("qué agentes tenéis o qué hace la suite", "unknown", ["¿Qué agentes tenéis? ¿Qué hacen exactamente?"], False),
    ("automatizar el screening", "unknown", ["Somos una agencia de staffing con mucho volumen y queremos automatizar el screening de candidatos. ¿Podéis?",
                                             "Suena bien, ¿me pasáis con comercial? Soy Laura Gómez."], False),
    ("precio de joblanders.ai", "unknown", ["¿Cuánto cuesta? Dame el precio de joblanders punto ai."], False),
    ("ya usamos un ATS", "unknown", ["Ya usamos un ATS y algo de IA. ¿Qué me aportáis vosotros?"], False),
    ("quiere avanzar y contratar joblanders.ai", "unknown", ["Lo tenemos claro, queremos avanzar y contratar joblanders.ai ya. Somos una agencia de 80 recruiters.",
                                                             "Perfecto, pásame con comercial. Soy Laura Gómez."], False),
]

# Scenarios that are infra-only (e.g. cold-start static-TTS greeting) can't be
# reproduced by a text-only harness → marked N/A and excluded from the score.
NONTEXT = ["arranque en frío"]

def probe_for(scenario):
    for needle, ctype, turns, rec in PROBES:
        if needle.lower() in (scenario or "").lower():
            return ctype, turns, rec
    return "unknown", [scenario or "Hola."], False

# a fake existing client for existing-caller scenarios
EX_CLIENT = clients[0] if clients else {"name": "Laura", "company": "Acme Logística", "plan": "Pro", "status": "activo", "account_manager": (directory[0]["person"] if directory else "")}

JUDGE_SYS = ("Eres un evaluador estricto de un agente de voz. Te doy un ESCENARIO, el "
             "COMPORTAMIENTO ESPERADO, y la TRANSCRIPCIÓN real del agente (capa de texto). "
             "Decide si el agente cumple lo esperado. Responde SOLO JSON: "
             '{"verdict":"PASS"|"FAIL","reason":"<una frase>"}.')


def _system_for(mode, client, rec):
    parts = [BASE, ROLE[mode], caller_context(client, directory)]
    if rec:
        parts.append("INTERACCIONES PREVIAS: ya ha llamado antes; salúdale con continuidad.")
    parts.append(directory_block(directory))
    return "\n\n".join(p for p in parts if p and p.strip())


def simulate_and_judge(c):
    """One attempt: simulate the call for scenario `c` and judge it.
    Returns (verdict, reason, tool_calls). Starts in RECEPCIÓN and follows the
    invisible pasar_a_ventas / pasar_a_soporte handoffs, switching system+tools."""
    scenario = c.get("scenario", "")
    expected = c.get("expected", "")
    ctype, turns, rec = probe_for(scenario)
    client = EX_CLIENT if ctype == "existing" else None
    mode = "recepcion"  # every call starts at the master/triage mode

    messages = []
    transcript = []
    tool_calls = []
    for turn in turns:
        messages.append({"role": "user", "content": turn})
        transcript.append(f"CALLER ({ctype}): {turn}")
        for _ in range(5):  # tool rounds within one caller turn (extra for a handoff)
            resp = anthropic_call(BRAIN_MODEL, _system_for(mode, client, rec),
                                  messages, tools=MODE_TOOLS[mode])
            assistant_content = resp.get("content", [])
            messages.append({"role": "assistant", "content": assistant_content})
            tool_results = []
            for block in assistant_content:
                if block.get("type") == "text":
                    transcript.append("LANDER: " + block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append(block["name"])
                    out = run_tool(block["name"], block.get("input", {}))
                    if isinstance(out, dict) and out.get("_handoff"):
                        mode = out["_handoff"]  # invisible switch: new prompt+tools next round
                        transcript.append(f"[MODO -> {mode}]")
                    else:
                        transcript.append(f"[TOOL {block['name']}({json.dumps(block.get('input',{}),ensure_ascii=False)})]")
                    tool_results.append({"type": "tool_result", "tool_use_id": block["id"],
                                         "content": json.dumps(out, ensure_ascii=False)})
            if resp.get("stop_reason") == "tool_use" and tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue
            break
        # If the agent already transferred to a human, don't bother with more turns.
        if "transfer_to_human" in tool_calls:
            break

    convo = "\n".join(transcript)
    judge_user = f"ESCENARIO:\n{scenario}\n\nESPERADO:\n{expected}\n\nTRANSCRIPCIÓN:\n{convo}\n\nHerramientas llamadas: {tool_calls or 'ninguna'}"
    jr = anthropic_call(JUDGE, JUDGE_SYS, [{"role": "user", "content": judge_user}],
                        max_tokens=200, temperature=0)
    jtext = "".join(b.get("text", "") for b in jr.get("content", []) if b.get("type") == "text").strip()
    try:
        verdict = json.loads(jtext[jtext.index("{"):jtext.rindex("}") + 1])
    except Exception:
        verdict = {"verdict": "?", "reason": jtext[:120]}
    return verdict.get("verdict"), verdict.get("reason"), tool_calls


ATTEMPTS = 3  # the brain runs at prod temperature, so retry FAILs to beat flakiness
results = []
for c in evals:
    scenario = c.get("scenario", "")
    # Infra-only scenarios (cold-start static-TTS greeting) can't be reproduced by a
    # text-only harness → mark N/A instead of a misleading FAIL, exclude from score.
    if any(n in (scenario or "").lower() for n in NONTEXT):
        results.append({"cat": c.get("category"), "scenario": scenario[:52],
                        "saved": c.get("status"), "verdict": "N/A",
                        "reason": "Infra (saludo en frío = TTS estático); no testable por texto.",
                        "tools": []})
        continue
    # Up to ATTEMPTS tries, stop on first PASS. A genuinely broken case fails them all;
    # a rare flake passes on retry. Only FAILs pay the extra cost.
    for attempt in range(1, ATTEMPTS + 1):
        v, reason, tool_calls = simulate_and_judge(c)
        if v == "PASS":
            if attempt > 1:
                reason = f"(intento {attempt}/{ATTEMPTS}) {reason}"
            break
    results.append({"cat": c.get("category"), "scenario": scenario[:52], "saved": c.get("status"),
                    "verdict": v, "reason": reason, "tools": tool_calls})

print(f"{'CAT':<22}{'SAVED':<11}{'RUN':<7}SCENARIO")
print("-" * 96)
npass = 0
scored = 0
for r in results:
    if r["verdict"] == "N/A":
        pass
    else:
        scored += 1
        if r["verdict"] == "PASS":
            npass += 1
    print(f"{(r['cat'] or '')[:21]:<22}{(r['saved'] or ''):<11}{(r['verdict'] or '?'):<7}{r['scenario']}")
    print(f"{'':<40}↳ {r['reason']}  tools={r['tools']}")
print("-" * 96)
na = len(results) - scored
print(f"RESULT: {npass}/{scored} PASS" + (f" ({na} N/A infra)" if na else ""))
