"""The summary maths must be right, or the go/no-go compares two wrong numbers."""

from scripts.bench_chat_concurrency import summarize


def test_p95_picks_the_nearest_rank_not_the_max():
    # 20 samples: p95 is the 19th value (nearest-rank), not the 20th.
    latencies = [float(i) for i in range(1, 21)]
    out = summarize(latencies, wall_seconds=2.0)
    assert out["p95_ms"] == 19.0
    assert out["p50_ms"] == 10.5


def test_throughput_is_requests_over_wall_clock_not_sum_of_latencies():
    # 10 concurrent requests of 1s each finishing in 1s wall = 10 rps, not 1.
    out = summarize([1000.0] * 10, wall_seconds=1.0)
    assert out["throughput_rps"] == 10.0
    assert out["n"] == 10


def test_an_empty_run_reports_zero_rather_than_dividing_by_zero():
    out = summarize([], wall_seconds=0.0)
    assert out["n"] == 0
    assert out["throughput_rps"] == 0.0
