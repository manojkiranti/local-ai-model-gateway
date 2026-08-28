"""Measure chat throughput and latency under concurrency.

Concurrency is the whole reason for the Ollama->vLLM move, so this is the
before/after instrument. Point it at either backend by setting AGENT_BASE_URL
(blank = whatever OLLAMA_BASE_URL is) and run the SAME script both times.

Three things this benchmark deliberately avoids, because a biased instrument
produces a wrong cutover decision:
  - The PROMPT is varied per request (a small rotating set, deterministic
    across runs) rather than sent identically every time. Recent vLLM enables
    automatic prefix caching by default, so an identical prompt gives vLLM
    cache hits Ollama structurally cannot get — that would measure caching,
    not concurrency/batching.
  - Every request sends a `max_tokens` cap, so requests/sec measures request
    handling under load rather than how verbose each backend's model happens
    to be.
  - `throughput_rps` still divides SUCCESSFUL requests by wall clock (a
    backend that fails fast under load must not look "faster" than one that
    completes the work), but the summary also reports `attempted` and
    `failed` and the CLI prints a loud caveat whenever any request failed, so
    a high-failure run can't be misread as a fast one.

Usage:
    .venv/bin/python scripts/bench_chat_concurrency.py --concurrency 10 --requests 20
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402

# A small rotating set of prompts, not one repeated string — see the module
# docstring. Deterministic (no randomness) so runs stay comparable across
# backends and across time.
PROMPTS = [
    "In two sentences, what is a central bank's policy rate?",
    "In two sentences, what does a bank's reserve requirement do?",
    "In two sentences, what is the difference between fiscal and monetary policy?",
    "In two sentences, what causes currency depreciation?",
    "In two sentences, what is the purpose of a lender of last resort?",
]
MAX_TOKENS = 200


def _prompt_for(index: int) -> str:
    return PROMPTS[index % len(PROMPTS)]


def summarize(
    latencies_ms: list[float], wall_seconds: float, attempted: int | None = None
) -> dict:
    """Throughput is SUCCESSFUL requests over WALL CLOCK, not the mean latency.

    Under concurrency those differ by the concurrency factor, and reporting the
    latter would make a serialising backend look identical to a batching one --
    which is exactly the difference this benchmark exists to detect.

    `attempted` (defaults to `len(latencies_ms)` when omitted, i.e. no
    failures) is reported alongside `throughput_rps` precisely so a run with
    failures cannot be misread as fast: dividing only successes by wall clock
    means a backend that fails requests quickly under load would otherwise
    look identical to one that completed the same count for real — comparing
    `n` to `attempted` (or reading the caller's printed `failed` count) is
    what catches that.
    """
    n = len(latencies_ms)
    if attempted is None:
        attempted = n
    if not latencies_ms:
        return {
            "n": 0,
            "attempted": attempted,
            "wall_seconds": wall_seconds,
            "throughput_rps": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
        }
    ordered = sorted(latencies_ms)
    # Nearest-rank percentile: index = ceil(p * n) - 1.
    p95_index = max(0, -(-95 * len(ordered) // 100) - 1)
    return {
        "n": n,
        "attempted": attempted,
        "wall_seconds": round(wall_seconds, 3),
        "throughput_rps": round(n / wall_seconds, 3) if wall_seconds else 0.0,
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
    }


async def _one_turn(client: OllamaClient, model: str, index: int) -> float:
    start = time.perf_counter()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _prompt_for(index)}],
        "stream": True,
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
    }
    async for _event in client.stream_chat(payload):
        pass
    return (time.perf_counter() - start) * 1000.0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--label", default="", help="e.g. ollama-baseline / vllm")
    args = parser.parse_args()

    settings = get_settings()
    client = OllamaClient(settings.chat_base_url, settings.ollama_timeout)
    print(f"backend={settings.chat_base_url} model={settings.agent_model} "
          f"concurrency={args.concurrency} requests={args.requests}", file=sys.stderr)

    semaphore = asyncio.Semaphore(args.concurrency)

    async def guarded(index: int) -> float | None:
        async with semaphore:
            try:
                return await _one_turn(client, settings.agent_model, index)
            except Exception as exc:  # noqa: BLE001 - a failed turn is data
                print(f"request failed: {exc}", file=sys.stderr)
                return None

    try:
        started = time.perf_counter()
        results = await asyncio.gather(*[guarded(i) for i in range(args.requests)])
        wall = time.perf_counter() - started
    finally:
        # try/finally so a KeyboardInterrupt or a cancelled gather still
        # closes the client instead of leaking its connection pool.
        await client.aclose()

    latencies = [r for r in results if r is not None]
    failed = len(results) - len(latencies)
    summary = summarize(latencies, wall, attempted=len(results))
    summary["label"] = args.label
    summary["backend"] = settings.chat_base_url
    summary["model"] = settings.agent_model
    summary["failed"] = failed
    print(json.dumps(summary, indent=2))
    if failed:
        # Loud on purpose: throughput_rps divides SUCCESSES by wall clock, so
        # a backend that fails fast under load can otherwise print a number
        # that reads as "fast" when it actually did less work. Compare `n`
        # to `attempted` before trusting throughput_rps from a failing run.
        print(
            f"CAVEAT: {failed}/{len(results)} requests failed — throughput_rps "
            "above is successes-per-wall-clock-second and is NOT comparable "
            "to a run with fewer/no failures. Fix the failure cause before "
            "using this run for a go/no-go decision.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
