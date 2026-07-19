"""Pre-flight health check for the voice agent's providers.

Catches, BEFORE a real call, the two classes of failure that have already bitten
this project silently in production:
  - ElevenLabs key invalid / quota exhausted -> agent joins the call, no audio.
  - Supabase URL/key malformed/unreachable -> dashboard edits silently stop
    reaching the live agent (config.py degrades to local defaults).
Also checks Deepgram + Anthropic auth so a bad key surfaces here, not on a call.

Run manually any time, or wire into a cron/GH Action:
    python healthcheck.py

If SLACK_WEBHOOK_URL is set, posts an alert ONLY when something needs
attention (WARN/FAIL) — a clean run is silent on Slack, still prints locally.

Exit code: 0 if nothing is FAIL, 1 if any check FAILs (WARN doesn't fail the run).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv(override=True)

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# Low-quota threshold: below this many remaining chars, warn (a phone call can
# easily burn a few hundred chars/turn).
ELEVEN_LOW_QUOTA_CHARS = 2000


def _get(url: str, headers: dict, timeout: float = 6.0) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URLs)
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001 — report as a failed check, don't crash
        return -1, {"_error": repr(exc)}


def check_elevenlabs() -> tuple[str, str]:
    if not ELEVEN_API_KEY:
        return "FAIL", "ELEVENLABS_API_KEY / ELEVEN_API_KEY not set"
    status, data = _get(
        "https://api.elevenlabs.io/v1/user/subscription",
        headers={"xi-api-key": ELEVEN_API_KEY},
    )
    if status != 200 or not isinstance(data, dict):
        return "FAIL", f"HTTP {status} ({(data or {}).get('_error', 'auth/quota error')})"
    used = data.get("character_count")
    limit = data.get("character_limit")
    if isinstance(used, int) and isinstance(limit, int):
        remaining = limit - used
        if remaining <= 0:
            return "FAIL", f"quota exhausted ({used}/{limit} chars) — calls will be SILENT"
        if remaining < ELEVEN_LOW_QUOTA_CHARS:
            return "WARN", f"low quota: {remaining} chars remaining ({used}/{limit})"
        return "OK", f"{remaining} chars remaining ({used}/{limit})"
    return "OK", "key valid (quota fields not returned)"


def check_deepgram() -> tuple[str, str]:
    if not DEEPGRAM_API_KEY:
        return "FAIL", "DEEPGRAM_API_KEY not set"
    status, data = _get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
    )
    if status == 200:
        return "OK", "key valid"
    return "FAIL", f"HTTP {status} ({(data or {}).get('_error', 'auth error')})"


def check_anthropic() -> tuple[str, str]:
    if not ANTHROPIC_API_KEY:
        return "FAIL", "ANTHROPIC_API_KEY not set"
    status, data = _get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
    )
    if status == 200:
        return "OK", "key valid"
    return "FAIL", f"HTTP {status} ({(data or {}).get('_error', 'auth error')})"


def check_supabase() -> tuple[str, str]:
    # Non-fatal by design (config.py degrades to local defaults), but WARN loudly:
    # this is the exact failure mode that silently disconnected the dashboard once.
    if not (SUPABASE_URL and SUPABASE_KEY):
        return "WARN", "SUPABASE_URL/KEY not set — agent runs on local defaults only"
    url = f"{SUPABASE_URL}/rest/v1/landeros_config?select=key&limit=1"
    status, data = _get(
        url,
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    if status == 200:
        return "OK", "reachable"
    err = (data or {}).get("_error") if isinstance(data, dict) else None
    return "WARN", f"unreachable (HTTP {status}{', ' + err if err else ''}) — using local defaults"


CHECKS = [
    ("ElevenLabs TTS", check_elevenlabs),
    ("Deepgram STT", check_deepgram),
    ("Anthropic LLM", check_anthropic),
    ("Supabase config", check_supabase),
]


def _slack_alert(lines: list[str]) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    text = "*Voice agent health check needs attention:*\n" + "\n".join(lines)
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps({"text": text}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310 (trusted URL)
    except Exception as exc:  # noqa: BLE001 — don't let alerting itself crash the check
        print(f"[SLACK] alert failed: {exc!r}")


def main() -> int:
    results: list[tuple[str, str, str]] = []
    for name, fn in CHECKS:
        status, detail = fn()
        results.append((name, status, detail))
        print(f"[{status:4}] {name}: {detail}")

    bad = [f"[{s}] {n}: {d}" for n, s, d in results if s != "OK"]
    if bad:
        _slack_alert(bad)

    return 1 if any(s == "FAIL" for _, s, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
