#!/usr/bin/env python
"""Live routing eval for the get_nrb_forex tool.

Runs the real agent loop against a real model and the live NRB API, and scores
whether the model routes NRB-rate questions to `get_nrb_forex` with the right
arguments — and, just as importantly, keeps NRB *document* questions away from it.

This is the model-side counterpart to tests/test_nrb_forex.py: the unit tests
prove the tool is correct given an argument dict; this proves the model produces
the right argument dict (and gets the answer to the user).

WHY THIS EXISTS — the failure it catches
    Ollama defaults to a 4096-token context, and `num_ctx` cannot be set per
    request on the /v1 surface. With ~19 tool schemas the prompt floor is ~4092
    tokens, so at the default the window overflows, Ollama silently drops the
    front of the prompt (the tool definitions and the date line), and the model
    "helpfully" refuses: "I don't have access to historical NRB data." The tool
    was never called. Raising OLLAMA_CONTEXT_LENGTH on the Ollama *service* fixes
    it. This eval turns that invisible, intermittent failure into a red line.

USAGE
    # local dev model (uses .env: OLLAMA_BASE_URL, AGENT_MODEL, MCP_SERVER_URL)
    .venv/bin/python scripts/eval_nrb_forex_routing.py

    # point at the GPU box / a specific model without editing .env
    OLLAMA_BASE_URL=http://<SERVER_HOST>:11434 AGENT_MODEL=qwen3.5:35b-a3b \
        .venv/bin/python scripts/eval_nrb_forex_routing.py

    # local tools only (drop MCP), for a reproducible token-floor measurement
    MCP_SERVER_URL= .venv/bin/python scripts/eval_nrb_forex_routing.py

    # steadier signal on the flaky negative case
    EVAL_REPEAT=5 .venv/bin/python scripts/eval_nrb_forex_routing.py

Exit code is 0 only if every case passes every repeat, so this is CI/smoke-test
usable. It hits the live NRB API by design — it is an integration check, not a
unit test — so it needs network and a reachable model server; skip it where those
are absent.

The labelled set is small (6 cases) on purpose: it is the review-loop artifact
named in the tool's Evaluation & Improvement section, meant to be re-run after a
context-length change, a description edit, or a model swap — not a benchmark.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Import the app exactly as the server builds it, so the eval measures the real
# system prompt, tool registry and agent loop — not a reconstruction.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.loop import run_turn  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import _build_mcp_client  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402

# --------------------------------------------------------------------------- #
# The labelled set. `must_say` / `must_not_say` are substring checks on the final
# answer; `expect_args` checks the arguments the model passed to get_nrb_forex.
# Dates are historical and fixed so the expected values never drift.
# --------------------------------------------------------------------------- #
CASES = [
    {
        "id": "today-usd",
        "prompt": "What is the NRB USD exchange rate today?",
        "expect_tool": "get_nrb_forex",
    },
    {
        "id": "past-usd-buy-sell",
        "prompt": "What was the USD buying and selling rate according to NRB on 2025-04-01?",
        "expect_tool": "get_nrb_forex",
        "expect_args": {"from": "2025-04-01"},
        "must_say": ["136.46", "137.06"],  # the real published rate for that day
    },
    {
        "id": "range-eur",
        "prompt": "Show me the NRB EUR exchange rates between 2026-08-01 and 2026-08-05.",
        "expect_tool": "get_nrb_forex",
        "expect_args": {"from": "2026-08-01", "to": "2026-08-05"},
    },
    {
        "id": "inr-unit",
        "prompt": "What is Nepal Rastra Bank's INR rate?",
        "expect_tool": "get_nrb_forex",
        # INR is quoted per 100. The unit must survive into the answer, or the
        # number is a 100x error.
        "must_say": ["100"],
    },
    {
        "id": "non-trading-day",
        "prompt": "What were NRB's exchange rates on 2026-08-06?",
        "expect_tool": "get_nrb_forex",
        "expect_args": {"from": "2026-08-06"},
        # A public holiday: NRB published the day with every rate null. The model
        # must not invent numbers.
        "must_not_say": ["152.", "160.", "175."],
    },
    {
        "id": "negative-monetary-policy",
        "prompt": "What does Nepal Rastra Bank's monetary policy say about inflation?",
        # The forex tool must NOT be called for a policy/document question.
        "expect_tool": None,
    },
]


def _score(case: dict, calls: list[dict], answer: str) -> tuple[bool, str]:
    """Return (passed, reason) for one turn."""
    names = [c["name"] for c in calls]
    forex = [c for c in calls if c["name"] == "get_nrb_forex"]

    if case["expect_tool"] is None:
        if "get_nrb_forex" in names:
            return False, "forex tool was called for a document/policy question"
        return True, "forex tool correctly NOT called"

    if not forex:
        return False, f"forex tool NOT called (called: {names or 'nothing'})"

    args = forex[0]["args"] if isinstance(forex[0]["args"], dict) else {}
    for key, want in (case.get("expect_args") or {}).items():
        if args.get(key) != want:
            return False, f"arg {key}={args.get(key)!r}, wanted {want!r}"
    for token in case.get("must_say", []):
        if token not in answer:
            return False, f"answer is missing {token!r}"
    for token in case.get("must_not_say", []):
        if token in answer:
            return False, f"answer contains {token!r} (likely fabricated)"
    return True, "routed and answered correctly"


async def _run_case(case: dict, settings, mcp) -> dict:
    ollama = OllamaClient(settings.chat_base_url, settings.ollama_timeout)
    t0 = time.time()
    try:
        out = await run_turn(
            messages=[{"role": "user", "content": case["prompt"]}],
            ollama=ollama, mcp=mcp, settings=settings, identity=None,
        )
    finally:
        await ollama.aclose()

    calls = [
        {"name": c.get("name"), "args": c.get("arguments")}
        for step in (out.get("trace") or [])
        for c in (step.get("tool_calls") or [])
    ]
    answer = out.get("final_answer") or ""
    passed, reason = _score(case, calls, answer)
    return {
        "id": case["id"], "passed": passed, "reason": reason,
        "calls": [c["name"] for c in calls],
        "args": next((c["args"] for c in calls if c["name"] == "get_nrb_forex"), None),
        "secs": round(time.time() - t0, 1), "iters": out.get("iteration_count"),
        "answer": answer,
    }


async def main() -> int:
    settings = get_settings()
    mcp = _build_mcp_client(settings)
    repeat = int(os.environ.get("EVAL_REPEAT", "1"))

    print(f"model={settings.agent_model}  server={settings.chat_base_url}  "
          f"mcp={'on' if settings.mcp_server_url else 'off'}  repeat={repeat}")
    print("=" * 78)

    all_passed = True
    for case in CASES:
        outcomes = []
        for _ in range(repeat):
            outcomes.append(await _run_case(case, settings, mcp))
        n_pass = sum(o["passed"] for o in outcomes)
        first = outcomes[0]
        mark = "PASS" if n_pass == repeat else ("FLAKY" if n_pass else "FAIL")
        if n_pass != repeat:
            all_passed = False
        tally = "" if repeat == 1 else f" [{n_pass}/{repeat}]"
        print(f"[{mark}]{tally} {case['id']} ({first['secs']}s, {first['iters']} it) "
              f"calls={first['calls']}")
        # Show a failing example's reason and the args it produced.
        bad = next((o for o in outcomes if not o["passed"]), None)
        detail = bad or first
        if detail["args"] is not None:
            print(f"         args: {json.dumps(detail['args'])}")
        if not detail["passed"]:
            print(f"         why:  {detail['reason']}")
            print(f"         said: {detail['answer'][:160]!r}")

    print("=" * 78)
    print("RESULT:", "all cases passed" if all_passed
          else "SOME CASES FAILED — check OLLAMA_CONTEXT_LENGTH on the model server")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
