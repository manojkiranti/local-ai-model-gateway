#!/usr/bin/env python
"""Live routing eval: do DOCUMENT questions still reach the corpus?

The counterpart to `eval_nrb_forex_routing.py`, and the guard for one specific
regression: **adding a tool must not stop a document question reaching
`search_department_docs`.** Retrieval quality is measured elsewhere
(`rag_eval_sweep.py`); this measures whether retrieval is CONSULTED at all,
which no amount of retrieval tuning can fix if the model never calls the tool.

WHY THIS EXISTS
    Every tool schema is re-sent on every turn, and every added tool lengthens
    the menu the model routes from. That cost is invisible in unit tests — each
    tool passes its own suite while collectively crowding out the one that
    matters. Run this after adding a tool, editing a description, changing
    CONTEXT_TOOL_SCHEMA_TOKENS, or swapping the model.

    Measured 2026-08-29 (qwen2.5:latest, local, MCP off), A/B against the same
    cases with `edit_excel` and `nepali_date` removed: routing was IDENTICAL on
    every document case, so those two tools cost RAG nothing. Re-measured after
    `read_department_doc`: 7/7, including the `bs-dated-doc` case that had missed
    on every registry until then. The A/B flag below is how both of those were
    established, and it caught a real regression in between — see KNOWN_MISSES.

USAGE
    .venv/bin/python scripts/eval_rag_routing.py
    MCP_SERVER_URL= .venv/bin/python scripts/eval_rag_routing.py     # local tools only
    EVAL_REPEAT=3 .venv/bin/python scripts/eval_rag_routing.py       # routing is flaky
    DROP_TOOLS=edit_excel,nepali_date .venv/bin/python scripts/eval_rag_routing.py   # A/B

It calls the real agent loop with a bound department, so it needs a reachable
model server. It does NOT need the corpus to contain anything: the question is
which TOOL the model reaches for, not what comes back.

Exit code is 0 only if every case meets its expectation on every repeat, with
KNOWN_MISSES excluded — a case listed there is a documented weakness, and the
run fails if one of them starts PASSING, so the list cannot rot silently.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.loop import run_turn  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import _build_mcp_client  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402
from app.rag.context import DepartmentContext, rag_context  # noqa: E402

RAG_TOOL = "search_department_docs"

# Document-shaped questions, several phrased to tempt a specific other tool.
CASES = [
    ("plain-policy", "What does our monetary policy say about the CRR requirement?", RAG_TOOL),
    # Tempts nepali_date: NRB files documents BY fiscal year.
    ("fiscal-year-doc", "What circulars were issued in fiscal year 2082/83?", RAG_TOOL),
    # Tempts read_document: names a document by date, with nothing attached.
    ("bs-dated-doc", "Summarise the directive published on 2082-01-15.", RAG_TOOL),
    # Tempts the spreadsheet tools.
    ("spreadsheet-doc", "What are the loan limits in the product sheet?", RAG_TOOL),
    ("nepali-year-doc", "What did NRB say about digital lending in 2082?", RAG_TOOL),
    # Controls: these SHOULD leave the corpus alone.
    ("date-only-control", "What is 2082-01-31 in the English calendar?", "nepali_date"),
    ("rate-control", "What is today's NRB buying rate for USD?", "get_nrb_forex"),
]

# Documented weaknesses, excluded from the exit code. A case here that starts
# passing FAILS the run, so a fix cannot go unnoticed and the list cannot rot.
#
# EMPTY as of 2026-08-29. `bs-dated-doc` used to live here: asked to summarise a
# document it was never given, the model invented a file_id, called
# read_document, and asked the user to upload a file the corpus already held.
# Two description edits and a stronger imperative had failed to move it. What
# fixed it was giving the model somewhere to GO — `read_department_doc` plus the
# clause in `search_department_docs`'s description naming it. Measured 3/3 both
# with the tool registered and with it dropped, so the DESCRIPTION did the work:
# once "read one of these documents in full" existed as a sentence, the corpus
# became the obvious route for "summarise the directive published on <date>".
#
# The same edit taught a second lesson, the hard way. Appended AFTER the
# spreadsheet clause it took `spreadsheet-doc` from 2/3 to 0/3; moved BEFORE it,
# both went 3/3. Position inside a description is load-bearing — a routing hint
# pushed to the end stops being read. Re-run this eval after ANY description
# edit, not just after adding a tool.
KNOWN_MISSES: set[str] = set()

# Cases whose QUESTION is genuinely ambiguous, so unanimity is the wrong bar.
# They must still reach the corpus a MAJORITY of the time — a collapse to 0 is
# a real regression and still fails — but demanding 3/3 from a borderline
# question turns this eval into an alarm nobody trusts.
#
# spreadsheet-doc ("what are the loan limits in the product sheet?"): "product
# sheet" reads as a spreadsheet, and the model sometimes reaches for
# inspect_excel. Measured at 2/3 BEFORE `read_department_doc` existed, so the
# flakiness is the question's, not any tool's. It is kept because it is a real
# user phrasing, and because its 0/3 collapse is what caught the description
# ordering bug above.
BORDERLINE = {"spreadsheet-doc"}


async def _run_case(prompt: str, settings, mcp) -> tuple[list[str], float]:
    ollama = OllamaClient(settings.chat_base_url, settings.ollama_timeout)
    t0 = time.time()
    try:
        with rag_context(DepartmentContext(id=1, code="eval")):
            out = await run_turn(
                messages=[{"role": "user", "content": prompt}],
                ollama=ollama, mcp=mcp, settings=settings, identity=None,
            )
    finally:
        await ollama.aclose()
    calls = [
        c.get("name")
        for step in (out.get("trace") or [])
        for c in (step.get("tool_calls") or [])
    ]
    return calls, time.time() - t0


async def main() -> int:
    settings = get_settings()
    mcp = _build_mcp_client(settings)
    repeat = int(os.environ.get("EVAL_REPEAT", "1"))

    drop = {n for n in (os.environ.get("DROP_TOOLS") or "").split(",") if n}
    if drop:
        import app.tools.local as local_tools

        local_tools.LOCAL_TOOLS[:] = [
            t for t in local_tools.LOCAL_TOOLS if t.name not in drop
        ]

    import app.tools.local as local_tools

    print(f"model={settings.agent_model}  server={settings.chat_base_url}  "
          f"mcp={'on' if settings.mcp_server_url else 'off'}  "
          f"tools={len(local_tools.LOCAL_TOOLS)}  repeat={repeat}"
          + (f"  dropped={sorted(drop)}" if drop else ""))
    print("=" * 78)

    failed = False
    for case_id, prompt, expected in CASES:
        hits = 0
        first_calls: list[str] = []
        for i in range(repeat):
            calls, secs = await _run_case(prompt, settings, mcp)
            if i == 0:
                first_calls, first_secs = calls, secs
            hits += expected in calls
        known = case_id in KNOWN_MISSES
        passed = hits == repeat
        if known:
            # A known miss that starts passing is news, and must not stay hidden.
            mark = "FIXED!" if hits else "known"
            if hits:
                failed = True
        elif case_id in BORDERLINE:
            # Majority, not unanimity. Zero is still a failure.
            ok = hits * 2 > repeat
            mark = "PASS" if passed else ("ok~" if ok else "FAIL")
            if not ok:
                failed = True
        else:
            mark = "PASS" if passed else ("FLAKY" if hits else "FAIL")
            if not passed:
                failed = True
        tally = "" if repeat == 1 else f" [{hits}/{repeat}]"
        print(f"[{mark:6s}]{tally} {case_id:18s} ({first_secs:4.1f}s) "
              f"want={expected} calls={first_calls}")

    print("=" * 78)
    if failed:
        print("RESULT: FAILED — a document question did not reach the corpus, or a "
              "KNOWN_MISS started passing (update KNOWN_MISSES).")
    else:
        print(f"RESULT: routing intact ({len(KNOWN_MISSES)} known miss(es) excluded)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
