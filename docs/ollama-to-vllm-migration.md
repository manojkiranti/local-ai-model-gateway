# Migrating the chat/agent model from Ollama to vLLM (live GPU server)

**Status:** the gateway-side **code prerequisite is DONE** on branch
`feat/vllm` (the chat/embeddings base-URL split, §6 Option A) and is
behaviour-neutral until `AGENT_BASE_URL` is set. **Nothing has been done on
the server** — vLLM is not running, and no cutover has happened. The runbook
(§8) and validation (§9) are still ahead.
**Author aid:** drafted 2026-08-28 against the repo state at that date.
**Companion docs:** `docs/server-and-models.md` (what runs where),
`docs/llm-transport-and-deployment.md` (why the transport is OpenAI-compatible),
`DOCKER.md`, and the transport gotchas in `CLAUDE.md`.

> Read `docs/server-and-models.md` first for the hardware/model/DB baseline this
> plan assumes. This document is the **how to switch**, scoped tightly.

---

## 0. TL;DR

- **Scope:** move **only the chat/agent model** (`qwen3.5:35b-a3b`) to vLLM.
  Embeddings (`qwen3-embedding:4b-q8_0`) and the (currently-off) reranker
  **stay on the existing Ollama container.** vLLM serves one model per process,
  so a full move would mean multiple vLLM servers — out of scope here.
- **Why:** **throughput / concurrency.** Ollama serialises concurrent requests
  far more than vLLM's continuous batching does. This is vLLM's strongest case
  and the only driver being optimised for. Latency-per-request and model quality
  must **not regress** — that is the guardrail, not the goal.
- **Strategy:** **side-by-side, config flip.** Stand vLLM up on a *different
  port* next to Ollama, repoint the gateway, keep Ollama warm as an instant
  rollback. Costs VRAM during the overlap (see §5).
- **Local dev stays on Ollama — vLLM is a server-only thing.** vLLM is a
  GPU-serving engine (proper NVIDIA card, matching CUDA, shared-memory for
  tensor-parallel) and is impractical on a dev laptop, where one developer hits
  the model serially and Ollama is ideal. The Option-A split (§6) is built for
  exactly this: `AGENT_BASE_URL` defaults to blank and falls back to
  `OLLAMA_BASE_URL`, so **the laptop sets nothing and behaves identically to
  today; only the server sets `AGENT_BASE_URL` to the vLLM port.** Same build,
  same code, both environments — the only difference is that one env var being
  present or absent.
- **The catch that makes it NOT "config only":** the gateway talks to **one**
  base URL (`OLLAMA_BASE_URL`) for chat **and** embeddings. If chat moves to a
  new vLLM port while embeddings stay on Ollama's `:11434`, the gateway needs
  **two** base URLs. That is a small, contained code change (§6) — or it can be
  avoided entirely with a router proxy (§6, Option B). Everything else really is
  config.
- **Environment constraint:** this working environment has **no SSH key, no
  `known_hosts`, no server address, and no remote Docker context** (verified
  2026-08-28: `~/.ssh` empty, only the local Docker context). **Nobody can
  execute this plan from here.** It is written to be handed to whoever has server
  access. Every "run this on the server" step is theirs to run.

---

## 1. What actually has to change (and what doesn't)

The transport was ported to the **OpenAI-compatible** surface precisely so a
backend swap is cheap. `app/ollama/client.py` is the one file that owns the wire
format; the agent loop never sees SSE or `choices[0].delta`. That payoff is real
and this plan leans on it. But "config only" is **too strong** for the chat-only
split. Here is the honest ledger.

### Genuinely portable (no change)

| Thing | Evidence | Why it's fine on vLLM |
|---|---|---|
| Chat request payload | `app/agent/loop.py:227` — `{model, messages, tools, stream, temperature}` | Standard OpenAI. **No `keep_alive`, no `options.num_ctx`, no Ollama-only fields.** vLLM accepts it verbatim. |
| Tool schema shape | `registry.list_ollama_tools()`, `mcp/client._to_ollama` | Standard OpenAI `{"type":"function","function":{...}}`. The `ollama_` naming is cosmetic (see §6.4). |
| Streaming tool-call accumulation | `merge_tool_call_deltas` / `finalize_tool_calls` in `client.py` | Already handles **fragmented** `arguments` across deltas — which is exactly vLLM's behaviour (Ollama sends them whole). The fragmented path is covered by hand-authored fixtures in `tests/test_openai_stream_parsing.py`. This is the one place the code was written *for* vLLM before vLLM existed in the stack. |
| Endpoints used | `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` (`client.py:149,167,169,181`) | vLLM's OpenAI server exposes all three. |
| Health badge | `is_healthy()` → `GET /v1/models` (`client.py:146`) | vLLM answers `/v1/models`. (It also has a dedicated `/health`; see §7.) |
| Agent stop condition | `loop.py:267` — stop when the assistant returns **no tool_calls** | Keyed on tool_calls presence, **not** on a `finish_reason` string, so it's robust to `finish_reason` differences — *provided* `client.py`'s normalization maps vLLM's `finish_reason:"tool_calls"` correctly (live-verify, §9). |

### The one real code change for chat-only scope

**The single base URL.** Chat and embeddings both read `settings.ollama_base_url`:

```
app/main.py:51                         chat client  -> ollama_base_url
app/tools/local/search_department_docs.py:233  query embed -> ollama_base_url
app/rag/worker.py:430                  doc embed    -> ollama_base_url
scripts/*.py (several)                 -> ollama_base_url
```

Move chat to vLLM on a new port and embeddings can no longer share that URL.
Resolve it with **one** of the two approaches in §6. This is the crux of the
whole migration.

### Config that must change (§7)

`AGENT_MODEL` (the model name vLLM serves), context length (a **launch flag**
now, not a service env var), the chat base URL, and `OLLAMA_TIMEOUT`.

### Config whose **name** is now a lie but keeps working

`OLLAMA_BASE_URL`, `OLLAMA_TIMEOUT`, `ollama_base_url`, `OllamaError`,
`OllamaClient`, `list_ollama_tools`, `ollama_schema`, the worker preflight's
`"ollama pull …"` message (`worker.py:103`). None of these break — they are just
misnamed once the backend is vLLM. Renaming them is **optional cleanup**, tracked
in §6.4, and deliberately **not** on the critical path.

---

## 2. Why side-by-side, and the sequencing constraint

The GPU box also runs an **unrelated production compose stack** owned by another
team (`/home/localllm/backend-local/docker-compose.yml`: `nic_ollama`,
`nic_postgres`, `nic_qdrant`). Our gateway only *piggybacks* on its published
`:11434` and `:5432`. **We do not own that stack.** Two consequences:

1. **Do not stop `nic_ollama` as step one.** The other team's app and our
   embeddings/reranker both depend on it. Ollama stays up throughout.
2. **vLLM must fit in the VRAM left over while Ollama is resident** (§5), or the
   two will fight over the two A40s and one of them gets OOM-killed — possibly
   theirs.

The gateway itself is **not deployed on the server yet** — it runs on the laptop
pointing at the server's `:11434`. So the "cutover" is really: *stand up vLLM on
the server, then change one URL (plus the embeddings split) in the laptop
gateway's config and restart it.* That keeps rollback trivial.

---

## 3. Target vLLM server — model source and format

**Do not try to feed Ollama's GGUF blob to vLLM.** Ollama stores models as GGUF
in a content-addressed store. vLLM *can* load GGUF but it is experimental,
single-file, and slower, and the Ollama blob layout is not a clean source.
**Acquire the model fresh from Hugging Face in `safetensors`** (BF16) — that is
vLLM's first-class path.

- Identify the exact HF repo for the model Ollama serves as `qwen3.5:35b-a3b`
  (an MoE with ~3B active params). **Verify the precise repo id and revision**
  before download — do not guess the name.
- Disk: a ~35B model in BF16 safetensors is **~65–75 GB** on disk. An AWQ/GPTQ
  4-bit build is **~18–22 GB**. Provision `--download-dir` / `HF_HOME` volume
  accordingly.

### Quantization on the A40 (this is Ampere, sm_86)

| Format | Works on A40 (sm_86)? | Note |
|---|---|---|
| **BF16** (unquantized) | ✅ | Simplest, highest quality. Fits (§5). Recommended for the first cutover so quality is not a variable. |
| **FP8** (weights or KV cache) | ❌ | **FP8 needs Hopper/Ada (sm_89+). A40 is sm_86 — no FP8.** Do not pass `--kv-cache-dtype fp8` or an FP8 checkpoint. |
| **AWQ / GPTQ INT4** | ✅ (Marlin kernels on Ampere) | Big VRAM saving; small quality cost. Consider only if BF16 VRAM headroom is tight during the Ollama overlap. |
| **GGUF** | ⚠️ experimental | Avoid — re-acquire safetensors instead. |
| **bitsandbytes** | ✅ | Slower than AWQ/GPTQ; not recommended for a serving path. |

> **MoE note:** confirm the specific model's MoE layers have a supported
> quantized kernel before choosing AWQ/GPTQ — MoE quant support has historically
> lagged dense support in vLLM. If in doubt, **start BF16**.

---

## 4. vLLM launch configuration (chat/agent)

Author these as a compose service or `docker run` on the server. **Flag names and
defaults drift between vLLM versions — pin a version and verify each flag against
that version's docs before launch.** The intent per flag:

| Flag | Value (intent) | Why |
|---|---|---|
| `--model` | the HF repo id / local safetensors path | the chat model |
| `--served-model-name` | e.g. `qwen3.5-chat` | this becomes `AGENT_MODEL`. Pick a stable name; the gateway sends it verbatim. |
| `--tensor-parallel-size` | `2` | shard across both A40s |
| `--max-model-len` | `32768` | **the vLLM equivalent of `OLLAMA_CONTEXT_LENGTH`.** Must equal `CONTEXT_WINDOW_TOKENS` in the gateway (§8). |
| `--gpu-memory-utilization` | e.g. `0.45` **while Ollama is co-resident**, raise later | vLLM grabs this fraction of *each* GPU at startup. Leave room for Ollama during the overlap (§5). |
| `--enable-auto-tool-choice` | (set) | **required** or vLLM never emits OpenAI `tool_calls` — the agent loop would get plain text and every tool turn breaks. |
| `--tool-call-parser` | `hermes` (verify for this model) | the parser that turns the model's tool syntax into OpenAI `tool_calls`. Qwen3-family commonly uses `hermes`; some builds ship a model-specific parser. **Verify against the model card + vLLM version.** |
| `--chat-template` | the model's template (often bundled; else pass the file) | tool-calling correctness depends on the right template. If tool calls misfire, this is the first suspect. |
| `--host` / `--port` | `0.0.0.0` / a **new** port (e.g. `8001` or `8100`) | **not `11434`** — Ollama owns that. Side-by-side needs a distinct port. |
| `--api-key` | **omit for the internal, firewalled port** | the gateway sends **no `Authorization` header** today (`client.py:140` — bare `AsyncClient`). If you set `--api-key`, the gateway gets **401** until code adds the header (§6.4). Simplest: don't set it; rely on the deferred internal firewall. |
| `--download-dir` / `HF_HOME` | a persistent volume | so weights survive restarts |

Docker essentials for vLLM: NVIDIA runtime, **`--ipc=host` or a large
`--shm-size`** (tensor-parallel uses shared memory — omitting this causes
NCCL/shm crashes), and the HF cache volume.

Ollama-isms that have **no vLLM equivalent** and must not be carried over:
`OLLAMA_CONTEXT_LENGTH` (→ `--max-model-len`), `num_ctx`, `keep_alive`,
`OLLAMA_MODELS`.

---

## 5. VRAM math and the overlap window

Two A40 = **~92 GB total** (46 GB each). During side-by-side, budget for **both**
servers resident:

- **Ollama** holding `qwen3.5:35b-a3b` (GGUF, ~q4-ish) + its 32k KV cache:
  roughly **20–30 GB** in use — check the live number with
  `docker exec nic_ollama ollama ps` and `nvidia-smi` **before** starting vLLM.
- **vLLM** BF16 ~35B across TP=2 + a 32k KV cache: the **weights are ~65–75 GB**
  spread over the two cards (~33–38 GB/card), **plus** KV cache. `--gpu-memory-
  utilization` caps vLLM's grab per card; set it so `vLLM_grab + Ollama_resident
  ≤ ~44 GB/card` (leave a safety margin under 46).

> **BF16 may not fit alongside a fully-loaded Ollama.** If `nvidia-smi` shows the
> two would collide, either (a) launch vLLM **AWQ/GPTQ INT4** (~18–22 GB weights
> total) for the overlap and switch to BF16 after Ollama is freed, or (b) reduce
> the overlap: cut over fast and stop leaning on both at 32k. **Measure, don't
> assume** — this is the single most likely place the migration takes the other
> team's app (or ours) down.

- **No swap on the box.** vLLM's CPU-offload/`--swap-space` and any host paging
  are effectively unavailable; an OOM is a hard kill, not a slowdown. Keep
  `--gpu-memory-utilization` conservative during overlap.
- After cutover and once Ollama's chat load is gone, vLLM's utilization can be
  raised (it still shares the box with Ollama's embedding model, which is small).

---

## 6. Resolving the single-base-URL problem

Pick **one**. This is the only architecturally interesting decision.

### Option A — split the config into two base URLs (IMPLEMENTED)

**Status: DONE on branch `feat/vllm`.** Introduce a dedicated base URL for the
chat/agent client; leave embeddings and the reranker on the existing Ollama URL.
This is the entire code footprint of the migration, and it is behaviour-neutral
until `AGENT_BASE_URL` is set.

**`app/config.py`** — a new field plus one property that owns the fallback rule.
A property rather than resolving inline at the call site, so the chat client and
the health badge cannot drift apart about which server they mean, and so the
rule is testable without booting the app:

```python
ollama_base_url: str = "http://localhost:11434"   # EMBEDDINGS/reranker
agent_base_url: str = ""                          # CHAT; blank = same server

@property
def chat_base_url(self) -> str:
    return self.agent_base_url or self.ollama_base_url
```

**`app/main.py` (lifespan)** — the one shared chat client is built from
`chat_base_url`, and the resolved value is **logged at startup**, because a
misspelled `AGENT_BASE_URL` is silently dropped (`extra="ignore"`) and the
fallback to Ollama otherwise looks exactly like a successful cutover:

```python
app.state.ollama = OllamaClient(settings.chat_base_url, settings.ollama_timeout)
logger.info("chat backend: %s (model %s)", settings.chat_base_url, settings.agent_model)
```

**`app/main.py` (`/health`)** — reports `chat_base_url`. It previously printed
`ollama_base_url`, which after the split is the *embeddings* server: an operator
checking the cutover would have been shown the wrong machine and told it was
healthy. The JSON key is still `"ollama"` so existing clients keep parsing it —
renaming it would break the frontend health badge for no operational gain.

**`.env.example`** — `AGENT_BASE_URL=` documented (blank), with both traps
written down. The repo's `tests/test_env_templates.py` guard enforces this: a
setting that exists in `config.py` but not in the template fails the suite.

**Everything else stays on `ollama_base_url`, unchanged:**
`app/rag/worker.py:430` (document embeddings), `app/tools/local/
search_department_docs.py:233` (query embeddings), and the scripts. The existing
`agent_model` / `agent_temperature` / `agent_max_iterations` settings are
untouched — only the *URL* was fused, and only the URL is being un-fused.

**Tests** (`tests/test_config_chat_backend.py`,
`tests/test_chat_backend_wiring.py`), each written failing first:

- blank `agent_base_url` falls back to Ollama — **the laptop path**;
- a set `agent_base_url` becomes the chat backend — the server path;
- splitting chat does **not** move `ollama_base_url` — guards the silent failure
  where embeddings would be sent to a vLLM serving a chat model, which answers;
- `/health` reports the chat backend and not the embeddings one;
- the shared client the agent loop streams through is built from the chat URL.

Full suite after the change: **2509 passed, 115 skipped, 0 failed** (skip count
unchanged, per the repo's own "compare the skip count" rule).

- **Payoff:** the gateway genuinely talks to two backends; each can be sized and
  restarted independently, and the laptop keeps a single local Ollama.

> These are **code changes**, so they contradict the "config only" line in
> `docs/server-and-models.md` §8 and `docs/llm-transport-and-deployment.md`.
> Those docs assumed a *whole-backend* swap (all roles move together). For the
> chat-only split, the split-URL change is unavoidable. Fix those docs (§11).

### Option B — one URL, a router proxy in front

Run a lightweight OpenAI-compatible **router** (e.g. LiteLLM proxy) on the
server. The gateway keeps its single `OLLAMA_BASE_URL` pointed at the router; the
router dispatches **by model name**: `AGENT_MODEL` → vLLM, `RAG_EMBED_MODEL` →
Ollama.

- **Gateway code:** genuinely unchanged (the "config only" promise holds).
- **Cost:** a new production service to deploy, monitor, and keep alive — another
  thing that can fail silently between the gateway and the models, on a box owned
  by another team. Adds a hop to every token.
- **When to prefer:** if you expect to move embeddings and the reranker to vLLM
  soon too (then the router is doing real multiplexing work and Option A would
  have grown a third URL anyway).

**Recommendation: Option A.** Concurrency is the only driver; the router buys
generality we don't need yet and adds an operational failure surface on a shared
box. Revisit Option B when embeddings/reranker also move.

### 6.4 Optional cleanup (NOT on the critical path)

Do these only as deliberate follow-up, never mixed into the cutover:

- Rename `OllamaClient`/`OllamaError`/`ollama_base_url`/`list_ollama_tools`/
  `ollama_schema` to backend-neutral names. Purely cosmetic; high churn.
- Fix the worker preflight message `"ollama pull {model}"` (`worker.py:103`) —
  only matters once **embeddings** move to vLLM (they don't here).
- If you ever set vLLM `--api-key`: add an `Authorization: Bearer` header in
  `OllamaClient.__init__` (`client.py:140`) from a new secret setting. Until
  then, leave `--api-key` off.

---

## 7. Config changes (gateway `.env` / `.env.docker`)

Chat-only, Option A. On the **gateway** host (laptop today):

| Setting | From | To | Read by |
|---|---|---|---|
| `AGENT_BASE_URL` *(new, Option A)* | — | `http://<SERVER_HOST>:<vllm_port>` | gateway chat client |
| `OLLAMA_BASE_URL` | `http://<SERVER_HOST>:11434` | **unchanged** (now embeddings/reranker only) | worker, query-embed, scripts |
| `AGENT_MODEL` | `qwen3.5:35b-a3b` | vLLM's `--served-model-name` (e.g. `qwen3.5-chat`) | agent loop |
| `OLLAMA_TIMEOUT` | `120`/`300` | keep generous; verify under load | both clients |
| `CONTEXT_WINDOW_TOKENS` | `32768` | **must equal vLLM `--max-model-len`** | `app/history/context.py` budget |

**Traps:**

- Pydantic settings are `extra="ignore"` — a **misspelled key is silently
  dropped** and looks configured. Double-check `AGENT_BASE_URL` actually lands
  (log it, or read it back at startup).
- `CONTEXT_WINDOW_TOKENS` is a **blind copy** of the server's real window — the
  `/v1` surface never reports the loaded window back. If vLLM runs `--max-model-
  len 16384` but the gateway thinks `32768`, the mismatch is **silent**: the
  gateway budgets confidently into an overflow and vLLM truncates. Keep the two
  numbers identical and change them together.
- **No `OLLAMA_CONTEXT_LENGTH` on the vLLM side.** It's a launch flag now. Any
  runbook step that says "set `OLLAMA_CONTEXT_LENGTH` and recreate the container"
  does **not** apply to vLLM.
- Containers reach host services via `host.docker.internal` + `extra_hosts:
  host-gateway` (already in `docker-compose.yml`). If the gateway is later
  containerized on the same box, vLLM is reachable the same way.

---

## 8. Cutover runbook (side-by-side, instant rollback)

Executed by someone with server access. **Ollama stays up the whole time.**

**Pre-flight (before touching anything):**
1. Record the baseline (§10) — you cannot detect a regression you never measured.
2. `nvidia-smi` and `docker exec nic_ollama ollama ps` → note current VRAM
   headroom per card. Decide BF16 vs INT4 for the overlap from real numbers (§5).
3. Confirm the exact HF repo id/revision for the chat model; pre-download weights
   to the vLLM volume.

**Stand up vLLM (does not touch Ollama or the gateway yet):**
4. Launch vLLM on the **new port** with §4's config and a **conservative
   `--gpu-memory-utilization`**. Watch `nvidia-smi` — abort if it crowds Ollama.
5. Wait for readiness: `GET http://<SERVER_HOST>:<vllm_port>/health` → 200, and
   `GET /v1/models` lists the served name. (During load, these may 404/hang —
   that's "not ready", not "broken".)
6. **Smoke-test vLLM directly** (curl `/v1/chat/completions`, non-stream then
   stream, then a tool-call turn) *before* the gateway ever points at it.

**Flip the gateway (the reversible step):**
7. Land the Option-A code change (§6) in the gateway build. (Until this ships,
   the gateway can't address two backends — this is the one code prerequisite.)
8. Set `AGENT_BASE_URL` + `AGENT_MODEL` (§7). Leave `OLLAMA_BASE_URL` as-is.
9. Restart the gateway. `GET /health` → chat backend reachable.
10. Run the live validation suite (§9). **Multi-tool turn is the make-or-break.**

**Rollback (any time, seconds):**
- Unset `AGENT_BASE_URL` (falls back to `OLLAMA_BASE_URL`) and restore
  `AGENT_MODEL=qwen3.5:35b-a3b`; restart the gateway. Chat is back on Ollama.
  vLLM can keep running or be stopped to reclaim VRAM. **Because Ollama never
  went down, rollback is a config revert, not a re-pull.**

**Decommission (only after a soak period, days not minutes):**
- Once vLLM is trusted, optionally raise `--gpu-memory-utilization`. Do **not**
  remove Ollama — embeddings and the reranker still use it, and the other team's
  app owns that container anyway.

---

## 9. Validation — the exact checks

The test suite runs on **fakes**; nothing in it has talked to a real `/v1`
server. These are **live** checks against the running vLLM.

**Direct-to-vLLM (before the gateway):**
- `curl /v1/models` → served name present.
- Non-streaming `/v1/chat/completions` → a normal answer.
- Streaming → tokens arrive incrementally.
- **A tool-call turn** → response contains OpenAI `tool_calls` with
  `finish_reason:"tool_calls"`. If it returns tool syntax as *plain text*,
  `--enable-auto-tool-choice` / `--tool-call-parser` / `--chat-template` are
  wrong — fix before proceeding.

**Through the gateway (`AGENT_BASE_URL` pointed at vLLM):**
- `GET /health` → chat backend reachable.
- A single-tool `/v1/chat` turn (stream + non-stream).
- **A multi-tool turn in one message** — this proves `tool_call_id` correlation
  survives vLLM's *fragmented* streaming deltas. This is the highest-risk path
  and the reason `tests/test_openai_stream_parsing.py`'s hand-authored fixtures
  exist; now verify them against a real server.
- An MCP tool turn (grants forwarded, result correlated).
- A file-producing tool (e.g. `create_pdf`) end to end.
- A department-RAG turn — proves **embeddings still work on Ollama** through the
  now-split URL, and citations render.
- A long conversation that exercises history truncation — proves
  `CONTEXT_WINDOW_TOKENS` matches `--max-model-len` (watch for the model
  "forgetting" the system prompt, which means the window is smaller than the
  gateway believes).

**Regression gate (the driver):** re-run the §10 concurrency benchmark and
confirm throughput improved **without** a per-request latency or quality
regression. Existing eval scripts to reuse, pointed at the new URL:
`scripts/eval_nrb_forex_routing.py` (tool routing) and, for retrieval,
`scripts/rag_eval_sweep.py` (unchanged — it exercises embeddings, still on
Ollama).

---

## 10. Success metric & how we'll know it worked

The migration is **for concurrency**, so success is measured, not asserted.

1. **Primary metric:** sustained **throughput** (completed chat turns/sec, and
   tokens/sec) at a target concurrency (e.g. 5, 10, 20 simultaneous streaming
   turns), vLLM vs the Ollama baseline. Success = a clear, repeatable improvement
   at the concurrency levels real usage hits.
2. **Guardrail metrics (must NOT regress):**
   - **p50/p95 per-request latency** at concurrency 1 and at the target level.
   - **Tool-calling correctness:** the `scripts/eval_nrb_forex_routing.py` pass
     rate is identical vLLM vs Ollama (a 35B MoE may emit tool calls differently
     — this is where a silent regression would hide).
   - **Answer quality:** a small held-out set of ~10 representative prompts
     (chat, one tool, multi-tool, one RAG) scored side-by-side by a human. This
     doubles as the migration's **eval set** — freeze it before cutover.
3. **Baseline capture (do this BEFORE cutover):** run 1 and 2 against Ollama and
   record the numbers. Without the baseline there is no regression test.
4. **Feedback capture:** keep the trace (`chat_messages.trace`, always persisted)
   and watch error rates / `finish_reason` anomalies in the first days. The
   gateway's `/health` and any vLLM `/metrics` (Prometheus) are the live signal.
5. **Review loop:** re-check the guardrail metrics after ~1 week of real traffic
   before decommissioning the Ollama chat fallback.

---

## 11. Silent-failure watch list (the §18-class risks)

This codebase's recurring lesson: *every way a deployment breaks looks like a
clean deployment.* For this migration specifically, the things that fail with **no
error, no failing test, no log line**:

- **Tool-calling parser wrong** → the model's tool syntax comes back as plain
  assistant text. The loop sees "no tool_calls", treats it as a final answer, and
  returns the raw tool syntax to the user as prose. Caught only by the §9
  multi-tool check.
- **`--max-model-len` < `CONTEXT_WINDOW_TOKENS`** → gateway over-budgets, vLLM
  silently truncates the *front* of the prompt (identity + date system prompt),
  and the turn still returns a normal-looking answer. Caught only by the long-
  conversation check + matching the two numbers.
- **Split URL misconfig** → `AGENT_BASE_URL` typo'd, `extra="ignore"` drops it,
  chat silently falls back to `OLLAMA_BASE_URL` (still Ollama). "Migration
  succeeded" but nothing moved. Caught by logging the resolved chat URL at
  startup and by checking the model id in the `/v1/chat` response body.
- **VRAM collision** → vLLM and Ollama co-resident push one over 46 GB/card; the
  OOM killer takes a process — possibly the other team's. Caught only by
  `nvidia-smi` **before** and during, never by our tests.
- **`finish_reason` mapping** → if `client.py`'s normalization doesn't treat
  vLLM's `finish_reason:"tool_calls"` as "there are tool calls", the loop could
  stop early or loop oddly. Live-verify in §9.
- **`--api-key` accidentally set** → uniform 401 from every chat turn. This one
  is *loud* (not silent) — listed only so nobody sets it expecting the gateway to
  authenticate.

---

## 12. Documentation to update after cutover

The switch makes several docs wrong. Update as part of "done":

- `docs/server-and-models.md` §2 (Ollama-as-the-model-server), §3 (chat model
  row), §8 ("Backend swap checklist" says *config only* — add the split-URL code
  change and the vLLM launch flags), §6/§9.
- `docs/llm-transport-and-deployment.md` — "Production server" option 2 (vLLM) is
  currently a stub; promote it to the live setup, correct the `--tensor-parallel-
  size 2` + `--max-model-len` + tool-calling flags, and add §5's VRAM caveat.
- `CLAUDE.md` — the `num_ctx`/`OLLAMA_CONTEXT_LENGTH` gotcha and the "wire format
  lives in one file" note both need a vLLM sentence.
- `README.md`, `STATUS.md`, `DOCKER.md` — any line that names Ollama as *the*
  model server or `qwen3.5:35b-a3b` on Ollama.
- **Memory:** update `memory/agent-model-hosts.md` and
  `memory/server-and-models-reference.md`.

---

## 13. Out of scope (deliberate) / future work

- **Embeddings → vLLM.** Would need a *second* vLLM process (one model per
  server), the worker preflight message fixed (`worker.py:103`), and either a
  third base URL (Option A grows) or the router (Option B pays off). Qwen3-
  Embedding's asymmetric query prefix and the 2560→1536 MRL truncation must be
  re-verified — vLLM may or may not apply the instruction prefix itself, and may
  expose a `dimensions` param that changes where truncation happens.
- **Reranker → vLLM native `/rerank`.** `app/rag/rerank.py` currently plans a
  logprob hack over `/v1/chat/completions` because *"Ollama's OpenAI shim does
  not expose a `/rerank` endpoint … but `/v1/chat/completions` does return
  `logprobs`"* (its own docstring). vLLM **does** expose a native rerank/score
  endpoint — moving to it is the cleaner path, but it's gated on the abstention
  work being fitted at all (`RAG_RERANK_ENABLED=false` today).
- **`--reasoning-parser` / thinking mode.** `docs/reasoning-thinking.md` is a
  parked exploration of a ChatGPT-style reasoning toggle. vLLM's
  `--reasoning-parser` surfaces `reasoning_content` separately and is the natural
  enabler — but it interacts with tool calling and is its own project. Not part
  of this migration.
- **Renaming the `ollama_*` symbols** (§6.4) — cosmetic, do it on its own branch.
- **Deploy hardening** — firewalling the internal deps (now vLLM + Ollama +
  Postgres + MCP) to the gateway's IP. Already deferred; the vLLM port joins that
  list, and is the reason `--api-key` can safely be omitted.

---

## 14. Open questions to resolve before execution

1. **Exact HF repo id + revision** for `qwen3.5:35b-a3b`, and whether a supported
   AWQ/GPTQ build exists for its **MoE** layers (fallback: BF16).
2. **Correct `--tool-call-parser`** name for this specific model on the pinned
   vLLM version (`hermes` is the likely answer — verify against the model card).
3. **Pinned vLLM version** and its CUDA/driver requirement vs the box (driver
   580.173.02, CUDA 13.0) — confirm the chosen image runs on that host.
4. **BF16 vs INT4 for the overlap** — decided by the live `nvidia-smi` headroom
   while Ollama is resident (§5).
5. **Who executes this** — nobody in the current dev environment has server
   access (§0). Confirm the operator and hand them this doc.
