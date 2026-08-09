# Reasoning / "thinking" control — decision notes + server probe

Status: **exploration, not built.** Parked until live server access is available.
Question that started this: can we offer a ChatGPT/Claude-style reasoning selector,
or is model behaviour fixed in `.env`?

## What we established (grounded in code + local models)

- It **can** be per-request (like a ChatGPT selector) — it does not have to be an
  `.env` global. `ChatTurnRequest.model` is already a per-request override; a
  reasoning field could work the same way.
- Today the gateway does **none** of it: the loop payload sends only
  `model, messages, tools, stream, temperature`. The request's `options` field is
  accepted but **ignored** (dead — nothing reads it). No think/reasoning handling
  anywhere.
- Two kinds of "reasoning control" — don't conflate:
  - **Graded low/med/high** — a real dial. Rare in open models. Purpose-built one:
    **`gpt-oss`** (`reasoning_effort: low|medium|high`, native, tool-capable).
  - **Thinking on/off** — most hybrid reasoning models (Qwen3 / `qwen3.5:35b-a3b`,
    GLM-4.5/4.6, DeepSeek). Covers ~80% of the value.

## Model landscape for our 2× A40 (~92 GB VRAM)

| Model | Reasoning | Tools | Fits? |
|-------|-----------|-------|-------|
| gpt-oss:20b | graded low/med/high | yes | easily (~13 GB) |
| gpt-oss:120b | graded low/med/high | yes | yes (MoE, ~63 GB) |
| qwen3.5:35b-a3b (server runs this) | on/off | yes | yes (~20 GB) |
| GLM-4.5-Air | on/off | yes | tight at Q4 (~70 GB) |
| GLM-4.6 (full) | on/off | yes | NO — 355B |
| glm4:latest / qwen2.5:latest (local) | none (`['tools','completion']`) | yes | — |

VRAM numbers approximate — confirm quant sizes in the Ollama library before pulling.

## Recommendation

- For a **tool-calling business assistant**, on/off thinking is almost always
  enough. We already run a capable hybrid model — start by wiring **qwen3.5's
  thinking toggle**, no model change.
- Only chase the graded dial if hard reasoning/math matters — then `gpt-oss:20b`,
  A/B'd against qwen3.5 on our real tool tasks.
- Verify tool-calling still works with thinking ON — some models regress. Only a
  live run catches this.

## The change, when we build it (model-agnostic, small)

1. Gateway: accept a per-request reasoning field, map to the model's param
   (`reasoning_effort` for gpt-oss, or `think` for Ollama), forward in the payload.
   Also wire the currently-dead per-request `options`/temperature while in there.
2. Streaming: thinking comes back separately from the answer — add a `reasoning`
   event type next to `content`/`tool_calls`/`finish` in `app/ollama/client.py`
   (the one wire-format file), so the frontend can show a collapsible "Thinking…"
   panel.
3. Frontend: a toggle in the composer.

Switching models later = `AGENT_MODEL` + the field mapping. Nothing else.

## Run these ON THE SERVER first (needs the real model)

They decide: is it a reasoning model, which field (`reasoning_effort` vs `think`)
turns thinking on over the `/v1` API the gateway uses, and — critically — does
tool-calling survive thinking being ON.

```bash
M=qwen3.5:35b-a3b
echo "=== 1. Ollama version ==="
curl -s http://localhost:11434/api/version

echo; echo "=== 2. Does the model advertise thinking? (look for 'thinking') ==="
curl -s http://localhost:11434/api/show -d "{\"model\":\"$M\"}" \
  | python3 -c "import sys,json;print('capabilities:',json.load(sys.stdin).get('capabilities'))"

echo; echo "=== 3. /v1 with reasoning_effort:high — accepted? reasoning field returned? ==="
curl -s http://localhost:11434/v1/chat/completions \
  -d "{\"model\":\"$M\",\"reasoning_effort\":\"high\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"What is 23*47? Think step by step.\"}]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);m=(d.get('choices') or [{}])[0].get('message',{});print('error:',d.get('error'));print('reasoning field present:', any('reason' in k.lower() for k in m));print('message keys:',list(m.keys()));print('content head:',(m.get('content') or '')[:150])"

echo; echo "=== 4. /v1 with think:true (Ollama extension) ==="
curl -s http://localhost:11434/v1/chat/completions \
  -d "{\"model\":\"$M\",\"think\":true,\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"What is 23*47? Think step by step.\"}]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);m=(d.get('choices') or [{}])[0].get('message',{});print('error:',d.get('error'));print('message keys:',list(m.keys()));print('content head:',(m.get('content') or '')[:150])"

echo; echo "=== 5. THE REAL TEST: thinking + a tool call over /v1 ==="
curl -s http://localhost:11434/v1/chat/completions \
  -d "{\"model\":\"$M\",\"think\":true,\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"What is 23*47? Use the calculator tool.\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"calculator\",\"description\":\"eval math\",\"parameters\":{\"type\":\"object\",\"properties\":{\"expression\":{\"type\":\"string\"}},\"required\":[\"expression\"]}}}]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);m=(d.get('choices') or [{}])[0].get('message',{});print('error:',d.get('error'));print('message keys:',list(m.keys()));print('tool_calls:',bool(m.get('tool_calls')));print('reasoning present:', any('reason' in k.lower() for k in m))"
```

Bring the output back and we pick: wire qwen3.5's toggle, or switch to gpt-oss.
