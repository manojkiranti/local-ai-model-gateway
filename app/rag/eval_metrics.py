"""Retrieval eval metrics. Pure -- no database, no model, no cohort loading.

Four numbers, and they are not equally important. **False-refusal rate governs
the operating point**: an assistant that refuses questions the corpus answers
reads to users as broken, and that is worse than the over-confidence it
replaces -- over-confidence yields a wrong answer the user may catch, a false
refusal denies a correct answer the corpus contained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Outcome:
    """What one cohort question actually produced."""

    question_id: str
    answerable: bool
    abstained: bool
    # Document ids in the order the tool presented them, best first.
    returned_document_ids: list[str]
    expected_document_id: Optional[str]


@dataclass(frozen=True)
class Report:
    recall_at_k: float
    mrr: float
    abstention_recall: float
    false_refusal_rate: float
    answerable: int
    unanswerable: int


def _ratio(hits: int, total: int) -> float:
    # A kind with no questions is 0.0, not a division error: a sweep row that
    # crashed would be read as "no data" anyway, and a crash loses the other
    # three numbers with it.
    return (hits / total) if total else 0.0


def score(outcomes: Sequence[Outcome]) -> Report:
    if not outcomes:
        raise ValueError("no outcomes to score -- the cohort ran zero questions")

    answerable = [o for o in outcomes if o.answerable]
    unanswerable = [o for o in outcomes if not o.answerable]

    recall_hits = 0
    reciprocal = 0.0
    for outcome in answerable:
        if outcome.abstained or outcome.expected_document_id is None:
            # An abstention retrieved nothing the model could use, so it cannot
            # also count as a retrieval success.
            continue
        ids = outcome.returned_document_ids
        if outcome.expected_document_id in ids:
            recall_hits += 1
            reciprocal += 1.0 / (ids.index(outcome.expected_document_id) + 1)

    return Report(
        recall_at_k=_ratio(recall_hits, len(answerable)),
        mrr=(reciprocal / len(answerable)) if answerable else 0.0,
        abstention_recall=_ratio(
            sum(1 for o in unanswerable if o.abstained), len(unanswerable)
        ),
        false_refusal_rate=_ratio(
            sum(1 for o in answerable if o.abstained), len(answerable)
        ),
        answerable=len(answerable),
        unanswerable=len(unanswerable),
    )
