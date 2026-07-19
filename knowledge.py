"""Knowledge base for the Joblanders voice agent.

MVP: hard-coded facts so the agent can answer basics and pitch products WITHOUT
hallucinating. This is deliberately a single, swappable module — later, replace
the bodies of `get_product_info` and `lookup_application_status` with real calls
to JoblandersOS (via MCP or HTTP) and nothing else in the agent has to change.

Golden rule: the agent may ONLY state facts that come back from these functions.
If a fact isn't here, the function says so and the agent offers a human.
"""

from __future__ import annotations

# --- Products / plans (edit freely) ------------------------------------------
# Keep values here as the single source of truth for what the agent may quote.
PRODUCTS: dict[str, dict[str, str]] = {
    "matching": {
        "nombre": "Emparejamiento con IA",
        "que_es": (
            "Joblanders usa IA para emparejar a tu empresa con talento joven "
            "cribado y encajado con tu vacante. Recibes candidatos ya filtrados, "
            "no un montón de CVs."
        ),
        "para_quien": "Empresas que contratan perfiles junior de cuello blanco en España.",
        "precio": "PENDIENTE_DEFINIR",  # keep as sentinel so the agent won't quote a made-up price
    },
}


def get_product_info(topic: str) -> dict:
    """Return factual product/plan info the agent is allowed to quote.

    `topic` is a free-text hint from the model (e.g. "precio", "matching",
    "planes"). Returns a dict the agent should read from; never improvise beyond it.
    """
    topic_l = (topic or "").lower()

    # Price is intentionally gated: never quote until it's defined.
    if any(k in topic_l for k in ("precio", "coste", "cuánto", "cuanto", "tarifa", "plan")):
        p = PRODUCTS["matching"]
        if p["precio"] == "PENDIENTE_DEFINIR":
            return {
                "found": False,
                "reason": "price_not_public",
                "say": (
                    "El precio depende del volumen y los perfiles, así que eso "
                    "lo cierra mejor una persona del equipo comercial."
                ),
            }
        return {"found": True, "precio": p["precio"], "nombre": p["nombre"]}

    # General product explanation.
    p = PRODUCTS["matching"]
    return {
        "found": True,
        "nombre": p["nombre"],
        "que_es": p["que_es"],
        "para_quien": p["para_quien"],
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
