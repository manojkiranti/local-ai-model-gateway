# Frontend sync prompt

Paste the fenced block below into a Claude CLI session running in the FRONTEND
repo (`local-ai-model-frontend`). It carries this gateway's current API contract
and asks that session to build to it.

**Current task in that block: render chat source citations** (added 2026-08-19).
`/v1/chat` — both the JSON body and the stream's `done` event — plus
`GET /v1/sessions/{id}` now return `sources`, and there is a new document download
route. The change is **additive**: nothing was removed or renamed, so the existing
frontend keeps working; it simply shows no citations until it reads the new field.

History, so the block's framing is not mistaken for the whole story: this file
began as a no-op re-check after the 2026-08 gateway↔Ollama transport port, which
changed nothing in the gateway's own HTTP API. That audit passed with no frontend
changes. Keep this file as the standing sync tool — when the contract changes
again, update the contract section AND the task section together, or a future
paste will tell an agent to verify drift that it should be building.

---

```
You are working in the FRONTEND repo for the "Local LLM Gateway" product. This
frontend talks ONLY to the gateway (base URL below) with a JWT bearer token. It
never talks to Ollama, a model server or the database, and it must never gain
"OpenAI"/model-server logic — the gateway is the single front door and executes
all tools itself.

TWO JOBS, in this order:

  (A) BUILD the chat source-citations UI. The gateway now returns `sources` on a
      chat answer: the department documents that answer was grounded in, with page
      numbers, a download link, and — for Nepal Rastra Bank documents — how the
      text was extracted. Nothing renders it yet. Details and the mandatory
      rendering rules are in the CHAT section and the RENDERING RULES below.

  (B) VERIFY the rest of the API layer still matches the contract below, and fix
      genuine drift. The contract is additive versus what this frontend was last
      synced to, so expect (B) to find little or nothing.

Only modify files in THIS frontend repo. Do not touch the gateway. If something in
the contract looks wrong or impossible to implement as written, say so rather than
guessing — the contract is generated from the gateway's tests, so a real conflict
is worth reporting back.

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

JOB A — build the citations UI:
1. Find where an assistant message is rendered, and where a chat response (both
   the non-streaming JSON and the NDJSON `done` event) is parsed into state.
2. Carry `sources` through into that state. It is `null` for most turns; treat
   null and [] differently (see RENDERING RULES) — null means "no corpus was
   searched", so render nothing at all, not an empty "Sources" heading.
3. Render a Sources area under the answer: one entry per document, with its title,
   its pages if any, and a link that DOWNLOADS the document. The download is
   behind JWT, so fetch it with the Authorization header, take the blob, and make
   a blob: URL — an <a href> to the endpoint cannot send the token and will 401.
   This frontend already does exactly that for /v1/files/{id}; reuse that helper.
4. When `machine_recovered` is true, render `verify_note` prominently on that
   source — a visible warning, not a tooltip and not muted small print. This is
   the one rule in this task that is not cosmetic: that document's text was
   produced by OCR or by a legacy-Nepali-font conversion that no human has checked,
   so a figure, date or name shown from it may be wrong. Also show `routes` (how
   each page was extracted) and, when `source_url` is present, a link to the
   official public page so a user can verify against the original.
5. When `cited` is false, present the entry as "related" rather than as the source
   of a specific sentence — the model did not mark which claim came from it.
6. Do the same on a reloaded thread: GET /v1/sessions/{id} replays `sources` on
   each assistant message, so history must render identically to a live turn.
7. Do not persist `download_url` anywhere; always take it from the response. It is
   derived server-side and may change.

JOB B — verify the rest:
8. Diff this frontend's API layer against the contract above: field names,
   endpoint paths, streaming event handling, `tool_call.arguments` being an OBJECT
   not a string, reading the X-Session-Id header, and no <a href> file downloads.
9. Fix only genuine mismatches. If a part already matches, say so and change it not.

Report: what you built for JOB A (with the file paths), what JOB B found, and
anything in the contract you could not implement as specified.
```
