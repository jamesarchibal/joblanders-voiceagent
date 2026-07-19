"""Local test harness for the caller-routing logic — no phone call needed.

Tests the two things that today can only be validated by placing a real call:
  1. Deterministic part: does a caller-ID match the fake client CRM (existing vs
     new), and does the directory resolve the right destination phone?
  2. LLM part (optional): given the SAME instructions the live agent builds
     (system prompt + caller context + directory block), what does Claude
     actually decide to do with a fake caller message? Calls the Anthropic API
     directly (text only, no audio/LiveKit/telephony) with the same
     `transfer_to_human` tool schema used on a real call.

Examples:
    # Deterministic only (free, instant) — known existing client:
    python test_routing.py --caller +34623004169 --local --no-llm

    # Full routing decision for an unknown caller reporting a login problem:
    python test_routing.py --caller +34611222333 --local \\
        --message "hola, no puedo entrar a mi cuenta, me da error"

    # Against the live Supabase clients/directory instead of the local seed data:
    python test_routing.py --caller +34600111222 --message "quiero ampliar mi plan"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from agent import (  # noqa: E402 — after load_dotenv, mirrors agent.py's own order
    PROMPT_PATH,
    _caller_context,
    _directory_block,
    _match_client,
    _resolve_dest,
)
from config import load_config

TRANSFER_TOOL = {
    "name": "transfer_to_human",
    "description": (
        "Transfer the caller to the right human, chosen from the directory. Call "
        "this when the caller asks for a person, refuses to talk to an AI, is "
        "frustrated, or the case is beyond what you can resolve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department": {"type": "string", "description": "Department from the directory."},
            "person": {"type": "string", "description": "Person from the directory to route to."},
            "summary": {"type": "string", "description": "One-sentence summary of the caller's need, in Spanish."},
        },
        "required": ["department", "person", "summary"],
    },
}


def _load_local_seed() -> tuple[list[dict], list[dict]]:
    """Reuse the same fake data seed_directory.py pushes to Supabase, offline."""
    import seed_directory

    return seed_directory.CLIENTS, seed_directory.DIRECTORY


def run(caller: str, message: str | None, use_local: bool, call_llm: bool) -> None:
    if use_local:
        clients, directory = _load_local_seed()
        print("[DATA] using local seed_directory.py (not Supabase)")
    else:
        cfg = load_config()
        clients, directory = cfg.clients, cfg.directory
        print(f"[DATA] loaded from Supabase: clients={len(clients)} directory={len(directory)}")

    client = _match_client(caller, clients)
    if client:
        print(f"[CLIENT] {caller} -> EXISTING: {client.get('company')} (gestor={client.get('account_manager') or '—'})")
    else:
        print(f"[CLIENT] {caller} -> NEW/unknown")

    context = _caller_context(client, directory)
    directory_block = _directory_block(directory)
    print("\n--- caller context appended to the prompt ---")
    print(context)
    print("\n--- directory block appended to the prompt ---")
    print(directory_block or "(empty — seed the directory first)")

    if not call_llm:
        print("\n(--no-llm: skipping the live routing decision)")
        return
    if not message:
        raise SystemExit("--message is required unless --no-llm is passed")

    import anthropic

    client_llm = anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8") + "\n\n" + context + "\n\n" + directory_block
    print("\n--- asking Claude what it would do ---")
    resp = client_llm.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        tools=[TRANSFER_TOOL],
        messages=[{"role": "user", "content": message}],
    )
    for block in resp.content:
        if block.type == "text":
            print(f"[SAY] {block.text}")
        elif block.type == "tool_use" and block.name == "transfer_to_human":
            dept = block.input.get("department")
            person = block.input.get("person")
            summary = block.input.get("summary")
            dest = _resolve_dest(person, directory)
            print(f"[ROUTE] department={dept!r} person={person!r} -> dial {dest}")
            print(f"[SUMMARY] {summary}")
        else:
            print(f"[TOOL] {block.name}: {json.dumps(block.input)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--caller", required=True, help="Fake caller-ID, e.g. +34611222333")
    parser.add_argument("--message", help="Fake first thing the caller says (required unless --no-llm)")
    parser.add_argument("--local", action="store_true", help="Use seed_directory.py instead of Supabase")
    parser.add_argument("--no-llm", action="store_true", help="Skip the Claude call; only test client/directory matching")
    args = parser.parse_args()
    run(args.caller, args.message, args.local, call_llm=not args.no_llm)
