# vLLM Server Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the chat/agent model from Ollama to vLLM on the live GPU box for concurrency, side-by-side and reversible, without disturbing the other team's production stack.

**Architecture:** vLLM runs as a second model server on a *new* port beside the existing `nic_ollama`. The gateway's chat client is repointed with `AGENT_BASE_URL` (already implemented, commit `9bfc190`); embeddings and the reranker keep talking to Ollama on `:11434`. Ollama stays up throughout, so rollback is a one-line config revert rather than a re-pull.

**Tech Stack:** vLLM OpenAI-compatible server (Docker, NVIDIA runtime), 2× A40 (Ampere sm_86) with `--tensor-parallel-size 2`, Qwen3.5 35B MoE in HF safetensors; gateway is FastAPI + httpx on Python 3.10.

**Spec:** `docs/ollama-to-vllm-migration.md` — read it before executing. This plan implements its §8 runbook and §9 validation; the spec carries the *why*, the VRAM math (§5), and the silent-failure watch list (§11).

## Global Constraints

- **The gateway-side code change is already DONE** (`feat/vllm`, commit `9bfc190`). `AGENT_BASE_URL` blank ⇒ falls back to `OLLAMA_BASE_URL`. Do not re-implement it.
- **`nic_ollama`, `nic_postgres`, `nic_qdrant` belong to another team.** Never stop, restart, recreate, or edit `/home/localllm/backend-local/docker-compose.yml`. Read-only inspection is fine.
- **vLLM binds a NEW port. Never `:11434`.** This plan uses `8100`; if taken, pick another and use it consistently.
- **`--max-model-len` MUST equal the gateway's `CONTEXT_WINDOW_TOKENS`** (currently `32768`). A mismatch is silent (spec §11).
- **A40 is Ampere sm_86 — no FP8.** Never pass `--kv-cache-dtype fp8` or use an FP8 checkpoint. AWQ/GPTQ INT4 and BF16 are the valid options.
- **Tool calling requires `--enable-auto-tool-choice` AND a `--tool-call-parser`.** Without both, vLLM returns tool syntax as plain text and every tool turn breaks silently.
- **Never paste a private key, `HF_TOKEN`, or any credential into chat.** SSH via a `~/.ssh/config` host alias; secrets live on the server or in env files.
- **Stop at every GATE.** Gates are human decision points, not checkpoints to narrate past.
- **Rollback is always:** unset `AGENT_BASE_URL`, restore `AGENT_MODEL=qwen3.5:35b-a3b`, restart the gateway.

**Placeholders you must substitute:**
`<SSH_HOST>` = the ssh config alias · `<VLLM_PORT>` = `8100` unless taken · `<HF_REPO>` = resolved in Task 2 · `<PARSER>` = resolved in Task 2 · `<VLLM_IMAGE>` = resolved in Task 2 · `<SERVED_NAME>` = `qwen35-chat`

---

## Task 1: Concurrency benchmark (the only code task)

Concurrency is the entire justification for this migration. Without a baseline captured *before* vLLM competes for VRAM, there is no way to prove the cutover helped. Same script measures both sides, so the comparison is honest.

**Files:**
- Create: `scripts/bench_chat_concurrency.py`
- Create: `tests/test_bench_concurrency.py`

**Interfaces:**
- Consumes: `app.config.get_settings` (for `chat_base_url`, `agent_model`), `app.ollama.client.OllamaClient`.
- Produces: `summarize(latencies_ms: list[float], wall_seconds: float) -> dict` returning keys `n`, `wall_seconds`, `throughput_rps`, `p50_ms`, `p95_ms`. Task 8 reads this output.

- [ ] **Step 1: Write the failing test**

```python
"""The summary maths must be right, or the go/no-go compares two wrong numbers."""

from scripts.bench_chat_concurrency import summarize


def test_p95_picks_the_nearest_rank_not_the_max():
    # 20 samples: p95 is the 19th value (nearest-rank), not the 20th.
    latencies = [float(i) for i in range(1, 21)]
    out = summarize(latencies, wall_seconds=2.0)
    assert out["p95_ms"] == 19.0
    assert out["p50_ms"] == 10.0   # nearest-rank p50 of 1..20 (both percentiles nearest-rank)


def test_throughput_is_requests_over_wall_clock_not_sum_of_latencies():
    # 10 concurrent requests of 1s each finishing in 1s wall = 10 rps, not 1.
    out = summarize([1000.0] * 10, wall_seconds=1.0)
    assert out["throughput_rps"] == 10.0
    assert out["n"] == 10


def test_an_empty_run_reports_zero_rather_than_dividing_by_zero():
    out = summarize([], wall_seconds=0.0)
    assert out["n"] == 0
    assert out["throughput_rps"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_bench_concurrency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.bench_chat_concurrency'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/bench_chat_concurrency.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_bench_concurrency.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Confirm the script runs against local Ollama**

Run: `.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 2 --requests 2 --label smoke`
Expected: JSON with `"n": 2`, `"failed": 0`. If Ollama isn't running locally, `failed: 2` is acceptable here — the point is the script executes.

- [ ] **Step 6: Commit**

```bash
git add scripts/bench_chat_concurrency.py tests/test_bench_concurrency.py
git commit -m "feat(bench): measure chat throughput and latency under concurrency"
```

---

## Task 2: Establish SSH access and resolve the three blocking unknowns

Read-only. Nothing on the server changes. This task answers the questions the spec §14 leaves open; **no VRAM is consumed and no container is touched.**

**Files:** none (findings recorded in Step 7).

**Interfaces:**
- Produces: `<HF_REPO>`, `<PARSER>`, `<VLLM_IMAGE>`, and the VRAM headroom figure that Task 4 uses to choose BF16 vs INT4.

- [ ] **Step 1: Verify SSH works from this environment**

Run: `ssh <SSH_HOST> 'echo ok && hostname && nvidia-smi -L'`
Expected: `ok`, the hostname, and two A40 lines.
**If this fails** (the Bash tool is sandboxed and may block outbound SSH): stop. Ask the human to run each subsequent command themselves with `! <command>` and paste output. The plan still works; only who types it changes.

- [ ] **Step 2: Record GPU headroom — this decides BF16 vs INT4**

```bash
ssh <SSH_HOST> 'nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv'
ssh <SSH_HOST> 'docker exec nic_ollama ollama ps'
```
Expected: per-GPU used/total in MiB, and `PROCESSOR` = `100% GPU`.
**Record `memory.total - memory.used` per card.** Task 4 needs it.

- [ ] **Step 3: Resolve the exact model identity**

```bash
ssh <SSH_HOST> 'docker exec nic_ollama ollama list'
ssh <SSH_HOST> 'docker exec nic_ollama ollama show qwen3.5:35b-a3b --modelfile'
```
Read the `FROM` line and any parameter/template block. Cross-check the matching repo on Hugging Face.
**Do not guess `<HF_REPO>`** — a wrong repo means downloading ~70 GB of the wrong weights. If the modelfile is ambiguous, ask the human before proceeding.

- [ ] **Step 4: Confirm the host can run the vLLM image**

```bash
ssh <SSH_HOST> 'nvidia-smi | head -4; docker --version; df -h /var/lib/docker'
```
Expected: driver `580.173.02`, CUDA `13.0`, and **≥ 120 GB free** for weights + image.
Pin `<VLLM_IMAGE>` to a specific tag (never `latest`) and confirm its CUDA/driver floor is satisfied by this host.

- [ ] **Step 5: Determine the tool-call parser**

Check the pinned vLLM version's docs for the parser matching this model family. `hermes` is the usual answer for Qwen3-family; newer vLLM may ship a model-specific parser. Also check whether a `--chat-template` file must be passed explicitly.
Record as `<PARSER>`. **This is verified empirically in Task 5, not trusted here.**

- [ ] **Step 6: Confirm the chosen port is free**

Run: `ssh <SSH_HOST> 'ss -lntp | grep -E ":(8100|11434) " || echo "8100 free"'`
Expected: `11434` in use (Ollama), `8100` free. If taken, choose another port and use it consistently from here on.

- [ ] **Step 7: Record findings**

Append a `## Cutover log` section to `docs/ollama-to-vllm-migration.md` with: date, per-GPU free VRAM, `<HF_REPO>`, `<VLLM_IMAGE>`, `<PARSER>`, `<VLLM_PORT>`, and the BF16-vs-INT4 decision with its reasoning.

```bash
git add docs/ollama-to-vllm-migration.md
git commit -m "docs(vllm): record server survey findings and the quantization decision"
```

- [ ] **GATE — human decision.** Present: free VRAM per card, the resolved repo, and the BF16/INT4 recommendation. **Do not start a download or a container until the human approves.**

---

## Task 3: Capture the Ollama concurrency baseline

Must happen **before** vLLM allocates any VRAM, or the baseline is measured against a degraded Ollama and the comparison is worthless.

**Files:** `docs/ollama-to-vllm-migration.md` (append results).

- [ ] **Step 1: Point the gateway at the server's Ollama**

In the gateway `.env`, confirm `AGENT_BASE_URL` is **blank/absent** and set `OLLAMA_BASE_URL=http://<SERVER_HOST>:11434`, `AGENT_MODEL=qwen3.5:35b-a3b`.

- [ ] **Step 2: Verify the gateway resolved the intended backend**

Start the gateway and read the startup log.
Run: `.venv/bin/uvicorn app.main:app --port 8000`
Expected log line: `chat backend: http://<SERVER_HOST>:11434 (model qwen3.5:35b-a3b)`
**If it says `localhost`, the env did not take** — fix before measuring.

- [ ] **Step 3: Warm the model, then measure at three concurrency levels**

```bash
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 1 --requests 3 --label warmup
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 1  --requests 10 --label ollama-c1  | tee /tmp/ollama-c1.json
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 5  --requests 20 --label ollama-c5  | tee /tmp/ollama-c5.json
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 10 --requests 30 --label ollama-c10 | tee /tmp/ollama-c10.json
```
Expected: `"failed": 0` on all three. A nonzero `failed` means the baseline is unreliable — investigate before continuing.

- [ ] **Step 4: Capture the tool-routing baseline**

Run: `.venv/bin/python scripts/eval_nrb_forex_routing.py`
Record the pass rate. This is the guardrail for tool-calling correctness (spec §10.2).

- [ ] **Step 5: Commit the baseline**

Append the three JSON summaries and the routing pass rate to the `## Cutover log`.

```bash
git add docs/ollama-to-vllm-migration.md
git commit -m "docs(vllm): record the Ollama concurrency baseline before cutover"
```

---

## Task 4: Stand up vLLM side-by-side

The first step that consumes VRAM. Ollama stays running.

**Files:** Create on the server: `/home/<you>/vllm/docker-compose.yml` — **our own file, in our own directory.** Never edit the other team's compose.

- [ ] **Step 1: Pre-download the weights (backgrounded)**

A ~70 GB pull exceeds any command timeout — run it detached and poll.

```bash
ssh <SSH_HOST> 'mkdir -p ~/vllm/hf-cache'
ssh <SSH_HOST> 'nohup docker run --rm -v ~/vllm/hf-cache:/root/.cache/huggingface \
  -e HF_TOKEN <VLLM_IMAGE> \
  huggingface-cli download <HF_REPO> > ~/vllm/download.log 2>&1 &'
```
Poll: `ssh <SSH_HOST> 'tail -3 ~/vllm/download.log; du -sh ~/vllm/hf-cache'`
Expected: size stabilises near the model's published footprint.

- [ ] **Step 2: Write our compose file**

```yaml
# ~/vllm/docker-compose.yml — OUR stack. Does not touch nic_* services.
services:
  vllm-chat:
    image: <VLLM_IMAGE>
    container_name: vllm_chat
    runtime: nvidia
    ipc: host                      # tensor-parallel needs shared memory
    ports:
      - "<VLLM_PORT>:8000"
    volumes:
      - ~/vllm/hf-cache:/root/.cache/huggingface
    environment:
      HF_HUB_OFFLINE: "1"          # weights are already local
    command: >
      --model <HF_REPO>
      --served-model-name <SERVED_NAME>
      --tensor-parallel-size 2
      --max-model-len 32768
      --gpu-memory-utilization 0.45
      --enable-auto-tool-choice
      --tool-call-parser <PARSER>
    restart: unless-stopped
```

`--gpu-memory-utilization 0.45` is the **conservative overlap value** from spec §5. Raise it only after Ollama's chat load is gone.

- [ ] **Step 2b: Sanity-check the flags before launching**

Confirm against the pinned image: every flag exists, `--max-model-len` is `32768` (matching `CONTEXT_WINDOW_TOKENS`), no FP8 anywhere, and the port is not `11434`.

- [ ] **Step 3: Launch and watch VRAM as it loads**

```bash
ssh <SSH_HOST> 'cd ~/vllm && docker compose up -d vllm-chat'
ssh <SSH_HOST> 'watch -n2 nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv'
```
**ABORT IMMEDIATELY** (`docker compose down`) if either card approaches its total while `nic_ollama` is resident — that is the failure mode that takes down the other team's app.

- [ ] **Step 4: Wait for readiness**

```bash
ssh <SSH_HOST> 'curl -sf http://localhost:<VLLM_PORT>/health && echo READY'
ssh <SSH_HOST> 'curl -s http://localhost:<VLLM_PORT>/v1/models | head -40'
```
Expected: `READY`, and `<SERVED_NAME>` in the model list. Before load completes these may hang or 404 — that is "not ready", not "broken". If it exits instead, read `docker logs vllm_chat`.

- [ ] **Step 5: Confirm Ollama is unharmed**

```bash
ssh <SSH_HOST> 'docker exec nic_ollama ollama ps'
ssh <SSH_HOST> 'curl -sf http://localhost:11434/v1/models >/dev/null && echo "ollama still ok"'
```
Expected: still `100% GPU`, still answering. **If Ollama has spilled to CPU or died, roll back Task 4 now** (`docker compose down`) and re-plan with INT4.

- [ ] **GATE — human decision.** Report VRAM after load on both cards, vLLM readiness, and Ollama's health. Get approval before any gateway traffic is sent.

---

## Task 5: Smoke-test vLLM directly (before the gateway sees it)

Catches a broken tool-call parser while the blast radius is still one curl.

- [ ] **Step 1: Non-streaming completion**

```bash
ssh <SSH_HOST> 'curl -s http://localhost:<VLLM_PORT>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"<SERVED_NAME>\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" | head -40'
```
Expected: JSON with `choices[0].message.content`, `finish_reason: "stop"`.

- [ ] **Step 2: Streaming**

Same request with `"stream":true`. Expected: multiple `data: {...}` SSE lines then `data: [DONE]`.

- [ ] **Step 3: The tool-call test — the make-or-break**

```bash
ssh <SSH_HOST> 'curl -s http://localhost:<VLLM_PORT>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"<SERVED_NAME>\",
       \"messages\":[{\"role\":\"user\",\"content\":\"What is 47*89? Use the calculator tool.\"}],
       \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"calculator\",
         \"description\":\"Evaluate a arithmetic expression\",
         \"parameters\":{\"type\":\"object\",\"properties\":{\"expression\":{\"type\":\"string\"}},
         \"required\":[\"expression\"]}}}]}" | head -60'
```

Expected: `finish_reason: "tool_calls"` and a structured `tool_calls` array with `function.name = "calculator"`.

**FAILURE MODE — read carefully.** If the tool call appears as *plain text* inside `message.content` instead of a structured `tool_calls` array, `<PARSER>` or the chat template is wrong. **Do not proceed to Task 6** — the agent loop would treat that text as a final answer and print raw tool syntax to users. Fix the parser (try the alternative from Task 2 Step 5, or pass `--chat-template`), recreate the container, and repeat this step.

- [ ] **Step 4: Streaming tool call**

Repeat Step 3 with `"stream":true`. Expected: `arguments` **fragmented across several deltas**, with `id` and `function.name` on the first only. This is the shape `merge_tool_call_deltas` was written for — confirming it here is the point of the whole exercise.

- [ ] **GATE — human decision.** All four pass ⇒ approved to point the gateway at vLLM.

---

## Task 6: Flip the gateway

The reversible step. One config change.

- [ ] **Step 1: Set the two variables**

In the gateway `.env`:
```
AGENT_BASE_URL=http://<SERVER_HOST>:<VLLM_PORT>
AGENT_MODEL=<SERVED_NAME>
```
Leave `OLLAMA_BASE_URL` pointing at `:11434` — embeddings still need it.

- [ ] **Step 2: Restart and verify the resolved backend**

Restart the gateway; read the startup log.
Expected: `chat backend: http://<SERVER_HOST>:<VLLM_PORT> (model <SERVED_NAME>)`
**If it still names `:11434`, `AGENT_BASE_URL` was dropped** — a misspelling is silently ignored (`extra="ignore"`). Fix and restart; do not proceed.

- [ ] **Step 3: Confirm health reports the chat backend**

Run: `curl -s localhost:8000/health`
Expected: `{"status":"ok","ollama":{"base_url":"http://<SERVER_HOST>:<VLLM_PORT>","reachable":true}}`
The key is still named `ollama` for client compatibility; the **url** is what proves the split took.

---

## Task 7: Validate through the gateway

Everything here uses the real agent loop. Spec §9.

- [ ] **Step 1: A plain chat turn, non-streaming then streaming**

`POST /v1/chat` with `{"message":"Hello, who are you?","stream":false}`, then `true`.
Expected: a coherent answer; streaming yields incremental `token` events.

- [ ] **Step 2: A single-tool turn**

`{"message":"What is 47 * 89?"}` — expected: a `tool_call`/`tool_result` pair for `calculator` and the correct answer, 4183.

- [ ] **Step 3: A MULTI-tool turn — the highest-risk path**

`{"message":"What is 47*89, and what is today's USD to NPR rate?"}`
Expected: **two distinct tool calls**, each result correlated to the right call. This proves `tool_call_id` correlation survives vLLM's fragmented deltas — the path covered until now only by hand-authored fixtures.
**If results are swapped or one is dropped, STOP and roll back (Task 9).**

- [ ] **Step 4: A department-RAG turn**

Ask a question answerable from an ingested department document.
Expected: an answer with citations. **This proves embeddings still route to Ollama** through the split URL. If it fails while plain chat works, `OLLAMA_BASE_URL` was disturbed.

- [ ] **Step 5: A file-producing tool**

`{"message":"Make me a PDF summarising Nepal's central bank."}`
Expected: a `generated_files` row and a downloadable file.

- [ ] **Step 6: A long conversation (context-window check)**

Run 10+ turns, then ask about something from turn 1 and ask the assistant its own name.
Expected: it recalls both. **Forgetting its identity means the system prompt was truncated** — `--max-model-len` is smaller than `CONTEXT_WINDOW_TOKENS`. Fix by matching the two.

- [ ] **Step 7: Tool-routing eval**

Run: `.venv/bin/python scripts/eval_nrb_forex_routing.py`
Expected: pass rate **≥ the Task 3 Step 4 baseline**. A drop means the 35B MoE emits tool calls differently under vLLM — a real regression, not noise.

---

## Task 8: Measure the win, then decide

- [ ] **Step 1: Re-run the identical benchmark**

```bash
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 1  --requests 10 --label vllm-c1  | tee /tmp/vllm-c1.json
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 5  --requests 20 --label vllm-c5  | tee /tmp/vllm-c5.json
.venv/bin/python scripts/bench_chat_concurrency.py --concurrency 10 --requests 30 --label vllm-c10 | tee /tmp/vllm-c10.json
```

- [ ] **Step 2: Compare against the primary metric and guardrails**

| Metric | Requirement |
|---|---|
| `throughput_rps` at c5 and c10 | **must improve clearly** — the reason for the migration |
| `p50_ms` / `p95_ms` at c1 | must **not** regress meaningfully |
| `failed` | must be `0` |
| Tool-routing pass rate | **≥ baseline** |

- [ ] **Step 3: Record the result**

Append both benchmark sets and the verdict to the `## Cutover log`.

```bash
git add docs/ollama-to-vllm-migration.md
git commit -m "docs(vllm): record post-cutover benchmark and the go/no-go verdict"
```

- [ ] **GATE — human decision.** Throughput improved with no guardrail regression ⇒ **keep**. Otherwise ⇒ **roll back (Task 9)**; the measurement did its job.

---

## Task 9: Rollback (execute only if a gate fails)

- [ ] **Step 1: Revert the gateway config**

Remove `AGENT_BASE_URL` (or set blank) and restore `AGENT_MODEL=qwen3.5:35b-a3b`.

- [ ] **Step 2: Restart and confirm**

Expected startup log: `chat backend: http://<SERVER_HOST>:11434 (model qwen3.5:35b-a3b)`
Then one chat turn to confirm service is restored. **Because Ollama never stopped, this is complete in seconds.**

- [ ] **Step 3: Free the VRAM**

Run: `ssh <SSH_HOST> 'cd ~/vllm && docker compose down'`
Then `nvidia-smi` to confirm the memory is released and `nic_ollama` is healthy.

- [ ] **Step 4: Record why**

Append the failure and its evidence to the `## Cutover log` — a failed cutover is the most valuable entry in it.

---

## Task 10: Post-cutover cleanup (only after a soak period)

Days, not minutes. Nothing here is urgent.

- [ ] **Step 1: Soak**

Watch error rates and `chat_messages.trace` for `finish_reason` anomalies over ~1 week of real traffic. Re-run Task 8 Step 1 at the end and confirm the guardrails still hold.

- [ ] **Step 2: Consider raising `--gpu-memory-utilization`**

Ollama no longer serves chat (it still serves embeddings, which are small), so vLLM can take more. Edit `~/vllm/docker-compose.yml`, `docker compose up -d vllm-chat`, re-verify with `nvidia-smi` and Task 5 Step 3.
**Do not remove Ollama** — embeddings and the reranker need it, and the container belongs to another team.

- [ ] **Step 3: Update the docs that are now stale**

Per spec §12: `docs/server-and-models.md` §2/§3/§9, `docs/llm-transport-and-deployment.md` (vLLM is no longer "FUTURE OPTION"), `README.md`, `STATUS.md`, and memory files `agent-model-hosts.md` + `server-and-models-reference.md`.

```bash
git add -A && git commit -m "docs: vLLM is the live chat backend; Ollama serves embeddings"
```

- [ ] **Step 4: Make the runner survive a reboot**

Confirm `restart: unless-stopped` behaves as intended: `ssh <SSH_HOST> 'docker restart vllm_chat'`, wait for `/health`, run one gateway chat turn.

---

## Self-Review Notes

**Spec coverage:** §5 VRAM → Task 2 Step 2 + Task 4 Steps 3/5. §8 runbook → Tasks 3–7. §9 validation → Task 7. §10 metrics → Tasks 1, 3, 8. §11 silent failures → each has an explicit detection step (parser: T5S3; context mismatch: T7S6; split misconfig: T6S2; VRAM collision: T4S3; embeddings mis-routing: T7S4). §12 docs → Task 10 Step 3. §14 unknowns → Task 2.

**Known gaps, deliberate:** exact `<HF_REPO>`, `<PARSER>` and `<VLLM_IMAGE>` cannot be resolved without the server — Task 2 resolves them and gates on human review rather than guessing. `huggingface-cli` invocation in Task 4 Step 1 may differ by image; verify against `<VLLM_IMAGE>` and fall back to `HF_HUB_OFFLINE=0` on first launch (vLLM downloads on demand) if the CLI is absent.
