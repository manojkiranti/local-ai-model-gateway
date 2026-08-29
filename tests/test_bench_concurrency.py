"""The summary maths must be right, or the go/no-go compares two wrong numbers."""

from scripts.bench_chat_concurrency import PROMPTS, _prompt_for, summarize


def test_both_percentiles_are_nearest_rank_observed_values():
    # 20 samples: nearest-rank p95 is the 19th value (19.0), p50 the 10th
    # (10.0) — both actually-observed latencies. p50 is NOT statistics.median
    # (which would interpolate the 10th and 11th to 10.5, a value that never
    # occurred); the two percentiles must be the same kind of number so a
    # reader can diff them across an Ollama-vs-vLLM run.
    latencies = [float(i) for i in range(1, 21)]
    out = summarize(latencies, wall_seconds=2.0)
    assert out["p95_ms"] == 19.0
    assert out["p50_ms"] == 10.0


def test_p50_nearest_rank_on_even_n_takes_the_lower_middle_not_an_average():
    # n=2: nearest-rank p50 index = ceil(0.5*2)-1 = 0 -> the LOWER value.
    # statistics.median would have returned 15.0 (the average); nearest-rank
    # returns 10.0, an observed sample.
    out = summarize([10.0, 20.0], wall_seconds=1.0)
    assert out["p50_ms"] == 10.0


def test_throughput_is_requests_over_wall_clock_not_sum_of_latencies():
    # 10 concurrent requests of 1s each finishing in 1s wall = 10 rps, not 1.
    out = summarize([1000.0] * 10, wall_seconds=1.0)
    assert out["throughput_rps"] == 10.0
    assert out["n"] == 10


def test_an_empty_run_reports_zero_rather_than_dividing_by_zero():
    out = summarize([], wall_seconds=0.0)
    assert out["n"] == 0
    assert out["throughput_rps"] == 0.0


def test_attempted_defaults_to_n_when_there_were_no_failures():
    # No `attempted` passed in => nothing failed => attempted == n.
    out = summarize([1000.0] * 5, wall_seconds=1.0)
    assert out["attempted"] == 5
    assert out["n"] == 5


def test_attempted_can_exceed_n_when_requests_failed():
    """Finding #4c: throughput_rps still divides successes by wall clock, but
    `attempted` must be reported alongside it so a high-failure run can't be
    misread as a fast one — comparing `n` to `attempted` (or the caller's
    separately printed `failed` count) is what exposes that.
    """
    out = summarize([1000.0] * 3, wall_seconds=1.0, attempted=10)
    assert out["n"] == 3
    assert out["attempted"] == 10
    assert out["throughput_rps"] == 3.0  # successes / wall clock, unchanged


def test_the_expected_keys_the_cutover_plan_consumes_are_still_present():
    out = summarize([1000.0] * 3, wall_seconds=1.0)
    for key in ("n", "wall_seconds", "throughput_rps", "p50_ms", "p95_ms"):
        assert key in out


def test_prompts_rotate_deterministically_and_are_not_a_single_repeated_string():
    """Finding #4a: an identical prompt on every request lets vLLM's default
    prefix caching produce cache hits Ollama structurally cannot get, which
    would measure caching rather than concurrency/batching. The rotation must
    also be deterministic (no randomness) so separate runs stay comparable.
    """
    assert len(PROMPTS) > 1
    seen = {_prompt_for(i) for i in range(len(PROMPTS) * 2)}
    assert len(seen) == len(PROMPTS)  # every prompt in the set gets used
    # Deterministic: same index always yields the same prompt.
    assert _prompt_for(0) == _prompt_for(len(PROMPTS))
    assert _prompt_for(3) == _prompt_for(3)
