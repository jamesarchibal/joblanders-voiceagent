"""Read LanderOS config from Supabase at session start, with local fallback.

The dashboard (LanderOS tab) writes prompt / knowledge / voice params to the
`landeros_config` table. The agent reads them once when a call starts, so edits
in the dashboard drive the live agent without a redeploy. If Supabase is
unreachable or a value is missing, the agent falls back to its baked-in defaults
(env vars + prompts/system_es.md + knowledge.py) and keeps working.

Uses only the standard library (no extra dependency); the single blocking read
runs in a thread from the async entrypoint.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")


@dataclass
class RemoteConfig:
    prompt: str | None = None  # base compartida (system_prompt) para los tres modos
    # Bloques de rol por modo (Recepción / Ventas / Soporte). Se anexan a la base;
    # si no hay override en el dashboard, el agente usa los ficheros prompts/role_*_es.md.
    prompt_recepcion: str | None = None
    prompt_ventas: str | None = None
    prompt_soporte: str | None = None
    # Enrutado: "unified" = un número, Recepción triajea y pasa (invisible) a
    # Ventas/Soporte como subagentes; "separate" = 3 agentes independientes, cada
    # uno con su número (DID) y su nombre, sin triaje ni cambio de modo.
    routing_mode: str = "unified"
    # Config por agente/modo: {role: {"name": str, "phone": str, "enabled": bool}}.
    # `phone` es el DID entrante que arranca directamente ese modo en "separate".
    agents: dict = field(default_factory=dict)
    knowledge: list[dict] = field(default_factory=list)  # [{id, topic, answer}, ...]
    params: dict[str, float] = field(default_factory=dict)  # {PARAM_ID: value}
    transfer_script: str | None = None  # guion del agente-susurro (traspaso a humano)
    # Fake "client CRM": inbound caller-ID -> known client (existing vs new).
    clients: list[dict] = field(default_factory=list)  # [{phone,name,company,plan,status,notes}]
    # Directorio de transferencia: departamentos + personas + palabras clave del motivo.
    directory: list[dict] = field(default_factory=list)  # [{department,person,role,keywords,phone}]


def _fetch_rows() -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/landeros_config?select=key,value"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_KEY or "",
            "Authorization": f"Bearer {SUPABASE_KEY or ''}",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (trusted URL)
        return json.loads(resp.read().decode("utf-8"))


def load_config() -> RemoteConfig:
    """Best-effort read of dashboard config. Never raises."""
    cfg = RemoteConfig()
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("[CONFIG] SUPABASE_URL/KEY not set; using local defaults")
        return cfg
    try:
        rows = _fetch_rows()
    except Exception as exc:  # noqa: BLE001 — degrade gracefully on any failure
        print(f"[CONFIG] Supabase read failed ({exc!r}); using local defaults")
        return cfg

    by_key = {row.get("key"): row.get("value") for row in rows if isinstance(row, dict)}

    sp = by_key.get("system_prompt") or {}
    if isinstance(sp.get("text"), str) and sp["text"].strip():
        cfg.prompt = sp["text"]

    # Per-mode role blocks (optional dashboard overrides of prompts/role_*_es.md).
    for role, attr in (
        ("prompt_recepcion", "prompt_recepcion"),
        ("prompt_ventas", "prompt_ventas"),
        ("prompt_soporte", "prompt_soporte"),
    ):
        rp = by_key.get(role) or {}
        if isinstance(rp.get("text"), str) and rp["text"].strip():
            setattr(cfg, attr, rp["text"])

    kb = by_key.get("knowledge") or {}
    if isinstance(kb.get("facts"), list):
        cfg.knowledge = [f for f in kb["facts"] if isinstance(f, dict)]

    vp = by_key.get("voice_params") or {}
    fields = vp.get("fields")
    if isinstance(fields, list):
        for f in fields:
            if isinstance(f, dict) and "id" in f and "value" in f:
                try:
                    cfg.params[str(f["id"])] = float(f["value"])
                except (TypeError, ValueError):
                    pass

    ts = by_key.get("transfer_script") or {}
    if isinstance(ts.get("text"), str) and ts["text"].strip():
        cfg.transfer_script = ts["text"]

    cl = by_key.get("clients") or {}
    if isinstance(cl.get("list"), list):
        cfg.clients = [c for c in cl["list"] if isinstance(c, dict)]

    dr = by_key.get("directory") or {}
    if isinstance(dr.get("people"), list):
        cfg.directory = [d for d in dr["people"] if isinstance(d, dict)]

    ag = by_key.get("agents") or {}
    if ag.get("mode") in ("unified", "separate"):
        cfg.routing_mode = ag["mode"]
    if isinstance(ag.get("list"), list):
        for a in ag["list"]:
            if isinstance(a, dict) and isinstance(a.get("role"), str):
                cfg.agents[a["role"]] = {
                    "name": str(a.get("name") or ""),
                    "phone": str(a.get("phone") or ""),
                    "enabled": bool(a.get("enabled", True)),
                }

    print(
        f"[CONFIG] loaded from Supabase: prompt={'yes' if cfg.prompt else 'no'} "
        f"knowledge={len(cfg.knowledge)} params={len(cfg.params)} "
        f"transfer_script={'yes' if cfg.transfer_script else 'no'} "
        f"clients={len(cfg.clients)} directory={len(cfg.directory)} "
        f"routing={cfg.routing_mode} agents={len(cfg.agents)}"
    )
    return cfg
