# Frontend sync prompt

Paste the fenced block below into a Claude CLI session running in the FRONTEND
repo (`local-ai-model-frontend`). It carries this gateway's current API contract
and asks that session to build to it.

**Current task in that block: consume PAGINATED chat history** (updated 2026-08-22).
Chat history is now keyset-paginated on the gateway's `feat/lazy-load`:
`GET /v1/sessions` returns an ENVELOPE instead of a bare array, and
`GET /v1/sessions/{id}` returns ONE PAGE instead of the whole thread. Full
rationale and known limits:
`docs/superpowers/plans/2026-08-22-chat-history-lazy-loading-PROGRESS.md`.

**Breaking in BOTH directions, so the two branches ship together.** An old
frontend against the new gateway calls `.map` on an object; a new frontend
against an old gateway reads `items` off a bare array. Both render zero
conversations, and neither shows an error explaining why. Same coupling as
`feat/role`/`feat/roles`.

Also on that branch: `POST /v1/chat`'s `message` gained `max_length=8000`, so a
long paste that used to succeed now returns 422.

Previous task, shipped: verify the department-role UI (2026-08-21). Per-department
levels (`viewer` < `editor` < `owner`) landed on the gateway's `feat/role` and the
frontend's `feat/roles`; the corrected contract is still below because it is still
the contract, and those two branches are still an unshipped pair.

Previous task, shipped: render chat source citations (2026-08-19) — `/v1/chat`,
the stream's `done` event and `GET /v1/sessions/{id}` return `sources`, plus the
document download route. Done on both sides; the contract for it is still below
because it is still the contract.

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

  (A) BUILD paginated chat history — see JOB A at the bottom. The gateway no
      longer returns the whole session list or the whole thread, so this is API
      consumption plus paging UI, NOT client-side virtualization of an
      already-fetched array.

  (A2) VERIFY the department-role UI against the contract below (it shipped
      earlier; re-check only if you touch it). Six review items were applied to
      the gateway — four of which change what you should expect:
        * `role` on a department row is now REQUIRED and a closed set
          ("viewer" | "editor" | "owner"), never null.
        * `POST .../members` with NO `role` key now PRESERVES an existing
          member's level (it used to demote them to viewer). If your client sends
          a client-side default of "viewer", REMOVE it — omit the field unless the
          user actually chose a level, or "re-add" still silently demotes.
        * The members routes now work on a SOFT-DISABLED department. If you added
          a workaround that refuses to call them when `is_active` is false, remove
          it.
        * Department CRUD (create / rename / enable-disable) is GLOBAL-ADMIN-only.
          An `owner` must not be shown those forms.

  (B) VERIFY the rest of the API layer still matches the contract below, and fix
      genuine drift — including the citations UI shipped in the previous sync.

Only modify files in THIS frontend repo. Do not touch the gateway. If something in
the contract looks wrong or impossible to implement as written, say so rather than
guessing — the contract below was written by hand from the gateway's code and
verified field-by-field against it, but hand-written means it can still be wrong,
and a real conflict is worth reporting back rather than coding around.

=== Gateway API contract (base URL http://localhost:8000) ===

AUTH:
  POST /auth/login     body {email, password}          PUBLIC
     -> 200 {access_token, token_type:"bearer", expires_in:<seconds>}
     -> 401 bad credentials  (detail "Invalid email or password" — identical
            whether the address is unknown, the password is wrong, or Active
            Directory rejected it; do not try to tell the user which)
     -> 403 the account exists but is deactivated
     -> 429 too many failed attempts. Read the Retry-After header (seconds) and
            show a countdown. Do NOT auto-retry.
     -> 503 DIRECTORY SIGN-IN IS DOWN. This one matters: it is NOT a wrong
            password. Render the server's `detail` verbatim, keep whatever the
            user typed, and offer "try again" — never "check your password", and
            never a password-reset prompt. Showing 503 as a credential failure
            during an AD outage sends a whole office to reset passwords that
            were never wrong.
     Send the token on every authed call as:  Authorization: Bearer <access_token>

     One endpoint, two kinds of account. Staff sign in with their Active
     Directory credentials (the email/UPN goes in the `email` field); local
     password accounts still exist for service and break-glass admin use. The
     frontend does not choose and must not ask: the server decides from the
     user's own record. There is no "sign in with AD" button.

  POST /auth/register  body {email, password(min 8)}   ADMIN ONLY
     -> 201 UserOut; 409 email taken; 403 caller is not an admin;
        401 no/invalid token
     REMOVE any "create an account" / self-signup link from the login screen.
     Registration now exists only so an admin can create a local service or
     break-glass account. (Exception you will not see in normal operation: on a
     completely empty database the first registration is allowed unauthenticated
     and becomes the admin.)

USER:
  GET /users?q=<email fragment>&limit=&offset=   ADMIN ONLY
     -> 200 {total, limit, offset, items:[UserOut]}   total is the MATCH count
     `q` is a case-insensitive substring of the email; LIKE wildcards are
     literal. This is how an admin resolves an email to a user id.

  PATCH /users/{id}  body {is_active: bool}      ADMIN ONLY
     -> 200 UserOut
     -> 409 refused, with `detail` naming why: deactivating yourself, or the last
            active admin. Show `detail` verbatim.
     -> 404 unknown id; 422 any other field (role is NOT patchable here)
     Deactivating takes effect on that user's NEXT request, not at token expiry.
     This is the offboarding control; disabling someone in AD does not revoke a
     token already issued to them.

  GET /users/me   -> {id, email, auth_provider("local"|"ad"), role("admin"|"member"), is_active,
                      created_at, updated_at}

CHAT (the core endpoint) — POST /v1/chat  (authed):
  Request body:
    { "message": string (required, non-empty, MAX 8000 chars -> 422 over that),
      "session_id": string | null,   // omit/null to start a new conversation
      "model": string | null,        // optional per-request override
      "stream": boolean,             // false = JSON, true = NDJSON stream
      "options": object | null,      // passthrough (e.g. {"temperature":0.2})
      "file_ids": string[] | null,   // ids from POST /v1/files to attach
      "department": string | null }  // a department tab's code, e.g. "hr"

  ABOUT "department" — READ THIS BEFORE BUILDING THE CITATIONS UI. A chat is
  either a GENERAL chat or bound to ONE department, and only a department chat
  searches documents. A general chat therefore returns "sources": null on every
  turn, always. If this frontend never sends `department`, you will build the whole
  citations UI and correctly never see a single citation — so wire the department
  selection first, or you cannot tell your UI from a broken backend.
    * Send `department` on the FIRST turn (session_id omitted) to open a department
      chat. That binds the session permanently.
    * On later turns of a bound session you MAY omit it — the server reads the
      binding from its own row. Sending it is an optional consistency check.
      Omitting it is NOT an error.
    * The department is never a tool argument and is never trusted from the body on
      an existing session; the server owns it. Do not try to "switch" a chat's
      department — start a new chat instead.
    * Errors: 404 unknown or deactivated department · 403 you have no grant for it ·
      409 this session belongs to a different department · 409 this is an existing
      GENERAL conversation and cannot be adopted into a department (start a new
      chat in the tab) · 404 the session is not yours.

DEPARTMENTS (the tabs the user may open a chat in) — authed:
  GET /v1/departments -> [ {code, name, is_active, role, ...} ]
      A member sees only the departments granted to them and still active; an
      admin sees all. This is the list to render as tabs / a picker, and it is how
      the user gets into a chat that can produce citations at all.

      `role` is YOUR level in that department: "viewer" | "editor" | "owner".
      It is the ONE field that decides what to draw, and it already accounts for
      global admins (an admin reads "owner" everywhere):

          role === "viewer"                     -> chat + read only
          role === "editor" || role === "owner"  -> also show upload / archive
          role === "owner"                       -> also show the members screen

      `role` is REQUIRED and always one of those three -- never null. If you ever
      receive null or a missing field, you are talking to a gateway without
      `feat/role`; fail closed and say so, do not guess.

      DO NOT recombine this with /users/me's global `role` to work out what is
      allowed. The server has already done it; a second copy of the policy on the
      client will drift, and a UI that shows an upload button the API then refuses
      is worse than no button.

      BUT `role` does NOT cover department CRUD. Creating a department, renaming
      it, and enabling/disabling it are GLOBAL-ADMIN-only:
          POST  /v1/departments            admin
          PATCH /v1/departments/{code}     admin
      An `owner` reaches the members screen and the corpus, NOT these. Gate them
      on /users/me's `role === "admin"`. Showing an owner a Create-department form
      is exactly the failure the previous paragraph warns about.

      THE THREE 403 DETAILS you can get from a department-scoped route, all to be
      rendered verbatim and none of them a login problem:
        "You do not have access to this department"          -> no grant at all
        "Editor access to this department is required"       -> grant too weak
        "Owner access to this department is required"         -> grant too weak

DEPARTMENT MEMBERS (admin, or an OWNER of that department) — authed:
  These three routes work on a SOFT-DISABLED department too, unlike the corpus
  routes. Grants deliberately survive `is_active = false`, so cleaning up a
  departing employee's access must not require reactivating the department. Do not
  special-case an inactive department here. (Note a non-admin owner has no UI path
  to it, since GET /v1/departments lists active departments only.)
  GET    /v1/departments/{code}/members
      -> [ {user_id, email, role, granted_by, granted_at} ]
      `email` is here because GET /users is admin-only — an owner needs it to tell
      members apart. Owners appear in this list even though an owner may not
      modify them; that is deliberate, so a refused revoke is explicable.

  POST   /v1/departments/{code}/members
      body { user_id | email, role? }   // exactly ONE of user_id / email
      OMITTING `role` means "do not change the level": a NEW member lands on
      "viewer", an EXISTING member keeps the level they have. Do NOT send a
      client-side default of "viewer" — that turns a re-add into a silent
      demotion, which is the bug this rule exists to prevent. Send `role` only
      when the user actually chose one.

      This endpoint is ALSO promote/demote: posting an existing member with a new
      `role` changes their level, so one screen does grant and change.

      An owner may act on their OWN row without an admin — stepping down from
      owner to editor/viewer, or revoking themselves, is allowed. Only *another*
      owner is untouchable.

      A DEACTIVATED account cannot be granted: 409 "That account is deactivated;
      reactivate it before granting access". Render it verbatim.
      -> 204 done
      -> 403 with a `detail` to render VERBATIM. Two distinct cases:
           "Owner access to this department is required"
                 you are a viewer/editor here — do not offer this screen at all
           "Only a global admin can grant owner access to a department"
                 an owner tried to create another owner. Keep the dialog open and
                 show the message; tell them to ask an admin. Do NOT retry.
      -> 404 unknown user (same answer for an unknown id and an unknown email)
      -> 422 `role` was not one of viewer/editor/owner
      NOTE: an owner cannot look users up (GET /users is admin-only), so grant by
      `email` on the owner screen.

  DELETE /v1/departments/{code}/members/{user_id}
      -> 204 removed
      -> 403 "Only a global admin can change or revoke another owner's access"
             — an owner may not evict a fellow owner. Render verbatim.
      -> 404 that user held no grant here

DEPARTMENT DOCUMENTS (editor of that department, or admin) — authed:
  These DO 404 on a soft-disabled department, for admins too: a retired department
  is gone from the product as far as its corpus is concerned. Membership is the
  documented exception, above.
  POST   /v1/departments/{code}/documents        multipart {title, file}
  POST   /v1/departments/{code}/documents/text   {title, content}
  DELETE /v1/departments/{code}/documents/{id}   archive
      -> 403 "Editor access to this department is required"
             THIS IS NOT A LOGIN PROBLEM. Never trigger a re-login or a token
             refresh on it — the user is signed in and simply lacks the level.
             Render `detail` and hide the control next time (see `role` above).

  GET    /v1/departments/{code}/documents[?include_archived=true]
      A viewer sees `ready` documents only, and `include_archived=true` is 403 for
      them. An editor sees every non-archived document plus extra operational
      fields (embed_model, embed_dim, updated_at) — so do not assume one shape.

  GET    /v1/ingest-jobs/{id}
      Editor of that document's department, or admin. Poll it after an upload.
      -> 404 when you may not see it. NOT 403: a job id maps to a document, so the
             API refuses to confirm the job exists at all. Treat 404 as "no longer
             available" rather than retrying.

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

SESSIONS (chat history, authed) — PAGINATED as of 2026-08-22:
  GET    /v1/sessions?limit=&cursor=
         -> { items: [ {id, title|null, created_at, updated_at, message_count} ],
              next_cursor: string | null }
         An ENVELOPE, not a bare array. `limit` default 30, max 100 (outside
         1..100 is a 422). `cursor` is OPAQUE base64 — never parse or construct
         one; only ever echo back a `next_cursor` the server gave you.
         `next_cursor: null` = no further page. 400 = malformed cursor.
         Row shape is UNCHANGED, `message_count` included.
         Ordering is keyset on (updated_at DESC, id DESC). It guarantees no
         DUPLICATES, but a session bumped to the top mid-scroll can be MISSED
         until a refetch of page one — that is expected, not a bug.
  GET    /v1/sessions/{id}?limit=&cursor=
         -> {id, title|null, created_at, updated_at,
             messages:[{id, seq, role, content, trace|null, sources|null,
                        model|null, created_at}],
             next_cursor: string | null}     ; 404 if not yours, 400 bad cursor
         ONE PAGE, not the full thread: the NEWEST `limit` messages, but returned
         ASCENDING by seq — so top-to-bottom rendering is unchanged.
         `next_cursor` walks OLDER messages, null once the first message is in.
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

JOB A — consume paginated chat history:
0. Read src/lib/api.ts, src/hooks/useSessions.ts and
   src/components/layout/Sidebar.tsx FIRST and follow the patterns already there
   (the request<T>() helper, AbortSignal threading, existing error handling). Do
   not restructure state management for this.
1. Update the two API functions and their types: listSessions (typed
   Promise<SessionSummary[]> today) and getSession. Add the envelope types. Keep
   SessionSummary and MessageOut exactly as they are — only the wrappers changed.
   Do not send `limit`; let the server's default of 30 govern both.
2. Sidebar: auto-load the next page with an IntersectionObserver sentinel at the
   bottom.
3. Thread: an explicit "Load older messages" button at the TOP. Chosen
   deliberately over an upward sentinel — auto-loading upward plus scroll
   anchoring is the jumpy case, and a button is predictable and testable.
4. THE SCROLL TRAP, and the one thing most likely to be got wrong: a button does
   NOT remove the anchoring work. Prepending older messages grows the DOM above
   the viewport, so the view jumps unless you restore it — capture scrollHeight
   before the prepend, then set scrollTop += (newScrollHeight - oldScrollHeight)
   in a LAYOUT effect, before paint. Assert this in a test.
5. Guard overlapping and chained requests in BOTH places: ignore a trigger while
   a request is in flight, and keep the sentinel disabled until the page has been
   appended. A sentinel that stays intersected after append fires repeatedly and
   walks several pages on one fast scroll.
6. Sidebar RESET on invalidation: drop accumulated pages and refetch page one
   when a session is created, deleted, or a turn completes (a turn bumps
   updated_at and reorders the list server-side). Dedupe by id when appending,
   defensively.
7. Handle 400 distinctly from 404. A 400 means OUR cursor state is bad, not that
   the user's data is wrong — reset to page one and refetch, never surface a raw
   error. 404 on the thread route still means "unknown session or not yours" and
   must behave exactly as it does today.
8. Distinguish "no more pages" (next_cursor === null) from "zero sessions" — do
   not render a dead sentinel or a disabled button when the first page came back
   short.
9. Composer: enforce the 8000-char message cap client-side. Show a counter as the
   user approaches it, and if they exceed it point them at file upload
   (POST /v1/files + file_ids), which is the intended path for long content. Do
   not let a long paste die on a raw 422.
10. Tests (vitest, matching the existing suite's conventions): the envelope is
   parsed and items rendered; paging appends without duplicates and stops on a
   null cursor; a cursor is never sent unless the server supplied it; prepending
   older messages preserves scroll position; a 400 resets to page one; the
   composer blocks >8000 chars before submitting.
11. Record the both-directions breaking-pair coupling somewhere durable in this
   repo (README or a docs note), not only in the session transcript.

JOB A2 — the citations UI (SHIPPED 2026-08-19; re-verify only if you touch it):
0. FIRST, make a department chat reachable, or nothing below is testable: check
   whether this frontend calls GET /v1/departments and sends `department` on the
   first turn. If it does not, wire that up (tabs or a picker) before touching the
   citations UI — a general chat returns "sources": null forever, so without this
   you cannot distinguish working code from broken code. Say in your report which
   of the two you found.
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

Run npm run test, npm run lint and npm run build. SHOW the output rather than
summarising it.

Report: what you built for JOB A (with the file paths), what JOB B found, and
anything in the contract you could not implement as specified — say so plainly
instead of working around it.
```
