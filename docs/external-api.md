# The external API: API keys and `POST /v1/ocr`

Design and reasoning: `docs/superpowers/specs/2026-08-23-external-api-keys-and-ocr-endpoint-design.md`.
This file is the runbook.

## Turning it on

Both `/v1/api-keys` (admin, JWT) and `/v1/ocr` (API key) are unregistered
unless `EXTERNAL_API_ENABLED=true` — `app/main.py` includes both routers
inside the same `if`. **The flag is read once, at process start** (route
registration happens when `app.main` is imported, not per-request), so
flipping it in `.env` needs a gateway restart before it takes effect — not
just a config reload.

The OCR route also needs the OCR stack, which is an opt-in image build flag:

    docker compose build --build-arg INSTALL_OCR=true gateway

With the stack absent the route exists and answers **503**, with the detail
`image OCR is not enabled on this deployment`. That is deliberate: it is a
deployment that means to serve OCR and cannot, which is a different fact from
a deployment that was never asked to (there the route simply does not exist —
404, not 503).

**Verify a deployment by making a real call with a known image, never by
whether the container started.** `docs/nrb-integration.md` §18 found five
distinct OCR deployment defects that all produced *successful* operations with
no text.

## Minting the first key

There is no bootstrap script: the admin routes are JWT-admin, and an admin
already exists on any deployed instance.

    TOKEN=$(curl -s -X POST localhost:8000/auth/login \
      -H 'content-type: application/json' \
      -d '{"email":"admin@example.com","password":"..."}' | jq -r .access_token)

    curl -s -X POST localhost:8000/v1/api-keys \
      -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
      -d '{"name":"odin-crm-ocr"}' | jq

`scopes` defaults to `["ocr:read"]` (the only scope that exists today; an
empty list is rejected — a key with no scopes can do nothing). The response
is the **only** time the plaintext key exists (shape:
`{id, name, prefix, key, scopes, expires_at}`). Store `key` in the consuming
app's secret manager immediately; there is no recovery, only re-minting.

## Calling it

    curl -s -X POST localhost:8000/v1/ocr \
      -H "X-API-Key: lgw_live_..." \
      -F file=@scan.png -F lang=devanagari | jq

`lang` is `devanagari` (default, reads English too) or `en` — anything else is
a 400 before the file is even read. Accepted extensions: `.png .jpg .jpeg
.webp .tif .tiff .bmp`. A PDF (or anything else outside that list) is a 400 —
OCR'ing page 1 of a document that may have a text layer would discard it, and
`read_document` is the right tool for that case.

## Reading the response

Body shape: `{text, lines[{text, confidence}], authoritative, caveat,
partial, image{kind,width,height,frames}, engine{name,model,backend,lang,
version}, request_id}`.

`authoritative` is always `false` and `caveat` is always present, and they
mean it: PP-OCRv5 drops letterheads and subject lines, mangles latin runs, and
misreads dates. **Never** treat a figure, date, account number or contact
detail from this endpoint as correct without a human checking it against the
image. `caveat` is not a second copy of that sentence — it is the *same*
`image_ocr.OCR_CAVEAT` constant `read_image` renders into chat, so the API and
a chat citation never disagree about the wording
(`tests/test_ocr_api_boundaries.py::test_the_caveat_is_one_constant_with_two_readers`).

`partial: true` means the image had more than one frame and only the first was
read — a multi-frame TIFF is a scanner's normal output.

`lines[].confidence` is reported because it is information. There is no
threshold and no "reliable" flag: the measurement behind these scores is
orthographic well-formedness, which is not a per-field correctness estimate —
nothing in the router or the schema compares a confidence to a literal
(structurally asserted by AST).

A `200` with `lines: []` and `text: ""` means the engine ran and genuinely
found nothing (a blank or textless image) — the `engine` block is still fully
populated, which is the proof the engine actually ran. A stack that could
*not* run is always a 503, never inferred from an empty result.

## Status codes

| Code | Meaning | What the caller should do |
|---|---|---|
| 401 | The key is absent, malformed, unknown, wrong, revoked or expired | Check the secret; ask an admin whether it was revoked |
| 403 | The key is genuine but lacks `ocr:read` | Ask an admin to re-mint with the scope. Do NOT rotate the key |
| 400 | Not an image, corrupt, too many pixels, empty upload, or a bad `lang` | Fix the input |
| 413 | Over `OCR_MAX_UPLOAD_BYTES` (default 10 MB) | Downscale before sending |
| 429 | This key's rate limit (`OCR_RATE_PER_MINUTE`/`OCR_RATE_BURST`) | Honour `Retry-After` |
| 503 | OCR unavailable, **or** at capacity (`OCR_MAX_CONCURRENT`/`OCR_QUEUE_WAIT_SECONDS`) | Retry on `Retry-After`; if the detail says "not enabled", it is a deployment fault, not a transient one |
| 500 | An unexpected exception inside the OCR engine | **Report it, do not blindly retry.** The real exception is logged server-side and never returned to the caller (the client only ever sees the generic detail `OCR failed unexpectedly`) — this is a server fault, and retrying the same image against the same bug wastes a call. Ask an operator to check the logs around the `request_id`/time of the call. |

401 is one message for all six credential causes on purpose — distinguishing
them tells an attacker which prefixes are real. The server log distinguishes
them; ask an operator.

**`X-Request-Id` is on the 200 response only.** It is set on the FastAPI
`Response` object early in the handler, but every non-200 path raises an
`HTTPException`, and FastAPI builds the error response from the exception
directly — the header set on the (unused) success `Response` never reaches
the client. So there is **no request id to quote on an error**, including the
500. Do not tell a caller "check the `X-Request-Id` header" for anything but a
successful call. To find the row for a *failed* call, locate it by key +
approximate time + status instead — `api_key_usage` gets a row on every path,
success or failure (see Operating, below), it is just not addressable by an
id the client never received.

## Revoking

    curl -X DELETE localhost:8000/v1/api-keys/<id> -H "authorization: Bearer $TOKEN"

204 on success, 404 if the key does not exist or is already revoked. Takes
effect on the holder's next call. The row is kept (`is_active=false` +
`revoked_at`) so the key's usage history stays attributable — keys are never
deleted.

## Operating

`api_key_usage` holds one row per call: route, status, bytes in, image
dimensions (null for a call that never got as far as decoding one), line
count, duration — and no image bytes and no OCR text. A row is written on
**every** path, including a 429 before any upload, a 413 mid-upload, and the
500 catch-all.

Two facts worth knowing before pointing a high-frequency caller at this
endpoint:

- **Every authenticated request writes to `api_keys.last_used_at`**
  (`touch_last_used`, inside the `require_api_client` dependency, before the
  route body runs at all). That is a write on the hot path of every single
  call — fine at the volumes this endpoint is sized for, but it means this is
  not a read-only credential check.
- **The rate limiter and the key-lockout counter are both PER PROCESS.** N
  uvicorn workers means N x the configured limit is actually available across
  the fleet. That is an acceptable trade for capacity protection (it still
  stops one key from monopolising a box); it is **not** a billing quota — do
  not size a contract around `OCR_RATE_PER_MINUTE` without knowing the worker
  count.

Monthly review, per the Evaluation section below:

    -- status split per key over the last 30 days
    SELECT k.name, u.status_code, count(*), round(avg(u.duration_ms)) AS avg_ms
    FROM api_key_usage u JOIN api_keys k ON k.id = u.api_key_id
    WHERE u.created_at > now() - interval '30 days'
    GROUP BY 1, 2 ORDER BY 1, 3 DESC;

A rising 503 share means the box is undersized (`OCR_MAX_CONCURRENT`,
`OCR_QUEUE_WAIT_SECONDS`). A rising empty-200 share (200 with `lines == []`)
means upstream image quality, not a code fault. **A nonzero 401 count on a
provisioned key needs a human** — it is either a leak being probed or a
caller with a stale secret. A nonzero 500 count needs a human too — it is a
bug, not a transient condition; find it via the row's key + time, then the
server log around that request.

## Evaluation & Improvement

**Success metric.** Per-call *usable-output rate*: the share of 200 responses
the consuming app accepts without a human re-keying the value. The gateway
cannot observe that directly, so the proxy it owns is `api_key_usage`:
`200-with-nonempty-lines ÷ total`, split by status class, per key.

**Eval.** `tests/test_ocr_api_eval.py`, gated behind `OCR_LIVE_TESTS=1` (needs
the OCR stack, a real model load, and `DATABASE_URL` — skipped otherwise, same
as `tests/test_image_ocr_eval.py` which it builds on). **15 cases**, not the
12/12 exact-match design the original plan described — that design was
replaced because the engine is measurably nondeterministic on Devanagari (the
same fixture returned `नेपाल राषट्र बैंक` on one run and `h राष्ट्र नंक` on
another; see `docs/image-ocr.md` §8). What actually runs:

  * **7 text cases**, imported unchanged from `test_image_ocr_eval.CASES` (4
    English renderings + 3 real NRB scan pages) and applied to `POST /v1/ocr`'s
    JSON body instead of `read_image`'s text block — English is scored on
    exact figures (`expect_all`), Devanagari on aggregates plus an any-of word
    set (`expect_any`/`min_lines`/`min_devanagari`), never on a fixed
    transcription.
  * **2 cases mapped from the tool's error branches** to what the route
    actually answers: a blank image is a `200` with `lines == []` and a fully
    populated `engine` block (not the tool's "no text was detected" string,
    which the HTTP route never emits); a non-image is a `400` containing the
    substring "could not read the image" (not the tool's exact sentence).
  * **5 API-shaped cases with no tool equivalent**, authored directly against
    the route's own contract: corrupt image, pixel bomb, wrong content type,
    oversized upload, scoped-out key (403).
  * One more test (`test_the_whole_eval_set_passes`) runs all of the above as
    a single pass/fail so a partial regression cannot hide inside an otherwise
    green module.

This establishes that the HTTP surface does not degrade what the tool already
achieves; it does **not** establish that the API and the tool are
byte-identical on the same image, which the engine does not support as a
claim. Current pass rate: not run in this environment (needs the OCR stack
live-loaded); re-run with `OCR_LIVE_TESTS=1 DATABASE_URL=... .venv/bin/pytest
tests/test_ocr_api_eval.py -v` before relying on a number.

**Feedback capture.** `api_key_usage` is the log: route, status, bytes,
dimensions, line count, duration, and (for a 200 only) a `request_id` the
caller can quote back. No image bytes and no OCR text are ever stored — the
image is the third party's content, and retaining it would recreate exactly
the confidentiality problem the "usage record only" decision avoided.

**Review loop.** Monthly: the status-class split per key (the SQL above), and
any key with a nonzero 401 or 500 rate — both need a human, for different
reasons (a leaked/stale credential vs. a real bug). Re-run the eval on any
change to `image_ocr.py`, the OCR requirements pin, or the Dockerfile's OCR
build steps; escalate outside the monthly cadence if the eval regresses or if
anyone reports treating an OCR'd figure as fact (a caveat-wording failure, not
an accuracy one — see `docs/image-ocr.md` §8 for the same rule on the chat
path).

**Known unmeasured.** Devanagari OCR *correctness* is the same open question
as `read_image`'s — still the open Nepali review (`docs/nrb-integration.md`
§15) that `docs/image-ocr.md` §8 also points to. This endpoint does not close
it, which is exactly why `authoritative` is always `false` and the caveat
ships on every response.
