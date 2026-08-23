# External API: API-key auth + a synchronous image-OCR endpoint

**Date:** 2026-08-23
**Status:** design approved, not implemented
**Scope:** one new credential type (`ApiClient`), three admin routes to manage it,
one external route (`POST /v1/ocr`).

## Problem

An external application needs to send an image and get its text back. The OCR
capability already exists — `app/files/image_ocr.py` (PP-OCRv5 on onnxruntime) —
but it is reachable only through the `read_image` local tool inside the agent
loop, and every HTTP route on this gateway is JWT-bearer with a `User` behind it.

So the work is not OCR. It is a second authentication scheme and the first
non-human-facing surface the gateway has had.

## Decisions

| Question | Decision |
|---|---|
| Call shape | **Synchronous** single call. No job table, no polling. |
| Key identity | **Standalone service client.** A key is an `ApiClient`, never a `User`. |
| Persistence | **Usage record only.** No image bytes, no OCR text retained. |
| Key admin | **Admin API routes** (JWT + `require_admin`), plaintext returned once. |
| Throttle | Per-key rate limit **and** a hard concurrency cap. |
| Input | **Images only** — the existing upload allowlist. No PDFs. |
| Missing OCR stack | **503** with a clear reason. `INSTALL_OCR` stays opt-in. |

Rejected: binding a key to a `users` row (a leaked key would reach chat, files,
departments and possibly admin routes — exactly what the `auth_provider`
dispatch exists to prevent); a third `auth_provider='api_key'` (breaks
`ck_users_credential` and puts robots in every user list); accepting PDFs (needs
docling ⇒ torch 1.1 G in the API image, the one thing the dependency boundary
exists to prevent); a generic scope framework (YAGNI — one route, one scope).

## Architecture

```
app/apikeys/
  models.py        api_keys, api_key_usage
  keygen.py        PURE: mint / parse / hash / constant-time compare
  repository.py    data access: lookup by prefix, touch last_used, record usage
  policy.py        PURE: is this key usable? is the scope satisfied?
  dependencies.py  require_api_client(scope) -> ApiClient
  throttle.py      per-key bucket
  router.py        /v1/api-keys   (admin, JWT)
app/publicapi/
  ocr_router.py    /v1/ocr        (API key)
  schemas.py       the response envelope
```

`keygen.py` and `policy.py` are **pure** — no DB, no HTTP — for the same reason
`app/rag/ranking.py` and `app/users/policy.py` are: the code that decides whether
a credential is accepted should be provable with no database and no GPU.

An `ApiClient` is a separate type from `User`, and keeping it out of
`app/auth/` is what makes "no key can reach a JWT route" structurally true
rather than a convention someone can forget.

### Why SHA-256 and not bcrypt

The secret is 32 bytes of `secrets.token_urlsafe` — full entropy, no dictionary
to attack. bcrypt's work factor buys nothing here and costs ~100 ms **on every
OCR request**. Passwords need bcrypt because humans choose them; this is not
that.

Format: `lgw_live_<prefix8>_<secret>`. Verification looks the row up by the
indexed `key_prefix` (one B-tree hit) and then `hmac.compare_digest`s the
SHA-256. Two ways to get this wrong, both of which the module docstring must
name: switching to bcrypt (slow, on the hot path), and comparing with `==`
(a timing oracle that reads as correct).

## Schema

Migration sits on the current single Alembic head. `alembic heads` must stay one
(`tests/test_alembic_lineage.py`).

```sql
CREATE TABLE api_keys (
  id            uuid PRIMARY KEY,
  name          text NOT NULL,
  key_prefix    text NOT NULL UNIQUE,
  key_hash      text NOT NULL,
  scopes        text[] NOT NULL DEFAULT '{}',
  is_active     boolean NOT NULL DEFAULT true,
  expires_at    timestamptz NULL,
  created_by_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz NULL,
  revoked_at    timestamptz NULL,
  CONSTRAINT ck_api_keys_revoked CHECK ((revoked_at IS NULL) = is_active),
  CONSTRAINT ck_api_keys_scopes  CHECK (scopes <@ ARRAY['ocr:read']::text[])
);

CREATE TABLE api_key_usage (
  id           bigserial PRIMARY KEY,
  api_key_id   uuid NOT NULL REFERENCES api_keys(id) ON DELETE RESTRICT,
  route        text NOT NULL,
  status_code  smallint NOT NULL,
  bytes_in     integer NOT NULL,
  width        integer NULL,
  height       integer NULL,
  lines_out    integer NULL,
  duration_ms  integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_api_key_usage_key_time ON api_key_usage (api_key_id, created_at DESC);
```

Four things that are deliberate:

1. **`ck_api_keys_scopes` closes the vocabulary**, the same rule as
   `ck_documents_status` and `ck_user_departments_role`. A typo'd scope
   (`'ocr:reed'`) must fail at insert rather than sit in a key someone believes
   works. Adding a scope means editing the CHECK — that is the point.
2. **`ck_api_keys_revoked` makes the half-revoked state unrepresentable**:
   `is_active=false` with no `revoked_at` (revoked when?) and `is_active=true`
   with a `revoked_at` (a revoked key still serving) are both illegal. Same
   shape as `ck_nrb_files_blocked_reason`.
3. **`ON DELETE RESTRICT` on both FKs; keys are never hard-deleted**, exactly
   like departments and `nrb_files`. Usage rows are the only evidence of a
   leaked key's activity, and a cascading delete destroys it. Revocation is
   `is_active=false` + `revoked_at`.
4. **`key_prefix` UNIQUE is functional, not tidiness** — it is the lookup key,
   so a collision makes verification ambiguous. Mint retries on conflict.

`api_key_usage` has no user FK because there is no user; the client is the
identity. `bigserial` because the table grows per request and is only ever read
by time range.

## Wire contract

### `POST /v1/ocr`

`X-API-Key: lgw_live_<prefix>_<secret>`, `multipart/form-data`, field `file`,
optional field `lang` (`devanagari` default, or `en`).

```json
200 {
  "text": "मिति २०८२।०४।१५\nTotal Amount: 45,320.75",
  "lines": [
    {"text": "मिति २०८२।०४।१५", "confidence": 0.94},
    {"text": "Total Amount: 45,320.75", "confidence": 0.97}
  ],
  "authoritative": false,
  "caveat": "machine-read text (OCR), not a transcription — words and whole lines can be dropped or misread. VERIFY every figure, date, account number and contact detail against the image itself.",
  "image": {"kind": "png", "width": 1654, "height": 2339, "frames": 1},
  "engine": {"name": "rapidocr", "model": "PP-OCRv5", "backend": "onnxruntime",
             "lang": "devanagari", "version": "..."},
  "request_id": "..."
}
```

- **`text` and `lines` both ship.** `text` is `"\n".join(lines)` for the 90%
  case; `lines` carries per-line confidence. Neither is cheaply derivable from
  the other, so neither is dropped.
- **`authoritative: false` and `caveat` are always present.** This is the
  §16.6 fact that PP-OCRv5 drops letterheads and subject lines, mangles latin
  runs, and misreads dates (`२०६९।१।३१` → `२०६९।९।३१`). An external app that
  writes this into a client file must be told in the payload, on every
  response. The caveat is **one constant with two readers** — shared with
  `read_image`'s `CAVEAT`, never copied — for the `sources.VERIFY_NOTE` reason:
  a second copy drifts, and then the API and the chat answer caveat
  differently, leaving the reader unable to tell which to believe.
- **No confidence threshold anywhere.** Scores are reported; nothing compares
  one to a constant and no field claims reliability. §16.6 declines to invent a
  threshold from an orthography measurement. AST-asserted, as for `read_image`.
- **`frames` is reported**, and `frames > 1` sets `"partial": true`. A
  multi-frame `.tif` is a scanner's normal output and the engine reads frame 1
  only — measured, page 2's text silently vanished.
- **`request_id` is echoed** so a support ticket joins to an `api_key_usage`
  row. That is the only reason usage rows are worth writing.

### Admin routes (JWT + existing `require_admin`)

```
POST   /v1/api-keys      {name, scopes?, expires_at?}
                         → 201 {id, name, prefix, key, scopes, expires_at}
GET    /v1/api-keys      → [{id, name, prefix, scopes, is_active,
                             created_at, last_used_at, expires_at}]
DELETE /v1/api-keys/{id} → 204   (revoke; row retained)
```

`key` appears in exactly one response body in the system's life and is never
stored in recoverable form. Schemas are `extra="forbid"`, so `is_active` or
`key_hash` in a create body is a loud 422 rather than a silently ignored field —
the same rule as `UserPatch` refusing `role` and the NRB run schema refusing
`all_files`.

No PATCH: rotation is "mint a new one, revoke the old", which needs no overlap
state machine. No `GET /{id}` and no usage-listing route — nothing consumes
them.

## Error taxonomy

| Status | When | Body / header |
|---|---|---|
| 401 | header absent, malformed, unknown prefix, hash mismatch, revoked, expired | `{"detail": "Invalid API key"}` — identical for all six |
| 403 | valid key, `ocr:read` not in scopes | names the missing scope |
| 400 | not an image, corrupt, undecodable, unsupported `lang` | names what is accepted |
| 413 | over the byte cap | states the cap |
| 429 | per-key rate limit | `Retry-After` |
| 503 | OCR stack absent / model load failure | "image OCR is not enabled on this deployment" |
| 503 | no concurrency slot within the wait timeout | `Retry-After: 5` |

1. **401 is one message for six causes, and that is security.**
   Distinguishing "unknown key" from "wrong secret" tells an attacker which
   prefixes are real; distinguishing "expired" tells them a valid key existed.
   The log records which of the six; the response never does.
2. **403 is not 401.** A scope failure means the credential is genuine — the
   caller must know their key is fine and their permissions are not, or they
   rotate a working key chasing the wrong bug. Same reasoning as
   `_require_level`'s 404-then-403 ordering.
3. **A missing OCR stack is 503, never 500 and never an empty 200.** Direct
   lesson of §18: five real deployment defects (a CWD-relative lexicon, a
   root-owned model dir against a uid-10001 process, `torch.compile` with no
   compiler, …) all produced *successful* operations with no text. An empty
   `lines: []` with a 200 is the worst outcome this route can have, because the
   caller writes "no text found" into a client file. So the route returns 200
   with empty `lines` **only** when the engine actually ran and genuinely found
   nothing — that case carries a full `engine` block — and "could not run" is
   never inferred from output emptiness.
4. **413 and the pixel cap are checked before any decode.**
   `images.MAX_IMAGE_PIXELS` runs on the declared dimensions before Pillow
   decodes: a ~200-byte PNG can declare 40000×40000 and pass a 10 MB wire cap,
   and Pillow only *raises* above 2× its own limit while merely warning between
   1× and 2×, so a 1.5× bomb passes if you rely on its exception. Reused, not
   reimplemented — `images._KINDS` is also the decoder allowlist keyed on the
   **sniffed** format, so a GIF renamed `.png` never reaches the GIF decoder.
5. **429 and 503-at-capacity are different answers.** 429 means you sent too
   much; 503 means the box is busy with other callers. A caller that throttles
   itself on a 503 fixes nothing.
6. **The temp file is unlinked in `finally` on every path**, 400s included. A
   rejected upload leaving bytes on disk is the defect the RAG upload route
   already compensates for — and here it would retain a third party's document
   we explicitly said we do not store.

**Auth failures are throttled**, keyed on the presented prefix, for the same
reason `/auth/login` is. Same documented per-process caveat (N uvicorn workers
= N× the limit) and the same eviction rule: **eviction prefers unlocked
entries**, so a flood of junk prefixes cannot evict a locked one and thereby
clear a lockout.

## Concurrency and blocking

`image_ocr.ocr_image` is synchronous, CPU-bound and holds the GIL. Called
directly in an `async def` route it stops the event loop — a single 4-second OCR
freezes every in-flight chat stream in that worker. Not a slowdown, a stall.

```python
_ocr_slots = asyncio.Semaphore(settings.ocr_max_concurrent)   # per process

async with _acquire_slot(timeout=settings.ocr_queue_wait_seconds):  # else 503
    result = await asyncio.to_thread(image_ocr.ocr_image, path, lang=lang)
```

- **`to_thread` is mandatory, not an optimisation** — the same pattern and the
  same reason as `app/rag/worker.py` running Docling through it. The GIL means
  threads do not give parallel OCR (onnxruntime releases it inside native
  kernels, so there is some overlap); what `to_thread` buys is that the event
  loop keeps serving.
- **The semaphore is separate from the thread offload**, because `to_thread`'s
  default executor has its own larger pool and would run many concurrent OCRs,
  each spawning onnxruntime's own intra-op threads, oversubscribing the box.
  `ocr_max_concurrent` defaults to 2.
- **Waiting is bounded** — 503 + `Retry-After` after `ocr_queue_wait_seconds`
  (default 10). An unbounded queue turns a load spike into a total outage.
- **The engine is cached per language** (`image_ocr` already memoises). First
  call after boot pays the model load, which is why the Dockerfile pre-warms
  the models and `chown`s `site-packages/rapidocr` — skipping either is a
  §18-class defect where the stack is present, the call "succeeds", and no text
  ever comes out. An optional `OCR_PREWARM` startup hook exists rather than
  pretending the latency is not there.

### Settings

```
EXTERNAL_API_ENABLED=false      # master switch; false ⇒ both routers unregistered
OCR_MAX_CONCURRENT=2
OCR_QUEUE_WAIT_SECONDS=10
OCR_MAX_UPLOAD_BYTES=10485760   # matches the existing upload cap
OCR_RATE_PER_MINUTE=30
OCR_RATE_BURST=10
API_KEY_PREFIX=lgw_live         # a dev key is visibly not a prod key
OCR_PREWARM=false
```

Validated at `Settings` construction, following `login_max_attempts`. The master
switch defaults **false**, so merging changes nothing about any existing
deployment.

**A deliberate inconsistency, recorded so nobody "fixes" it:** an absent OCR
stack is 503 (§ above) because a 404 is indistinguishable from a wrong URL —
but `EXTERNAL_API_ENABLED=false` *does* unregister the routes and therefore
gives 404. These are different situations: the first is a deployment that means
to serve this API and is broken; the second was never asked to serve it, where
404 is honest and no route is a smaller attack surface than a disabled one.

## Testing

**Pure unit (no DB, no GPU) — where the credential logic is proved:**

- `keygen`: mint → parse round-trip; mints never repeat; `hmac.compare_digest`
  is used (AST-asserted — `==` on a hash is a timing oracle that reads as
  correct); truncated, prefix-only and wrong-secret strings never verify.
- `policy`: fails **closed** on every unknown input — `None` key, empty scopes,
  unknown scope string, past `expires_at`, `is_active=false`. Same rule as
  `permissions.allows(None, …)` being False: a scope that escaped the CHECK
  must not compare as satisfied.
- The caveat is **one constant with two readers** — asserted equal to
  `read_image`'s.
- **No threshold** — AST test over the router: no comparison of a confidence
  value to a literal.

**Integration (Postgres; a throwaway `NullPool` engine per call, because the
module-level engine's pool is bound to the first event loop and the second
`asyncio.run` dies with "Event loop is closed"):**

- 401 for each of the six causes with an identical body; the log distinguishes
  them, the response does not.
- 403 for a scoped-out key. **A key cannot reach a JWT route and a JWT cannot
  reach `/v1/ocr`** — asserted in both directions, since that separation is the
  entire reason for a distinct `ApiClient`.
- Revoke → next call 401. `last_used_at` advances. A usage row is written on
  success **and** on 400/413/429.
- Mint returns the plaintext once; the list route never contains a `key` field —
  asserted on the serialised JSON, not the model.
- 413, the pixel bomb and a renamed GIF are **security tests**, and the pixel
  bomb is asserted rejected *without* Pillow having decoded.
- The temp file is gone after every path, success and failure.
- **`INSTALL_OCR` absent ⇒ 503**, run in a **subprocess** with the stack
  unimportable (the `test_image_ocr_import_boundary.py` technique — `sys.modules`
  is process-global). Plus: importing the app still pulls in none of rapidocr,
  onnxruntime or cv2.

**Live-only (`OCR_LIVE_TESTS=1`, skipped by default):** the 9-case image eval
from `docs/image-ocr.md` driven through HTTP, asserting the API and `read_image`
return the **same lines** for the same image. That equality is the real
regression guard — it is what stops the two paths drifting.

## Evaluation & Improvement

1. **Success metric.** Per-call *usable-output rate*: the share of 200
   responses the consuming app accepted without a human re-keying the value.
   The gateway cannot see that directly, so the proxy it owns is
   `api_key_usage`: `200-with-nonempty-lines ÷ total`, split by status class,
   per key. A rising 503 share means the box is undersized; a rising empty-200
   share means upstream image quality, not a code fault.
2. **Eval.** The 9 labelled images in `docs/image-ocr.md` (Devanagari, English,
   mixed, low-dpi scan, blank, multi-frame TIFF, rotated, pixel-bomb, corrupt)
   extended to 12 with three API-shaped cases (oversized, wrong content type,
   scoped-out key). Scored exact-match on expected `lines` for the text cases
   and exact status + detail for the error cases. Target **12/12**, as a PR
   gate rather than a report. Current rate unknown until the suite runs —
   `read_image`'s existing 9-case target is 8/8 and has not been re-run here.
3. **Feedback capture.** `api_key_usage` is the log: route, status, bytes,
   dimensions, line count, duration, `request_id`. No image bytes and no OCR
   text — the text is the third party's content, and holding it recreates the
   confidentiality problem the persistence decision avoided. The consuming
   app's corrections stay in that app; what returns to us is a `request_id` on
   a ticket.
4. **Review loop.** Monthly: the status-class split per key, p95 duration
   against `OCR_QUEUE_WAIT_SECONDS`, and any key with a nonzero 401 rate (on a
   provisioned key that means either a leak being probed or a caller with a
   stale secret — both need a human). Re-run the 12-case eval on any change to
   `image_ocr.py`, the OCR requirements pin, or the Dockerfile's OCR steps.

## Out of scope

Async/job-based OCR (the envelope is shaped so a `202 {job_id}` variant can be
added under a different route without breaking clients); PDF input; per-scope
rate limits; key rotation with overlap windows; HMAC request signing; a usage
dashboard; any frontend (`../react/local-ai-model-frontend` would own an
API-keys page if one is ever wanted).
