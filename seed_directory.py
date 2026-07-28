"""Seed the fake client CRM + transfer directory into the dashboard Supabase.

Writes two rows into `landeros_config` (same table the agent already reads at call
start via config.py):
  - key "clients"   -> {"list":[ {phone,name,company,plan,status,account_manager,notes}, ... ]}
  - key "directory" -> {"people":[ {department,person,role,keywords,phone}, ... ]}

This is fake test data so we can real-life-test "new vs existing client" routing.
Every directory phone points at James's number for now, so transfers still ring
James — but the agent picks the RIGHT department + person (visible in the whisper
and the call log). Swap in real per-person numbers later.

Run:  python seed_directory.py         (creds read from .env / environment)
Edit the CLIENTS / DIRECTORY lists below to change the fake data, then re-run.
"""

from __future__ import annotations

import json
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

# James's number — seeded as an existing client so an inbound call from it is
# recognised during testing. Also the single real transfer destination for now.
JAMES = "+34623004169"

# `account_manager` = persona del DIRECTORY que gestiona a ese cliente. Para un
# cliente EXISTENTE, Lander tiende a pasarle con su gestor (salvo que el motivo sea
# claramente de otro departamento). Si un cliente no tiene gestor -> departamento General.
CLIENTS = [
    {
        "phone": JAMES,
        "name": "James (test)",
        "company": "Cliente Demo",
        "plan": "Pro",
        "status": "activo",
        "account_manager": "James",
        "notes": "Fila de PRUEBA (el número de James) para testear el flujo de cliente existente.",
    },
    {
        "phone": "+34600111222",
        "name": "Laura Gómez",
        "company": "Randstad",
        "plan": "Enterprise",
        "status": "activo",
        "account_manager": "James",
        "notes": "Renovación en septiembre; interesada en ampliar volumen de vacantes.",
    },
]

# Todas las personas son "James" por ahora: hoy hay UN solo humano real y todas las
# transferencias marcan su número. Se mantienen los departamentos + keywords para el
# enrutado; cuando haya equipo real, sustituir cada `person`/`phone` por la persona
# y número correctos.
DIRECTORY = [
    {
        "department": "Cuentas",
        "person": "James",
        "role": "incidencias técnicas y acceso",
        "keywords": ["no puedo entrar", "error", "bug", "acceso", "la app no va",
                     "falla", "técnico", "contraseña", "no funciona"],
        "phone": JAMES,
    },
    {
        "department": "Cuentas",
        "person": "James",
        "role": "cuenta y facturación",
        "keywords": ["factura", "pago", "cobro", "cuenta", "suscripción", "baja",
                     "recibo", "plan actual", "precio de mi plan"],
        "phone": JAMES,
    },
    {
        "department": "Comercial",
        "person": "James",
        "role": "nuevas empresas",
        "keywords": ["contratar", "empezar", "nuevo", "precio", "demo", "interesado",
                     "cómo funciona", "alta", "quiero contratar"],
        "phone": JAMES,
    },
    {
        "department": "Comercial",
        "person": "James",
        "role": "ampliaciones y renovaciones",
        "keywords": ["ampliar", "más vacantes", "añadir", "upgrade", "renovar",
                     "subir plan", "upsell", "más volumen"],
        "phone": JAMES,
    },
    {
        "department": "General",
        "person": "James",
        "role": "recepción general (fallback cuando no hay gestor ni departamento claro)",
        "keywords": ["hablar con alguien", "recepción", "general", "no sé", "cualquiera"],
        "phone": JAMES,
    },
]


def _upsert(rows: list[dict]) -> None:
    url = f"{SUPABASE_URL}/rest/v1/landeros_config"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY or "",
            "Authorization": f"Bearer {SUPABASE_KEY or ''}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted URL)
        print(f"  -> HTTP {resp.status}")


def main() -> None:
    if not (SUPABASE_URL and SUPABASE_KEY):
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    print(f"Seeding {len(CLIENTS)} clients + {len(DIRECTORY)} directory people into landeros_config…")
    _upsert([
        {"key": "clients", "value": {"list": CLIENTS}},
        {"key": "directory", "value": {"people": DIRECTORY}},
    ])
    print("Done.")


if __name__ == "__main__":
    main()
