#!/usr/bin/env python3
"""Layer-2 latency meter for Lander (voice-agent).

Reads the `[LATENCY] {...}` JSON lines that agent.py emits per turn (one per
metrics event) and reports p50/p95/max per phase, plus a PASS/FAIL against
budgets. This is the voice-layer complement to evals/run.py (which only tests
the brain): it measures what a text harness can't — how fast the caller hears
a reply, and the whisper delay on transfer pickup.

Usage:
    # live, straight from LiveKit Cloud (last N minutes of prod logs):
    lk agent logs | python evals/latency.py
    # or from a saved log file:
    python evals/latency.py worker.log

Phases (LiveKit metric fields):
    end_of_utterance_delay  turn-detection lag: caller stops -> agent starts thinking
    ttft                    LLM time-to-first-token
    ttfb                    TTS time-to-first-byte (caller starts hearing audio)
    duration                full LLM/TTS generation
The perceived "gap of silence" after the caller speaks ~= EOU + ttft + ttfb.
"""
import json
import sys

# Budgets (seconds). Tuned for a phone call: anything past these feels laggy.
BUDGETS = {
    "end_of_utterance_delay": 0.8,
    "ttft": 1.2,
    "ttfb": 1.0,
    "response_gap": 2.5,  # EOU + ttft + ttfb, the felt silence
}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))
    return xs[i]


def fmt(x):
    return "  -  " if x is None else f"{x:5.2f}"


def main():
    src = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin
    buckets: dict[str, list[float]] = {}
    gaps: list[float] = []
    turn: dict[str, float] = {}

    for line in src:
        i = line.find("[LATENCY]")
        if i < 0:
            continue
        try:
            m = json.loads(line[i + len("[LATENCY]"):].strip())
        except Exception:
            continue
        for k in ("end_of_utterance_delay", "ttft", "ttfb"):
            v = m.get(k)
            if isinstance(v, (int, float)):
                buckets.setdefault(k, []).append(v)
                turn[k] = v
        # A TTS event (ttfb) closes a turn: sum whatever we gathered into a gap.
        if "ttfb" in m and turn:
            gaps.append(sum(turn.get(k, 0.0) for k in
                            ("end_of_utterance_delay", "ttft", "ttfb")))
            turn = {}

    buckets["response_gap"] = gaps
    n = max((len(v) for v in buckets.values()), default=0)
    if not n:
        sys.exit("No [LATENCY] lines found. Deploy the metrics handler, place a "
                 "call, then: lk agent logs | python evals/latency.py")

    print(f"{'PHASE':<24}{'p50':>7}{'p95':>7}{'max':>7}{'budget':>8}{'':>4}samples")
    print("-" * 64)
    ok = True
    for phase, budget in BUDGETS.items():
        xs = buckets.get(phase, [])
        p95 = pct(xs, 95)
        verdict = "  "
        if p95 is not None:
            good = p95 <= budget
            ok = ok and good
            verdict = "OK" if good else "!!"
        print(f"{phase:<24}{fmt(pct(xs,50)):>7}{fmt(p95):>7}"
              f"{fmt(max(xs) if xs else None):>7}{budget:>8.2f}{verdict:>4}"
              f"  {len(xs)}")
    print("-" * 64)
    print("RESULT: " + ("PASS — all phases within budget" if ok
                        else "FAIL — a phase exceeds its p95 budget (see !!)"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
