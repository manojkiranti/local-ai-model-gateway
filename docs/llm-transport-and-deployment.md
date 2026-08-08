# LLM Transport & Deployment — reference

Quick reference for how this gateway talks to the model server, and how to run it
locally and on the GPU box. For the code-level gotchas see `CLAUDE.md`; this file
is the "why" and the "how to deploy".

## What the transport is (and why we changed it)

The gateway talks to the model server over the **OpenAI-compatible REST API**
(`/v1/chat/completions`, `/v1/models`, `/v1/embeddings`), with `httpx`. It used to
use Ollama's **native** API (`/api/chat`, `/api/tags`). We ported it on
2026-08 (branch `feat/openai-compatible-transport`).

**Why bother:** the OpenAI surface is a shared standard. Ollama, vLLM, llama.cpp,
LiteLLM and TGI all speak it. So switching the backend later is a **URL change**
(`OLLAMA_BASE_URL`) instead of a rewrite of the tool-calling layer. That is the
whole payoff.

**We do NOT use the `openai` (or `ollama`) SDK** — plain `httpx`. The SDK would
not solve streamed tool-call fragment accumulation for us (only its *beta* helper
does) and it would displace our `OllamaError` → HTTP-status mapping. See CLAUDE.md.

**One file owns the wire format:** `app/ollama/client.py`. Its `stream_chat`
yields normalized events (`{"type":"content"|"tool_calls"|"finish"}`); the agent
loop never sees SSE or `choices[0].delta`. Point `OLLAMA_BASE_URL` at a different
OpenAI-compatible server and nothing outside that file should need editing.

## The context-length gotcha (important)

The native API let us request context per call (`num_ctx`). **The `/v1` surface
has no such field**, and Ollama's shim *ignores* a passthrough `options.num_ctx`
(verified on 0.32.5: asked for 8192, model loaded at 4096).

So context must be set **on the server**, not in the request. If you don't,
Ollama falls back to **4096 tokens**, which is too small for this agent: the ~12
local tool schemas are ~2,800 tokens on their own, and a single tool result is
capped at 8,000 chars (~2,000 tokens) — so one tool call overflows a 4096 window
and the model appears to "forget" mid-turn.

**Fix (both hosts): set `OLLAMA_CONTEXT_LENGTH` as a service env var.** This is
the equivalent of vLLM's `--max-model-len` launch flag, so the mental model
carries across backends.

## Local dev (this laptop)

- Ollama runs as a systemd service (`User=ollama`), model store at
  `/home/manoj/.ollama/models`, listening on `:11434`.
- Model: `qwen2.5:latest` (7.6B). `.env` → `AGENT_MODEL=qwen2.5:latest`.
- Gateway on `:8000`: `.venv/bin/uvicorn app.main:app --reload --port 8000`.

Set the context floor once (needs sudo):

```bash
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_CONTEXT_LENGTH=32768"
sudo systemctl restart ollama
```

> Note: this machine's Ollama service is `User=ollama` but its model store is
> owned by `manoj`, so the service can read models (inference works) but cannot
> **write** them — `ollama pull`/`create`/`rm` fail with `chtimes: operation not
> permitted`. `OLLAMA_CONTEXT_LENGTH` avoids that entirely (it needs no store
> write). If you ever do need to pull/create models locally, fix ownership with
> `sudo chown -R ollama:ollama /home/manoj/.ollama/models`.

## Production server (GPU box `chatbot`)

Hardware:

| Resource | Detail |
|----------|--------|
| CPU | 2× Intel Xeon Gold 5415+ — 16 cores / 32 threads total (2 sockets, NUMA) |
| GPU | 2× NVIDIA A40, **46 GB VRAM each (~92 GB total)**, driver 580.173.02, CUDA 13.0 |
| RAM | 125 GB (≈119 GB available) |
| Swap | none |

Current LLM: `qwen3.5:35b-a3b` (MoE, ~3B active params) on Ollama. (STATUS.md's
older `qwen2.5:72b` target is stale — ignore it.)

**Two ways to serve it, both fed by the same gateway code:**

1. **Ollama on the server** (what's running now)
   - Same as local: set `OLLAMA_CONTEXT_LENGTH=32768` on the service and restart.
   - Point the gateway's `OLLAMA_BASE_URL` at the server's `:11434`.
   - `AGENT_MODEL=qwen3.5:35b-a3b`.

2. **vLLM on the server** (the reason for the transport port)
   - Launch with the OpenAI server enabled and context as a flag:
     `--max-model-len 32768`. With 2× A40 you can also use `--tensor-parallel-size 2`.
   - Point `OLLAMA_BASE_URL` at vLLM's `:8000/v1` base. **No gateway code change.**
   - `AGENT_MODEL` = whatever name vLLM serves the model under.

**VRAM note:** a 32k-token KV cache is a real allocation on top of the weights.
92 GB across two A40s is comfortable for a 35B MoE at 32k, but if you later push
context much higher or run several models, measure with `nvidia-smi` rather than
assuming it fits.

**Deploy hardening (deferred):** firewall the internal deps (Ollama/Postgres/MCP)
to the gateway's IP so only the gateway is reachable. Not done yet — noted here so
it isn't forgotten.

## Backend swap checklist

To move from local Ollama → server Ollama → vLLM, you only ever touch config:

1. `OLLAMA_BASE_URL` → the new server's OpenAI base URL.
2. `AGENT_MODEL` → the model name the new server serves.
3. Context: `OLLAMA_CONTEXT_LENGTH` (Ollama) or `--max-model-len` (vLLM).
4. Restart the gateway. No code edits if the backend is OpenAI-compatible.

## Still to verify live

Nothing on the branch has talked to a real `/v1` server yet — the test suite runs
on fakes. Before trusting it in front of users, run a real turn and confirm:
single-tool call, **multi-tool** call (proves `tool_call_id` correlation),
streaming tokens, MCP status, and a file-producing tool. On the server,
**re-verify tool-calling with `qwen3.5:35b-a3b`** — shim behaviour is
version/model dependent, and a 35B MoE may emit tool calls differently than the
local 7B.
