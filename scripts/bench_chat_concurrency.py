"""Measure chat throughput and latency under concurrency.

Concurrency is the whole reason for the Ollama->vLLM move, so this is the
before/after instrument. Point it at either backend by setting AGENT_BASE_URL
(blank = whatever OLLAMA_BASE_URL is) and run the SAME script both times.

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

PROMPT = "In two sentences, what is a central bank's policy rate?"


def summarize(latencies_ms: list[float], wall_seconds: float) -> dict:
    """Throughput is requests over WALL CLOCK, not the mean latency.

    Under concurrency those differ by the concurrency factor, and reporting the
    latter would make a serialising backend look identical to a batching one --
    which is exactly the difference this benchmark exists to detect.
    """
    if not latencies_ms:
        return {"n": 0, "wall_seconds": wall_seconds, "throughput_rps": 0.0,
                "p50_ms": 0.0, "p95_ms": 0.0}
    ordered = sorted(latencies_ms)
    # Nearest-rank percentile: index = ceil(p * n) - 1.
    p95_index = max(0, -(-95 * len(ordered) // 100) - 1)
    return {
        "n": len(ordered),
        "wall_seconds": round(wall_seconds, 3),
        "throughput_rps": round(len(ordered) / wall_seconds, 3) if wall_seconds else 0.0,
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
    }


async def _one_turn(client: OllamaClient, model: str) -> float:
    start = time.perf_counter()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "temperature": 0.1,
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

    async def guarded() -> float | None:
        async with semaphore:
            try:
                return await _one_turn(client, settings.agent_model)
            except Exception as exc:  # noqa: BLE001 - a failed turn is data
                print(f"request failed: {exc}", file=sys.stderr)
                return None

    started = time.perf_counter()
    results = await asyncio.gather(*[guarded() for _ in range(args.requests)])
    wall = time.perf_counter() - started
    await client.aclose()

    latencies = [r for r in results if r is not None]
    summary = summarize(latencies, wall)
    summary["label"] = args.label
    summary["backend"] = settings.chat_base_url
    summary["model"] = settings.agent_model
    summary["failed"] = len(results) - len(latencies)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
