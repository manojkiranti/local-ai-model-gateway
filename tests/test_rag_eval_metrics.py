"""The metrics themselves, on hand-built outcomes with known answers.

A broken metric reports a false pass, so the scorer is tested before it is
trusted to judge retrieval.
"""

import pytest

from app.rag.eval_metrics import Outcome, score


def ans(qid, expected, returned, abstained=False):
    return Outcome(question_id=qid, answerable=True, abstained=abstained,
                   returned_document_ids=returned, expected_document_id=expected)


def neg(qid, abstained):
    return Outcome(question_id=qid, answerable=False, abstained=abstained,
                   returned_document_ids=[], expected_document_id=None)


def test_recall_counts_the_expected_document_anywhere_in_the_results():
    r = score([ans("q1", "A", ["B", "A"]), ans("q2", "C", ["D"])])
    assert r.recall_at_k == 0.5


def test_mrr_rewards_a_higher_rank():
    first = score([ans("q1", "A", ["A", "B"])]).mrr
    second = score([ans("q1", "A", ["B", "A"])]).mrr
    assert first == 1.0
    assert second == 0.5


def test_a_miss_contributes_zero_to_mrr():
    assert score([ans("q1", "A", ["B", "C"])]).mrr == 0.0


def test_abstention_recall_is_over_the_unanswerable_questions_only():
    r = score([neg("n1", True), neg("n2", False), ans("q1", "A", ["A"])])
    assert r.abstention_recall == 0.5


def test_false_refusal_rate_is_over_the_answerable_questions_only():
    # The number that governs the operating point: refusing a question the
    # corpus answers is worse than answering it imperfectly.
    r = score([ans("q1", "A", [], abstained=True), ans("q2", "B", ["B"]),
               neg("n1", False)])
    assert r.false_refusal_rate == 0.5


def test_an_abstained_answerable_question_is_not_also_counted_as_recall():
    r = score([ans("q1", "A", [], abstained=True)])
    assert r.recall_at_k == 0.0
    assert r.false_refusal_rate == 1.0


def test_counts_are_reported_so_a_rate_can_be_read_in_context():
    r = score([ans("q1", "A", ["A"]), neg("n1", True), neg("n2", True)])
    assert (r.answerable, r.unanswerable) == (1, 2)


def test_no_questions_of_a_kind_yields_zero_not_a_crash():
    r = score([ans("q1", "A", ["A"])])
    assert r.abstention_recall == 0.0
    assert r.unanswerable == 0


def test_an_empty_outcome_set_is_rejected():
    with pytest.raises(ValueError):
        score([])
