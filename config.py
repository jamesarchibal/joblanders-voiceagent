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
    prompt: str | None = None
    knowledge: list[dict] = field(default_factory=list)  # [{id, topic, answer}, ...]
    params: dict[str, float] = field(default_factory=dict)  # {PARAM_ID: value}
    transfer_script: str | None = None  # guion del agente-susurro (traspaso a humano)


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

    print(
        f"[CONFIG] loaded from Supabase: prompt={'yes' if cfg.prompt else 'no'} "
        f"knowledge={len(cfg.knowledge)} params={len(cfg.params)} "
        f"transfer_script={'yes' if cfg.transfer_script else 'no'}"
    )
    return cfg
