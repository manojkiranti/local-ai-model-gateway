# Frontend sync prompt

Paste the block below into a Claude CLI session running in the FRONTEND repo to
audit it against this gateway's current API contract. (As of 2026-08 the existing
frontend was confirmed working against the OpenAI-transport gateway with **no
changes** — the port was gateway↔Ollama only. Keep this as a re-check tool for
future contract changes.)

**2026-08-19: the contract DID change.** `/v1/chat` (both paths) and
`GET /v1/sessions/{id}` now carry `sources`, and there is a new document download
route. Additive — nothing was removed or renamed — so an existing frontend keeps
working; it just shows no citations until it reads the new field.

---

```
You are working in the FRONTEND repo for the "Local LLM Gateway" product. The
gateway backend was refactored so that internally it talks to Ollama over the
OpenAI-compatible /v1 API instead of Ollama's native API. IMPORTANT: that change
is entirely gateway↔Ollama. The gateway's OWN HTTP API — the one this frontend
calls — did NOT change. Your job is to VERIFY this frontend still matches the
gateway's current contract (below) and fix any drift you find. Do not add any
"OpenAI" or model-server logic to the frontend; it never talks to Ollama.

Only modify files in THIS frontend repo. Do not touch the gateway.

=== Gateway API contract (base URL http://localhost:8000) ===

AUTH (public):
  POST /auth/register  body {email, password(min 8)}  -> 201 UserOut; 409 if email taken
  POST /auth/login     body {email, password}
     -> 200 {access_token, token_type:"bearer", expires_in:<seconds>}; 401 if bad creds
  Send the token on every authed call as:  Authorization: Bearer <access_token>

USER:
  GET /users/me   -> {id, email, auth_provider, role("admin"|"member"), is_active,
                      created_at, updated_at}

CHAT (the core endpoint) — POST /v1/chat  (authed):
  Request body:
    { "message": string (required, non-empty),
      "session_id": string | null,   // omit/null to start a new conversation
      "model": string | null,        // optional per-request override
      "stream": boolean,             // false = JSON, true = NDJSON stream
      "options": object | null,      // passthrough (e.g. {"temperature":0.2})
      "file_ids": string[] | null }  // ids from POST /v1/files to attach

  When stream=false -> 200 JSON:
    { "session_id": string,
      "message": { "role": "assistant", "content": string },
      "model": string,
      "stop_reason": "completed" | "max_iterations" | "error",
      "trace": null | [ ...tool trace entries... ],    // null when no tools ran
      "sources": null | [ Source, ... ] }              // null when no corpus was searched

  Source (one entry per DOCUMENT the answer was grounded in, best first):
    { "document_id": string,
      "title": string,
      "department_code": string,
      "file_name": string|null,
      "file_type": string|null,      // pdf | docx | xlsx | csv | text
      "pages": number[],             // ascending; [] for csv/xlsx/typed text
      "cited": boolean,              // true = the model's [N] named this document
      "download_url": string|null,   // relative; see the download route below
      "origin": string|null,         // "nrb" for an NRB catalog doc, else upload|manual
      // The five below are NRB-only and null for anything else:
      "source_url": string|null,     // the document's public page on nrb.org.np
      "published_at": string|null,
      "routes": string[]|null,       // native | legacy_conversion | ocr, per page
      "machine_recovered": bool|null,
      "verify_note": string|null }

  RENDERING RULES for sources:
    * null means the turn searched no corpus. Render nothing — not an empty panel.
    * `cited: false` means the answer was grounded in these documents but the model
      did not mark them (or two searches ran, so [N] is ambiguous). Still show them,
      just without implying a specific claim came from a specific file.
    * `machine_recovered: true` MUST render its `verify_note`. That text is not
      decoration: the page's text came from OCR or from a legacy-font conversion
      that no human has verified, so a figure, date or name on it may be wrong.
      The same sentence was shown to the model, so your badge and the answer agree.
    * `sources` is NOT suppressed by EXPOSE_TRACE. The trace is diagnostics; the
      citations are part of the answer.

  When stream=true -> 200, Content-Type application/x-ndjson.
    The new/continuing session id is ALSO returned in the response header:
        X-Session-Id: <session_id>
    Body is newline-delimited JSON objects, one per line. Event types:
      {"type":"token","content": string}                      // assistant text delta
      {"type":"tool_call","name": string,"arguments": object,"iteration": int}
      {"type":"tool_result","name": string,"status": string,"result": string,"iteration": int}
      {"type":"done","session_id": string,"stop_reason": string,
       "iteration_count": int,"final_answer": string|null,
       "error_message": string|null,"trace": array,
       "sources": null | [ Source, ... ]}   // same shape as the JSON body's
    Sources arrive ONLY on "done", never earlier: they are resolved against the
    FINAL answer's [N] markers, so they cannot be correct before the turn ends.
    Render tokens live as they arrive; tool_call/tool_result are the "glass-box"
    activity you can show inline. The turn ends on the single "done" event.
    Note: arguments in tool_call is a JSON OBJECT, not a string.

  Errors: 401 missing/invalid JWT; 404 unknown session_id or model not pulled;
          502 Ollama/MCP unreachable.

SESSIONS (chat history, authed):
  GET    /v1/sessions            -> [ {id, title|null, created_at, updated_at, message_count} ]
  GET    /v1/sessions/{id}       -> {id, title|null, created_at, updated_at,
                                     messages:[{id, seq, role, content, trace|null,
                                                sources|null, model|null,
                                                created_at}]}   ; 404 if not yours
         `sources` replays the same shape the live turn returned (download_url is
         recomputed on read), so a reloaded thread shows the same links.
  DELETE /v1/sessions/{id}       -> 204 ; 404 if not yours

FILES (per-user, authed):
  POST   /v1/files   multipart upload (.xlsx/.csv) -> 201 {id, filename, media_type, size, source}
         400 bad ext/corrupt/zip-bomb; 413 too large
  GET    /v1/files            -> {items:[{id, filename, media_type, size, source}], ...}
         optional ?source=generated|uploaded
  GET    /v1/files/{id}       -> raw file bytes (owner-scoped); 404 not yours; 410 gone
  DELETE /v1/files/{id}       -> 204
  GOTCHA: downloads are behind JWT, so an <a href> can't send the Bearer header.
          Fetch with the Authorization header, get a blob, make a blob: URL.

DEPARTMENT DOCUMENT DOWNLOAD (authed) — what a citation links to:
  GET /v1/departments/{code}/documents/{document_id}/download -> raw bytes
      403 you have no grant for that department
      404 unknown document, another department's document, one that is not `ready`
          (members), or a row whose bytes are missing
      GOTCHA: same as /v1/files/{id} — behind JWT, so an <a href> cannot fetch it.
      Fetch with the Authorization header, take the blob, make a blob: URL.

MCP status badge (authed): GET /v1/mcp/status -> always 200, health is in the body
  {configured, reachable, tools, error}. Never treat a non-reachable MCP as a failure.

=== What to actually do ===
1. Find this frontend's API layer (auth, chat, sessions, files calls).
2. Diff it against the contract above. Report any mismatch: wrong field names,
   wrong endpoint paths, wrong streaming event handling, treating tool_call
   arguments as a string, missing X-Session-Id read, <a href> file downloads, etc.
3. Fix only genuine mismatches. If it already matches, say so and change nothing.
4. Do NOT add Ollama/OpenAI/model logic — the frontend only speaks to the gateway.
Report what you found and what (if anything) you changed.
```
