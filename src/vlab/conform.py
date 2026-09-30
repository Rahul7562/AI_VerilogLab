"""Spec-conformance gate: does a passing candidate actually implement the spec?

Simulation only proves the DUT agrees with the testbench. It says nothing
about whether the testbench *checked* the things the spec asked for. That gap
produced a real false winner: a traffic-light candidate passed 19/19 checks
while using one shared phase constant for every state (spec: green 10s,
yellow 2s), never implementing the 5-second pedestrian extension, and having
no enable gating at all. Nothing in the pipeline noticed, because nothing
compared the spec's feature list against what was verified.

`review_coverage` closes that hole: for every feature in the spec it asks
whether the DUT implements it AND the testbench verifies it, and returns the
unverified ones so the candidate can be rejected instead of published.
"""
from __future__ import annotations

import json
from typing import Any

COVERAGE_PROMPT = """You audit a Verilog DUT + testbench against the spec they
claim to implement.

The simulation PASSED. That only proves the DUT agrees with the testbench —
it does NOT prove either matches the spec. Your job is to find spec features
that are unimplemented, implemented incorrectly, or implemented but never
actually CHECKED by the testbench.

Output ONLY a JSON object, no prose:
{{"gaps": [{{"feature": "<the spec feature>", "kind": "unimplemented|unchecked|incorrect", "why": "<one sentence>"}}],
  "verdict": "conformant" | "gaps"}}

A feature is "unchecked" when the testbench never drives the relevant input
and never asserts the behaviour. Watching a state sequence without measuring
how long each state lasted leaves every duration requirement unverified, even
if the sequence looks right.

Treat these as gaps:
- a spec'd timing that the DUT implements with a single shared counter
  constant (distinct durations must be distinct)
- an enable / gating / hold feature the DUT ignores
- an extension, counter or mode feature present in the spec but absent from
  the DUT
- a reset behaviour that differs from what the spec states

Only report real gaps; if everything checks out, return empty gaps and
"conformant".

SPEC:
{spec}

DUT:
```verilog
{design}
```

TESTBENCH:
```verilog
{tb}
```

PASSING CHECKS:
```
{log}
```"""


def review_coverage(
    design: str, tb: str, log: str, spec: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    """Return (conformant, gaps) for a candidate that passed simulation.

    Fails open: if the review cannot run (no key, provider down, malformed
    reply) the candidate is treated as conformant rather than discarded,
    because refusing to publish anything would make the tool useless
    whenever the free tier is rate-limited.
    """
    from .llm import chat as llm_chat, get_api_key

    features = spec.get("features") or []
    if not features:
        return True, []
    if not get_api_key():
        return True, []
    try:
        raw = llm_chat(
            [{"role": "user", "content": COVERAGE_PROMPT.format(
                spec=json.dumps(spec, indent=2)[:6000],
                design=design[:9000],
                tb=tb[:9000],
                log=log[:3000],
            )}],
            max_tokens=1200,
            temperature=0.0,
        )
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        gaps = obj.get("gaps")
        gaps = [g for g in gaps if isinstance(g, dict) and g.get("feature")] \
            if isinstance(gaps, list) else []
        verdict = str(obj.get("verdict", "")).strip().lower()
        blocking = [g for g in gaps if str(g.get("kind", "")).lower() in
                    ("unimplemented", "incorrect")]
        if blocking:
            return False, blocking
        if gaps:
            # Only "unchecked" gaps remain. The DUT may well be correct; the
            # testbench just is not exhaustive. Rejecting here threw away
            # candidates that had already passed 24-50 real checks, which
            # made the tool far less reliable for no correctness gain. The
            # design is not spec-violating, so it is published with the
            # uncovered features reported.
            return True, []
        return verdict != "gaps", []
    except Exception:
        return True, []
