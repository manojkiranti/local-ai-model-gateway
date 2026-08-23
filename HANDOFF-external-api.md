# Handoff: external API-key OCR endpoint (paste this into a new session)

I'm continuing work on the local-ai-model-gateway repo at
`/home/manoj/newlaptop/projects/python/local-ai-model-gateway`. A previous session built an
external API-key-authenticated OCR endpoint end to end. **Read this whole prompt before acting.**

## State right now

- Branch **`feat/rest-api`**, HEAD `679ee75`, **25 commits ahead of `main`**, clean tree, **unmerged**.
- Full suite on this exact tree: **2489 passed, 28 skipped, 0 failed** (~7.5 min).
  All 28 skips are accounted for and legitimate: 15 `test_ocr_api_eval.py` (gated on `OCR_LIVE_TESTS=1`,
  passes 15/15 when enabled), 8 `test_history_context_eval.py` (needs the live model), 3
  `test_nrb_extraction.py` (needs `NRB_DOCLING_TESTS=1`), 1 `test_rag_retrieval_eval.py` (cohort not
  frozen), 1 `test_ocr_api_integration.py` (the stack-installed 503 test, whose complement runs).
  **Zero auth-helper skips** — the failure mode where a broken `_ensure_user` hides 86 tests behind a
  green run did not occur. If you re-run the suite, compare the skip *distribution*, not just the count.
- Migration `53c2ce388596` (api_keys, api_key_usage) chains to `c2f8b1d47e93`, is **applied** to the dev
  database, single Alembic head, no model-vs-schema drift.

## What was built

`POST /v1/ocr` — external caller sends `X-API-Key` + a multipart image, gets OCR text synchronously.
Admin JWT routes `POST/GET /v1/api-keys` and `DELETE /v1/api-keys/{id}` mint, list and revoke keys.
Everything is behind `EXTERNAL_API_ENABLED`, **default false**, so merging changes nothing until it is
switched on. Code: `app/apikeys/` (keygen, policy, models, repository, throttle, dependencies, schemas,
router) and `app/publicapi/` (ocr_router, schemas, middleware). Runbook: `docs/external-api.md`.
Design + plan: `docs/superpowers/specs/2026-08-23-external-api-keys-and-ocr-endpoint-design.md` and
`docs/superpowers/plans/2026-08-23-external-api-keys-and-ocr-endpoint.md`. `CLAUDE.md` has the
Endpoints, Layout and Conventions/gotchas entries.

## THE PENDING DECISION — ask me before doing anything else

The branch is finished and awaiting integration. Present exactly these and wait:
1. Merge back to `main` locally
2. Push and create a Pull Request
3. Keep the branch as-is

## Then, optionally: the next endpoint

I asked "what other APIs can we make like this" and then to brainstorm one, but wanted to clarify
something first — **ask me what I wanted to clarify.** The candidates, in my recommended order:
- **`POST /v1/extract`** — PDF/DOCX/TXT/MD/JSON → text. Reuses `app/files/documents.py` (zero DB refs).
  Completes the document story: a caller shouldn't need to know whether their PDF has a text layer, and
  `documents.py` already distinguishes a scanned page (`text_pages == 0`) from an empty one. **Bounded.**
- **`/v1/spreadsheet/{inspect,aggregate}`** — the arithmetic-correctness work is already paid for
  (`numeric.py`: Decimal, never eval, rejects "nan"/"Infinity", unparseable cells counted AND named).
  **Bounded**, two routes + a paging contract.
- **`POST /v1/embeddings`** — `embed_texts` handles the Qwen3 query/document asymmetry and re-sorts by
  `index`. One decision: `mode` must be REQUIRED, since guessing silently degrades retrieval.
- **`POST /v1/search`** (department RAG) — **architectural**: the department boundary is
  `user_departments` and an API key has no user, so a key needs its own binding; and `app/rag/ranking.py`
  fails OPEN by design, which needs re-examining for a machine consumer.
- **`POST /v1/chat`** externally — architectural, own design round (per-token rate limits, key-owned
  sessions, injection now arriving from outside the org).
- **Never `fetch_url`** — exposing it makes the gateway an open proxy.

The reusable win: `app/apikeys/` is credential-generic. A new endpoint is one scope string (a two-place
edit: `policy.ALL_SCOPES` **and** the `ck_api_keys_scopes` CHECK — deliberately two places, the CHECK
stops a typo being stored and the set stops one being honoured), one router, one `require_api_client(...)`.

## Non-obvious facts that cost real effort to learn — do not undo these

1. **The key secret is `secrets.token_hex`, NOT `token_urlsafe`.** base64url contains `_`, which is the
   token's own delimiter; measured, 48.6% of `token_urlsafe(32)` secrets contain one, and those keys
   would fail verification forever. `parse` takes the last two `_`-delimited fields; do not "restore"
   urlsafe and do not replace the positional split with a scan (a scan breaks on a configurable
   `API_KEY_PREFIX` like `deadbeef`).
2. **Verification is prefix-indexed SHA-256, not bcrypt** — 256-bit random secret, so a work factor buys
   nothing and would cost ~100 ms per request. `hmac.compare_digest` is AST-asserted because `==` on a
   hash is a timing oracle that reads as correct code.
3. **All six credential causes return a byte-identical 401 body AND headers** (absent, malformed, unknown
   prefix, wrong secret, revoked, expired). The log distinguishes them; the response never does.
   **Latency does differ** — attributable causes write a usage row — and `docs/external-api.md` discloses
   that honestly. Don't re-assert uniformity you don't have.
4. **A scope failure is 403, not 401** — the credential is genuine, so the caller must not rotate a
   working key chasing the wrong bug.
5. **An expired/revoked key DOES consume a throttle attempt.** An earlier version exempted it by analogy
   to the AD-login rule; that analogy fails, because AD-unavailable is transient while revocation is
   permanent for a prefix. The exemption was an unthrottled oracle whose 429-vs-401 boundary told an
   attacker a leaked secret was genuine.
6. **A missing OCR stack is 503, never an empty 200.** `docs/nrb-integration.md` §18: five real
   deployment defects each produced a *successful* operation with no text, and an empty `lines: []` with
   a 200 makes the caller write "no text found" into a client file. An engine that is PRESENT but throws
   is 500; only an absent/unloadable stack is 503, and the route branches on `available()` to tell them
   apart.
7. **`asyncio.to_thread` + a separate semaphore are both mandatory.** `ocr_image` is sync and CPU-bound,
   so calling it directly in an `async def` route stalls the event loop and freezes every in-flight chat
   stream. The semaphore is separate because `to_thread`'s executor is much larger and would
   oversubscribe the box; waiting is bounded (503 + Retry-After), because an unbounded queue turns a
   spike into an outage. **429 and 503-at-capacity are different answers** and tests assert they stay
   distinguishable in both directions.
8. **`OcrContentLengthGuard` must be registered BEFORE `CORSMiddleware`.** `add_middleware` inserts at
   position 0 and the stack builds in reverse, so the last one added is OUTERMOST — the opposite of how
   it reads. Wrong order and the guard's 413 carries no CORS headers.
9. **The OCR caveat is ONE constant with two readers** (`image_ocr.OCR_CAVEAT`), asserted by **identity**
   (`is`) not equality — equality would pass against two copies of the same literal.
10. **No code compares a confidence score to a literal.** An AST test enforces it; §16.6 declines to
    invent a threshold from an orthography measurement.
11. **`image.kind` is `"PNG image"`, not `"png"`.** That exact error shipped twice.
12. **The eval does NOT assert the API and `read_image` return identical lines** — the engine is
    nondeterministic on Devanagari (same fixture gave `नेपाल राषट्र बैंक` on one run, `h राष्ट्र नंक` on
    another). It holds the API to the same measured predicates as the tool, importing that eval's own
    `CASES` table. Don't "fix" it back into byte-equality.
13. **`api_keys.scopes` is `ARRAY(Text)`, not `ARRAY(String)`** — Postgres has no `varchar[] <@ text[]`
    operator, so the table could not be created the other way.
14. Keys are **never hard-deleted** (revoke = `is_active=false` + `revoked_at`, and
    `ck_api_keys_revoked` makes the half-revoked state unrepresentable). Usage rows are the only evidence
    of what a leaked key did — hence `ON DELETE RESTRICT` on both FKs.

## Known-and-recorded, not fixed

- **The whole multipart body still reaches disk before authentication** — FastAPI parses the form before
  solving dependencies, and Starlette caps only NON-file parts. `OcrContentLengthGuard` rejects a
  *declared* oversized body early; a client that lies about or omits `Content-Length` sails past it.
  **A reverse-proxy body cap is a documented PREREQUISITE** before enabling the flag anywhere real
  (nginx `client_max_body_size 12m;`), stated in the runbook's "Turning it on".
- `touch_last_used` writes on **every** authenticated request, before the route body — a per-request
  write on the hot path. Named follow-up: make it a coarse/conditional write (only if older than ~60 s).
- `X-Request-Id` is returned on the **200 only**, not on error paths. Ruled: not worth restructuring
  every raise. The runbook says how to find the usage row without it.
- `api_key_usage` has a documented retention paragraph but **nothing prunes it automatically**.
- The guard matches `scope["path"]` exactly, so it silently no-ops behind `--root-path /api` (commented).
- R2's lockout transition-detection relies on single-threaded event-loop semantics (no `await` between
  two `retry_after` reads) — a tripwire for any future refactor of `_record_failure`.
- No frontend for key management; no async/job OCR; no PDF input on `/v1/ocr`.

## Process notes worth knowing

The full ledger of every decision is `.superpowers/sdd/2026-08-23-external-api-keys-and-ocr-endpoint/progress.md`
(plus per-task briefs, reports and review diffs in the same directory). **Delete that directory once the
branch lands** — git history becomes the record.

**Eight defects were found in the plan's own specified code during execution, six of them because an
implementer argued instead of complying.** If you dispatch subagents here, tell them to push back rather
than transcribe, and treat "the brief says so" as weaker evidence than the code. A whole-branch review
after all eleven tasks had individually passed still found eight more Important issues — per-task reviews
structurally cannot see cross-cutting security, task seams, or a knob that lies.
