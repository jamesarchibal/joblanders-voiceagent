#!/usr/bin/env python3
"""Throwaway: simula la demo Marlex con el MISMO prompt que el agente en vivo.

Modo directo (CRIBADO_DIRECT=1): sin Recepción ni handoff, entra ya en Cribado.
Uso:  sim_marlex.py [ca|es]
"""
import json, os, sys, urllib.request

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AGENT_DIR)
os.environ.setdefault("CRIBADO_DEMO_NUMBERS", "+34623004169")
os.environ.setdefault("CRIBADO_DIRECT", "1")

import agent as A

IDIOMA = (sys.argv[1] if len(sys.argv) > 1 else "ca").lower()
MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
AK = os.getenv("ANTHROPIC_API_KEY")
if not AK:
    for line in open(os.path.join(AGENT_DIR, ".env")):
        if line.startswith("ANTHROPIC_API_KEY="):
            AK = line.split("=", 1)[1].strip().strip('"').strip("'")
if not AK:
    sys.exit("no ANTHROPIC_API_KEY")


def call(system, messages, tools):
    body = {"model": MODEL, "max_tokens": 600, "system": system,
            "messages": messages, "tools": tools, "temperature": 0.6}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "x-api-key": AK,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


TOOLS_CRIBADO = [
    {"name": "registrar_cribado", "description":
     "Registra el cribado del candidato para el reclutador humano.",
     "input_schema": {"type": "object", "properties": {
         "puesto": {"type": "string"}, "nombre": {"type": "string"},
         "experiencia": {"type": "string"}, "disponibilidad": {"type": "string"},
         "salario": {"type": "string"}, "ubicacion": {"type": "string"},
         "idiomas": {"type": "string"}, "encaje": {"type": "string"},
         "motivo": {"type": "string"}}, "required": ["puesto"]}},
    {"name": "finalizar_llamada", "description": "Cuelga la llamada.",
     "input_schema": {"type": "object", "properties": {}}},
]

GUIONES = {
    "ca": [
        "Hola, sóc en James, he aplicat a una posició de consultor per a l'empresa Marlex.",
        "Cinc anys en consultoria de selecció, sobretot perfils d'IT i logística.",
        "Em podria incorporar d'aquí a dues setmanes, jornada completa.",
        "Estaria buscant uns quaranta-cinc mil bruts a l'any.",
        "Visc a Barcelona i em puc desplaçar sense problema, o híbrid.",
        "No, res més, gràcies.",
        "Molt bé, adéu.",
    ],
    # Reproduce la llamada REAL del 28 sep que salio mal: respuestas cortas,
    # un "si" a una pregunta abierta y un salario destrozado por el STT.
    # Lander NO debe inventarse datos ni tragarse los 930.000 EUR.
    "ruido": [
        "Sí.",
        "Dos.",
        "A l'última hora completa.",
        "Ah, nou cents trenta.",
        "Sí.",
        "No, merci.",
        "Adéu.",
    ],
    "es": [
        "Hola, soy James, he aplicado a una posición de consultor para la empresa Marlex.",
        "Cinco años en consultoría de selección, sobre todo perfiles de IT y logística.",
        "Podría incorporarme en dos semanas, jornada completa.",
        "Estaría buscando unos cuarenta y cinco mil brutos al año.",
        "Vivo en Barcelona y me puedo desplazar sin problema, o híbrido.",
        "No, nada más, gracias.",
        "Vale, perfecto. Adiós.",
    ],
}


def main():
    class _Cfg:  # cfg vacío: fuerza los ficheros locales de prompts/
        prompt_recepcion = prompt_ventas = prompt_soporte = None

    bundle = A._Bundle(
        base_prompt=A.PROMPT_PATH.read_text(encoding="utf-8"),
        role_prompts=A._load_role_prompts(_Cfg()),
        caller_context="El que llama es James Archibald, del número +34623004169. No consta como cliente.",
        recall_block="", directory_block="", caller="+34623004169", cribado_demo=True,
    )
    sys_cribado = bundle.instructions_for("cribado")

    saludo = A.GREETING_TEXT.format(name="Lander")
    print("=" * 78)
    print(f"DEMO MARLEX · idioma={IDIOMA} · modo DIRECTO (sin Recepción, sin handoff)")
    print("=" * 78)
    print(f"LANDER: {saludo}   <- TTS estático, sin LLM\n")

    # En la llamada real `session.say(...)` AÑADE el saludo al historial, asi que el
    # modelo ve en que idioma se abrio. Sin esta linea el simulador miente y Lander
    # parece cambiar al castellano cuando en produccion no lo hace.
    msgs = [{"role": "assistant", "content": saludo}]
    preguntas, registros = 0, 0
    pendientes = list(GUIONES[IDIOMA])
    colgado = False
    while not colgado:
        # Solo hablamos cuando el turno es nuestro (tras un tool_result no toca).
        if not msgs or msgs[-1]["role"] == "assistant":
            if not pendientes:
                break
            respuesta = pendientes.pop(0)
            msgs.append({"role": "user", "content": respuesta})
            print(f"JAMES:  {respuesta}\n")

        r = call(sys_cribado, msgs, TOOLS_CRIBADO)
        msgs.append({"role": "assistant", "content": r["content"]})

        said = "".join(b["text"] for b in r["content"] if b["type"] == "text").strip()
        if said:
            preguntas += said.count("?")
            print(f"LANDER: {said}")

        # Devolvemos un tool_result de verdad, como hace LiveKit en la llamada real.
        results = []
        for b in r["content"]:
            if b["type"] != "tool_use":
                continue
            print(f"[TOOL]  {b['name']} "
                  f"{json.dumps(b.get('input'), ensure_ascii=False) if b.get('input') else ''}")
            if b["name"] == "registrar_cribado":
                registros += 1
            if b["name"] == "finalizar_llamada":
                print("\n>>> LANDER CUELGA")
                colgado = True
            results.append({"type": "tool_result", "tool_use_id": b["id"],
                            "content": "ok"})
        if results and not colgado:
            msgs.append({"role": "user", "content": results})
        elif not results:
            print()

    print("=" * 78)
    print(f"RESUMEN: {preguntas} preguntas | registrar_cribado x{registros} "
          f"(debe ser 1)")
    print("=" * 78)


main()
