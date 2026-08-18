"""Whole-line paging shared by the read tools. Not a tool — no SPEC here.

Both `read_document` and `read_image` face the same problem: `agent/loop.py`
truncates any tool result over `MAX_TOOL_RESULT_CHARS` from the END. So:

  * METADATA LEADS. The header (totals, and the exact `start_line=` to resume
    from) goes FIRST, because that is the part which must survive the loop's cut.
    `read_excel` puts its continuation note last, which is exactly where the cut
    lands.
  * WE TRUNCATE FIRST, on whole lines. If the loop cut the body instead, the
    header would promise "continue at line 401" while the model only ever saw
    line 90 — a silent hole that reads as a complete result.

Extracted so the two tools cannot drift apart on the off-by-one: `last + 1` is
the resume point precisely because a line that would cross the budget is dropped
whole rather than sliced.
"""

from __future__ import annotations

from typing import Optional

# Must equal agent.loop.MAX_TOOL_RESULT_CHARS. NOT imported from there: the
# agent imports the tool registry, so a tools -> agent import is circular.
# tests/test_read_document_tool.py asserts the two agree.
MODEL_RESULT_CAP = 8000
HEADER_BUDGET = 400  # room for the metadata block


def window(
    lines: list[str],
    start_line: int,
    max_lines: Optional[int],
    *,
    line_cap: int,
    char_budget: int,
) -> tuple[list[str], int, bool, Optional[tuple[int, int]]]:
    """Return (window, last_line_number, truncated, hard_cut).

    Truncation is on WHOLE lines: a line that would cross the budget is dropped
    entirely, so `last_line_number` is exactly what the model received and
    `last_line_number + 1` is exactly where it should resume.

    `hard_cut` is `(line_number, original_length)` when a single line longer
    than the ENTIRE character budget had to be cut mid-line, else `None`. That
    case is invisible to `truncated` when it is also the LAST line: there is no
    next line to resume at, so `last < len(lines)` is False even though real
    content was dropped. The caller uses `hard_cut` to say so regardless.
    """
    start = max(1, start_line)
    index = start - 1
    cap = line_cap
    if max_lines is not None:
        cap = max(1, min(int(max_lines), line_cap))
    selected = lines[index : index + cap]

    out: list[str] = []
    used = 0
    hard_cut: Optional[tuple[int, int]] = None
    for offset, line in enumerate(selected):
        cost = len(line) + 1  # + the newline that joins it
        if not out and cost > char_budget:
            # A single line longer than the entire budget. Emit it alone and
            # hard-cut, or the reader could never make progress past it.
            out.append(line[:char_budget] + " …[long line truncated]")
            hard_cut = (index + offset + 1, len(line))
            break
        if used + cost > char_budget:
            break
        out.append(line)
        used += cost

    last = index + len(out)
    return out, last, last < len(lines), hard_cut
