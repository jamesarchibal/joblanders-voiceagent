"""Knowledge base for the Joblanders voice agent.

MVP: hard-coded facts so the agent can answer basics and pitch products WITHOUT
hallucinating. This is deliberately a single, swappable module — later, replace
the bodies of `get_product_info` and `lookup_application_status` with real calls
to JoblandersOS (via MCP or HTTP) and nothing else in the agent has to change.

Golden rule: the agent may ONLY state facts that come back from these functions.
If a fact isn't here, the function says so and the agent offers a human.
"""

from __future__ import annotations

# --- Product: joblanders.ai (edit freely) ------------------------------------
# Single source of truth for what the agent may quote. joblanders.ai sells custom,
# on-premise AI agents to high-volume recruiting/staffing/talent agencies — NOT the
# .com candidate marketplace. See memory/joblanders-ai-pitch.md.
PRODUCT: dict[str, str] = {
    "nombre": "Joblanders (joblanders.ai)",
    "que_es": (
        "Construimos agentes de IA a medida que automatizan todo el ciclo de "
        "recruiting de forma nativa — agentes que hacen el trabajo, no digitalización."
    ),
    "para_quien": (
        "Agencias grandes de recruiting, staffing y talent management con mucho volumen."
    ),
    "on_premise": (
        "Los agentes se despliegan on-premise, en la infraestructura del cliente: "
        "ellos controlan el flujo, los datos y los agentes. Nosotros los construimos "
        "e integramos con todo lo que ya usan."
    ),
    "integracion": (
        "Es a medida: trabajamos con la empresa el tiempo que haga falta —del orden de "
        "unos días a unas pocas semanas, según su volumen y sus flujos— para integrarlo "
        "de forma full y personalizada. Al ser a medida no cerramos una fecha exacta de "
        "entrada; lo concretamos con ellos."
    ),
    "precio": "PENDIENTE_DEFINIR",  # sentinel: never quote a made-up price
}

# The agent suite — nombrar solo si el que llama pide detalle (mensaje macro primero).
SUITE: list[dict[str, str]] = [
    {"nombre": "Sourcing / Hunting", "que_hace": "busca y saca candidatos a escala."},
    {"nombre": "Screening / Advisor", "que_hace": "pruebas de habilidades, screening, video o llamadas."},
    {"nombre": "Lander", "que_hace": "scoring, matching, ranking y reportes de candidatos."},
    {"nombre": "Asistente de consultores", "que_hace": "agenda, prioridades y día a día del consultor."},
    {"nombre": "Onboarding", "que_hace": "onboarding (más para la empresa final)."},
]


def get_product_info(topic: str) -> dict:
    """Return factual product info the agent is allowed to quote.

    `topic` is a free-text hint from the model (e.g. "precio", "suite", "on
    premise", "integración"). Returns a dict the agent reads from; never improvise
    beyond it.
    """
    topic_l = (topic or "").lower()

    # Specific intents first (checked before price, whose "cuánto" is greedy).
    # Suite detail — only when the caller digs in.
    if any(k in topic_l for k in ("agente", "suite", "sourcing", "hunting", "screening",
                                  "onboarding", "consultor", "matching", "qué hacéis", "que haceis")):
        return {"found": True, "nombre": PRODUCT["nombre"], "suite": SUITE}

    # On-premise / data control.
    if any(k in topic_l for k in ("premise", "premisa", "datos", "seguridad", "on-prem", "onprem")):
        return {"found": True, "on_premise": PRODUCT["on_premise"]}

    # Integration time / how it works.
    if any(k in topic_l for k in ("integr", "tarda", "tiempo", "implant", "despliegue", "semana")):
        return {"found": True, "integracion": PRODUCT["integracion"]}

    # Price is intentionally gated: never quote, Comercial closes it.
    if any(k in topic_l for k in ("precio", "coste", "cuánto", "cuanto", "tarifa", "plan", "presupuesto")):
        return {
            "found": False,
            "reason": "price_not_public",
            "say": (
                "El precio depende del alcance y del volumen, así que eso lo cierra "
                "mejor una persona del equipo comercial."
            ),
        }

    # General macro pitch.
    return {
        "found": True,
        "nombre": PRODUCT["nombre"],
        "que_es": PRODUCT["que_es"],
        "para_quien": PRODUCT["para_quien"],
    }


def get_support_info(topic: str) -> dict:
    """Return factual SUPPORT info the agent may quote to an existing client.

    Fallback only: the live support facts are managed in the dashboard (LanderOS →
    Knowledge, "Soporte"). Until those are filled there's nothing baked in here, so
    this defers to a human rather than invent a how-to or a policy.
    """
    return {
        "found": False,
        "reason": "not_in_kb",
        "say": (
            "Eso lo confirmo con una persona del equipo para no darte un dato "
            "equivocado; te paso con quien lleva tu cuenta."
        ),
    }


def lookup_application_status(reference: str) -> dict:
    """Look up a candidate application / account by reference.

    STUB: wire this to JoblandersOS later. For now it always returns not-found so
    the agent honestly defers to a human instead of inventing a status.
    """
    return {
        "found": False,
        "reason": "not_connected",
        "say": (
            "Ahora mismo no puedo consultar tu caso en el sistema, pero te paso "
            "con una persona que lo mira contigo."
        ),
    }
