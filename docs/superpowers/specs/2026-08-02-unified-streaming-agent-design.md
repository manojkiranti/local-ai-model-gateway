# Unified streaming chat + tools — Design

_2026-08-02 · Local LLM Gateway_

## Goal
Resolve the chat-vs-agent fork. Today `/v1/chat` streams but has no tools;
`/v1/agent` has tools but doesn't stream. End-state: **one endpoint** (`/v1/chat`)
that streams AND is tool-capable, exposing full glass-box activity.

## Decisions (locked with user)
- **One unified endpoint.** `/v1/chat` handles everything; the model uses tools
  when useful. `/v1/agent` is **removed** (folded in). The "agent" concept is
  invisible plumbing.
- **Tools always available** — every turn runs the loop (even tool-free ones).
- **Full glass-box stream** — typed NDJSON events: `token`, `tool_call`,
  `tool_result`, terminal `done`. Not just tokens.
- **Transport: NDJSON** (house style), `Content-Type: application/x-ndjson`.
- **Loop is the single engine** for both stream and non-stream (no duplicate paths).

## Validated against live Ollama (qwen2.5:latest)
- Plain answers stream content deltas token-by-token.
- A tool-calling turn returns `tool_calls` **complete in a single chunk** (content
  empty) — no partial-argument reassembly needed.

## Architecture
```
stream_turn(...)  async generator, yields typed events, the ONE engine
   ├─ /v1/chat stream=true  → serialize each event to NDJSON → client;
   │                           persist assistant row on `done` (fresh session)
   └─ run_turn(...) collects → {final_answer, stop_reason, trace, …}
        /v1/chat stream=false → persist + return JSON
```
- `app/ollama/client.py`: add `stream_chat(payload)` — async generator over parsed
  NDJSON chunks (wraps the existing `open_chat_stream`).
- `app/agent/loop.py`: refactor into `stream_turn(...)` (async generator, ports all
  robustness) + `run_turn(...)` (collects the generator into today's result dict,
  so non-stream reuses identical logic). Loop **always** streams Ollama internally.
- `app/chat/router.py`: the unified endpoint. Builds local+MCP registry every turn
  (via `stream_turn`), always tool-capable; `stream` chooses NDJSON vs JSON.
- Remove `app/agent/router.py` and the `/v1/agent` route + its `main.py` include.
  Keep `app/agent/loop.py` + `schemas.py` (chat imports them).

## Event protocol (NDJSON, one JSON object per line)
- `{"type":"token","content": str}` — assistant content delta
- `{"type":"tool_call","name": str,"arguments": any,"iteration": int}`
- `{"type":"tool_result","name": str,"status": str,"result": str,"iteration": int}`
  (status ∈ ok|unknown_tool|bad_arguments|repeat|tool_error)
- `{"type":"done","session_id": str,"stop_reason": str,"iteration_count": int,
     "final_answer": str|null,"error_message": str|null,"trace": TraceEntry[]}`
- `done` always terminates (success or error). The loop yields `done` without
  `session_id`; the router injects it before serializing.
- Streaming response also carries the new id in the **`X-Session-Id`** header.

## Persistence (unchanged model)
- User row committed immediately (`history.service.open_turn`), as today.
- Assistant row on `done`: `content = final_answer` (or error fallback), `trace`
  = the JSONB **only when tools were actually used** (a tool-free turn stores
  `trace=null`, keeping plain chat clean). **Clean-turn rule holds:** live stream
  may show narration + tool activity, but the persisted/reloaded turn is the final
  answer + trace.
- Streaming persists from a fresh `SessionLocal` in the generator wrapper (the
  request-scoped session isn't safe mid-stream) — same pattern as before.

## Non-stream response shape (changed)
`/v1/chat` (stream=false) now returns tools info:
`{session_id, message:{role,content}, model, stop_reason, trace?}`
(`trace` present only when tools were used). Previously `{session_id, message, model}`.

## Error handling
- **MCP unreachable:** streaming pre-flights MCP reachability BEFORE returning the
  StreamingResponse, so it can still 502 cleanly (can't 502 mid-stream). Non-stream
  gets 502 via the loop as today. (Reuses the existing reachability probe.)
- **Ollama down / mid-run tool errors / max-iter:** surfaced as a `done` event with
  `stop_reason` = error|max_iterations (+ `error_message`); non-stream maps the same
  into the result dict.
- Unknown session id → 404 (open_turn). Empty message → 422.

## Testing
- Unit (no network): `stream_turn` driven by a fake streaming Ollama — assert the
  event sequence for (a) plain answer (tokens→done) and (b) a tool turn
  (tool_call→tool_result→tokens→done, correct trace). `run_turn` collects to the
  same result dict the old `run_agent` returned (keeps existing loop tests' intent).
- Integration (real PG, skip if down): `/v1/chat` non-stream persists trace;
  `/v1/chat` stream yields typed events + `X-Session-Id` and persists the final
  answer + trace. Ollama/MCP faked.

## Incremental phases
1. `ollama.stream_chat` + refactor loop to `stream_turn`/`run_turn`; keep behavior
   identical (unit-tested in isolation). No endpoint change yet.
2. Unify `/v1/chat` (stream + non-stream, always tools); remove `/v1/agent`; update
   main, tests, docs, and the frontend contract.

## Out of scope
Streaming partial tool-call arguments (Ollama sends them whole). SSE transport.
Per-token persistence. Cancellation/abort mid-stream from the client.
```
