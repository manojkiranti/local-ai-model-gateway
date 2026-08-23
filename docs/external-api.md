# The external API: API keys, `POST /v1/ocr` and `POST /v1/extract`

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

**Prerequisite: cap the body size at the reverse proxy, before the gateway
ever sees it.** `POST /v1/ocr` is an ASGI app's own request handling reading a
multipart body, and FastAPI parses the whole form (spooling any file part to a
temp file) before ANY dependency — including the API-key check — ever runs.
The gateway does what it can: a `Content-Length`-based middleware in front of
this one route rejects a request whose DECLARED size is over
`OCR_MAX_UPLOAD_BYTES` before the body is read at all, and the route itself
counts bytes as it streams the body and cuts it off mid-transfer past the same
cap. Neither one stops the bytes from arriving on the wire in the first place
— a client that lies about its own `Content-Length`, or a chunked request that
omits the header entirely, sails past the middleware exactly as it would past
any other declared-length check. Only a reverse proxy in front of the gateway
can do that. Set it there too:

    # nginx
    client_max_body_size 12m;

(a little above `OCR_MAX_UPLOAD_BYTES`'s default 10 MB, to leave room for
multipart framing overhead). Without this, an attacker holding no API key at
all can still make the gateway spend CPU and disk reading and spooling an
oversized body before answering 401 — concurrent repeats fill the disk. This
is the same shape of gap `POST /v1/files` has always had; it matters more here
because `/v1/ocr` is the first endpoint in this gateway that accepts uploads
from OUTSIDE the organisation.

**Prerequisite: do not put this gateway behind a path-prefixing reverse
proxy (`--root-path`) without checking `UploadContentLengthGuard` first.** The
guard matches `scope["path"]` against `/v1/ocr` EXACTLY. Under a proxy that
forwards under a prefix and runs Uvicorn/Gunicorn with e.g. `--root-path
/api`, `scope["path"]` becomes `/api/v1/ocr` and the guard's comparison never
matches — it silently stops existing, with no error and no failing test,
while the rest of the request pipeline (auth, the route's own streamed byte
cap) still works. This gateway is not currently deployed behind such a proxy,
so it is not a live gap, but it is exactly the kind of change that would
reintroduce one without anyone noticing at deploy time.

**Verify a deployment by making a real call with a known image, never by
whether the container started.** `docs/nrb-integration.md` §18 found five
distinct OCR deployment defects that all produced *successful* operations with
no text.

**`OCR_PREWARM=true`** loads the three ONNX models at process startup instead
of charging the ~0.7s load (plus onnxruntime's first-inference warmup) to
whichever caller happens to make the first request. Off by default. Failure to
load is logged and ignored either way — a deployment without the OCR stack
installed still boots, and `/v1/ocr` answers its own 503 on the first call.

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
version}, request_id}`. `image.kind` is the human string `images._KINDS` maps
the sniffed Pillow format to (`"PNG image"`, `"JPEG image"`, `"WebP image"`,
`"TIFF image"`, `"BMP image"`) — **not** the bare format name (`"png"`).

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
| 413 | Over `OCR_MAX_UPLOAD_BYTES` (default 10 MB) — from the Content-Length guard before the body is read, or from the route's own streamed count if the caller understated its Content-Length | Downscale before sending |
| 429 | This key's rate limit (`OCR_RATE_PER_MINUTE`/`OCR_RATE_BURST`) — detail "Rate limit exceeded for this API key" | Honour `Retry-After` |
| 429 | This key's PREFIX is credential-locked (`API_KEY_MAX_ATTEMPTS` bad attempts within `API_KEY_ATTEMPT_WINDOW_SECONDS`) — detail "Too many failed attempts for this key" | This is checked BEFORE the secret or scope, so it can fire on a perfectly good key whose prefix was probed by someone else. Wait for `Retry-After`; if it recurs, ask an admin to check for a leaked/probed prefix |
| 503 | The OCR PACKAGE itself is not importable (`image_ocr.available()` is `False` — `INSTALL_OCR` was not set at build time, or rapidocr/onnxruntime are genuinely absent), **or** the box is at capacity right now (`OCR_MAX_CONCURRENT`/`OCR_QUEUE_WAIT_SECONDS`) | Retry on `Retry-After`; if the detail says "not enabled", it is a deployment fault (rebuild with `INSTALL_OCR=true`), not a transient one |
| 500 | `available()` is `True` (the package imports) but the call still failed — either the ONNX models could not be BUILT (root-owned model dir, a missing lexicon, a `torch.compile` failure — the §18 deployment-defect class in `docs/nrb-integration.md`) or an unexpected exception from a specific image while the engine itself is fine | **Report it, do not blindly retry.** The real exception is logged server-side and never returned to the caller (the client only ever sees the generic detail `OCR failed unexpectedly`) — this is a server fault, and retrying the same image against the same bug wastes a call. Both causes log the same line, `ocr unavailable (request <id>): <exc>` — the `<exc>` text is what distinguishes them: `could not load the OCR engine (<ExceptionType>)` means the MODELS failed to build (this is the deployment-defect class, and it is diagnosable from that message alone), anything else is a genuinely unexpected per-image failure. Ask an operator to check the logs around the `request_id`/time of the call. |

401 is one message for all six credential causes on purpose — distinguishing
them tells an attacker which prefixes are real. The server log distinguishes
them; ask an operator. **Only three of those six causes leave a usage row**
(see Operating, below, for exactly which).

**The uniform-401 property holds for the response body and headers, but not
for latency.** The three attributable causes (wrong secret, revoked, expired)
write a usage row before answering, which is measurably slower than an
unknown-prefix or absent-header rejection (medians 2.91 ms vs 1.82 ms over 30
samples) — so a caller with no valid credential at all can, in principle,
learn by timing whether an 8-character PREFIX is provisioned. This is
accepted as-is rather than engineered around: a prefix is a NON-secret lookup
handle by design (`app/apikeys/models.py` stores it in plaintext precisely
because it is not a secret), the secret half is 256 bits, and knowing a
prefix is real buys an attacker no access — so a timing side-channel on that
one fact is not worth a nullable FK and a sentinel-row migration to close.
R2's fix (above) already removes the timing differential on the lockout path,
since a locked-out prefix now costs the same near-zero time whether or not it
belongs to a real key.

**`X-Request-Id` is on the 200 response only.** It is set on the FastAPI
`Response` object early in the handler, but every non-200 path raises an
`HTTPException`, and FastAPI builds the error response from the exception
directly — the header set on the (unused) success `Response` never reaches
the client. So there is **no request id to quote on an error**, including the
500. Do not tell a caller "check the `X-Request-Id` header" for anything but a
successful call. To find the row for a *failed* call, locate it by key +
approximate time + status instead — when the failure is attributable to a
real key at all (see Operating, below, for exactly which outcomes get a row);
it is not addressable by an id the client never received either way.

## Revoking

    curl -X DELETE localhost:8000/v1/api-keys/<id> -H "authorization: Bearer $TOKEN"

204 on success, 404 if the key does not exist or is already revoked. Takes
effect on the holder's next call. The row is kept (`is_active=false` +
`revoked_at`) so the key's usage history stays attributable — keys are never
deleted.

## Operating

`api_key_usage` holds one row per call: route, status, bytes in, image
dimensions (null for a call that never got as far as decoding one), line
count, duration — and no image bytes and no OCR text.

**A row needs a real `api_keys.id` to attach to, so it is written for every
outcome EXCEPT two.** The route's own checks (rate limit, upload guards, OCR
failure) always run with an authenticated `ApiClient` already resolved, so
every one of those — the 429 rate limit, the 413s, the 400s, the 503s, the
500 — gets a row. Inside `require_api_client` itself: of the SIX 401
credential-rejection causes, **three** have a real key in hand and get a row —
wrong secret, revoked, expired (an earlier version of this doc said "four",
folding in the missing-scope 403 and the credential-lockout 429, neither of
which is one of the six). The remaining two of the six — the header absent or
malformed, and an unknown prefix — never resolve to a key row at all, so
there is nothing to attach a usage row to; they leave no row, by
construction, not by omission. A missing-scope 403 is a separate outcome
(the credential is genuine) and always gets its own row.

**The credential-lockout 429 gets exactly ONE row per lockout episode, not
one per request.** Before a fix on 2026-08-23, every request that arrived
while a prefix was already locked did its own `find_by_prefix` lookup plus a
`record_usage` + `commit` — so a leaked prefix with no valid secret at all
turned the cheapest possible rejection into the most expensive one, at
whatever rate the network allowed, for the whole `API_KEY_LOCKOUT_SECONDS`
window, with zero authentication cost to the caller. The one attributable row
is now written by the specific request whose own credential-failure call
trips the lock; every later request while still locked is answered with no
database access at all. An earlier version of this doc claimed a row "on
every path", which was never true for the two unattributable causes above,
and briefly implied "every locked request", which was true but not by
design.

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
- **The credential-lockout tuning (`API_KEY_MAX_ATTEMPTS` /
  `API_KEY_ATTEMPT_WINDOW_SECONDS` / `API_KEY_LOCKOUT_SECONDS`) is SEPARATE
  from `LOGIN_MAX_ATTEMPTS`.** That setting must stay below the AD domain's
  own lockout threshold — a bank-critical constraint on human logins that has
  nothing to do with an external integrator's key. Raise the API-key knobs,
  never the login one, to accommodate a caller's retry behaviour.
- **`api_key_usage` has no retention policy** — one row per call, never
  pruned, `ON DELETE RESTRICT` against the owning key so a row cannot outlive
  evidence of who made the call. Important 3 (above) means every 401/403/429
  now writes one too, not just successful and 400+ calls, so the table grows
  faster than it did at launch. There is no automatic cleanup: an operator
  should schedule a periodic

      DELETE FROM api_key_usage WHERE created_at < now() - interval '1 year';

  (or whatever retention period the deployment's audit/compliance policy
  calls for) rather than let it grow unbounded. Nothing in this codebase runs
  that for you.

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

# `POST /v1/extract`: text and structure of one document

Same `EXTERNAL_API_ENABLED` switch, same `X-API-Key` header, a different
scope (`document:read` rather than `ocr:read` — a key minted for one cannot
reach the other, in either direction:
`tests/test_extract_api_integration.py::test_an_ocr_only_key_is_403_not_401`
and `::test_a_document_read_key_cannot_reach_the_ocr_route`). Everything under
"Turning it on", "Minting the first key" and "Revoking" above applies
unchanged. This section covers what is specific to `/v1/extract`.

## Calling it

    curl -s -X POST localhost:8000/v1/extract \
      -H "X-API-Key: lgw_live_..." \
      -F file=@payslip.pdf | jq

Accepted extensions, and which route each family takes:

| Extension | Route | Notes |
|---|---|---|
| `.pdf` `.docx` `.txt` `.md` `.json` | `native` | Read from the document's own text layer. A PDF with no text layer is a **422**, not empty text — see below. |
| `.xlsx` `.csv` | `native` | Returned as `sheets`, never flattened into `text` (`text` is `""` for these two). |
| `.png` `.jpg` `.jpeg` `.webp` `.tif` `.tiff` `.bmp` | `ocr` | The same engine `/v1/ocr` uses. `lang` (`devanagari` default, or `en`) applies to these only. |

There is no field-extraction mode here (`/v1/extract/fields` is a separate,
not-yet-built endpoint) and no model call happens on this path at all — a key
provisioned only for `document:read` cannot buy model access by adding a form
field.

## Reading the response

Body shape: `{kind, text, lines[{text, confidence}], sheets[{name, headers,
rows, total_rows, truncated}], source{route, authoritative, caveat?, pages,
text_pages, pages_skipped, partial}, request_id}`.

**Read `source` first.** `route: "native"` means the text came from the
document's own text layer and is exact: `authoritative` is `true` and
**`caveat` is absent from the JSON entirely — not `null`, absent.** `route:
"ocr"` means it was machine-read exactly like `/v1/ocr`: `authoritative` is
`false`, `caveat` is present, and no figure, date, account number or contact
detail should be treated as correct without checking it against the original.

The reason `caveat` is dropped rather than sent as `null` for a native source,
not just omitted by convention: `/v1/ocr` only ever sees images, so an
unconditional caveat is right there. This endpoint also reads DOCX, PDF text
layers, XLSX and CSV, whose text is exact — attaching the same warning to
those trains a reader to ignore it, and then it is missing on the one page
that actually needed it (an OCR'd page inside an otherwise-native PDF; see
`docs/nrb-integration.md` §29.2, and `app/rag/sources.py`'s identical rule for
native NRB chat citations). The wording itself, when it does appear, is the
*same* `image_ocr.OCR_CAVEAT` constant `read_image` and `/v1/ocr` already use
— three readers, one constant
(`tests/test_ocr_api_boundaries.py::test_the_caveat_is_one_constant_with_three_readers`).

`sheets` is populated for `.xlsx`/`.csv` only and empty for every other
format; `text` is the inverse (empty for a spreadsheet, populated for
everything else). `lines[].confidence` is `null` for a native source (nothing
uncertain to report) and populated only when `route == "ocr"` — reported,
never enforced, same as `/v1/ocr`.

## Status codes

| Code | Meaning | What the caller should do |
|---|---|---|
| 400 | Unsupported extension, empty upload, a corrupt/unreadable file, or a bad `lang` | Fix the input |
| 401 | The key is absent, malformed, unknown, wrong, revoked or expired — one message for all six causes | Check the secret; ask an admin whether it was revoked |
| 403 | The key is genuine but lacks `document:read` | Ask an admin to re-mint with the scope. Do NOT rotate the key |
| 413 | Over `EXTRACT_MAX_UPLOAD_BYTES` (default 25 MB) — from the Content-Length guard before the body is read, or from the route's own streamed count if the caller understated or omitted its Content-Length | Downscale/split before sending |
| 422 | A PDF whose pages carry no text layer at all — a scanned document with no OCR available for it on this endpoint | Do not retry as-is. `/v1/extract` never OCRs a PDF (unlike an image upload, where OCR is the whole point); the caller needs either a text-layer version of the document or to route the individual page images through `/v1/ocr` itself |
| 429 | This key's rate limit (`EXTRACT_RATE_PER_MINUTE`/`EXTRACT_RATE_BURST`) or its prefix is credential-locked — same two-cause split as `/v1/ocr`, distinguished by `Retry-After` and the detail text | Honour `Retry-After` |
| 503 | The box is at capacity right now (`EXTRACT_MAX_CONCURRENT`/`EXTRACT_QUEUE_WAIT_SECONDS`), or — for an image upload only — the OCR stack is not installed on this deployment | Retry on `Retry-After`; a "not enabled" detail is a deployment fault (rebuild with `INSTALL_OCR=true`), not a transient one |
| 500 | An unexpected failure — including the response body failing to build after a genuinely successful extraction (a `zip(strict=True)` length mismatch between lines and confidences) | Report it; the real exception is logged server-side and never echoed. Exactly **one** usage row is written for this outcome, never a false 200 alongside it — see `tests/test_extract_api_integration.py::test_a_response_build_failure_is_500_with_exactly_one_row_no_false_200` |

Same 401 uniformity, same `X-Request-Id`-on-200-only rule, same
per-attributable-cause usage-row split as `/v1/ocr` — see those sections
above; none of it differs for this route.

## Two things that differ from `/v1/ocr`, and one that does not

- **The upload cap is bigger, and the reverse-proxy prerequisite now has to
  cover BOTH.** `EXTRACT_MAX_UPLOAD_BYTES` defaults to **25 MB** against
  `/v1/ocr`'s 10 MB (a PDF or DOCX is routinely bigger than a phone-camera
  scan). The nginx `client_max_body_size` prerequisite in "Turning it on"
  above must be sized for the **larger** of the two caps actually configured,
  with headroom for multipart framing — e.g. `client_max_body_size 30m;` at
  the current defaults, not `12m`. Sizing it to only the OCR cap would make
  nginx itself reject a legitimately-sized `/v1/extract` upload before this
  gateway ever saw it, with nginx's own error page instead of this route's
  413 JSON body.
- **The `--root-path` path-matching caveat now applies to two routes, not
  one.** `UploadContentLengthGuard` (and `UPLOAD_CAPS`, the table it reads)
  covers `/v1/ocr` and `/v1/extract` by exact `scope["path"]` match. Behind a
  path-prefixing reverse proxy running with `--root-path` (e.g. `/api`), both
  paths gain the prefix and the guard silently stops matching *either* one —
  same failure mode as before, just twice the surface. This is still not a
  live gap (this gateway is not currently deployed behind such a proxy), and
  still a deploy-time prerequisite rather than a suffix-match guess, for the
  same reasons given above.
- **The wording of a route-level 413 is NOT the same string on the two
  routes, even though the shared middleware's 413 is.**
  `UploadContentLengthGuard` (the pre-auth, declared-`Content-Length` guard)
  says `"upload exceeds the … MB limit"` for both paths — one string, one
  `UPLOAD_CAPS` table. But each route's OWN streamed-count check (the one
  that catches a caller who understated or omitted `Content-Length`) is
  independent code: `/v1/extract`'s says `"upload exceeds the … MB limit"`
  too, while `/v1/ocr`'s own check still says `"image exceeds the … MB
  limit"` (`app/publicapi/ocr_router.py`, not touched by this change). So a
  413 from `/v1/ocr` can read either way depending on which of the two guards
  caught it, while a 413 from `/v1/extract` always reads the same regardless
  of which guard caught it. Documented as the current state, not fixed here —
  changing `ocr_router.py`'s wording is a one-line, separate change with its
  own review, not a side effect of adding a second route.
- **Its own rate bucket, same shape as `/v1/ocr`'s.** `EXTRACT_RATE_PER_MINUTE`
  / `EXTRACT_RATE_BURST` are independent settings from `OCR_RATE_PER_MINUTE`
  / `OCR_RATE_BURST`, enforced by a separate `RateLimiter` instance
  (`get_extract_rate_limiter()`), **per process** like every other limiter in
  this codebase — N uvicorn workers means N × the configured limit is
  actually available across the fleet. A key holding both scopes is throttled
  against each route independently; spending the OCR bucket does not touch
  the extract one.

## Evaluation & Improvement

**Success metric.** Same shape as `/v1/ocr`'s: the share of `200` responses
the consuming app uses without a re-parse or a manual fix-up. Not directly
observable from the gateway, so the owned proxy is `api_key_usage`: the
status-code split for `route = 'POST /v1/extract'`, plus the non-empty-text
rate per key (a `200` with `text == ""` and `sheets == []` — which should
essentially never happen for a native document, since anything that would
produce it goes through the 422 branch first).

**Eval.** `tests/test_extract_api_eval.py`, gated only on `DATABASE_URL` (no
live model, no OCR stack — every case is a native format built in-process).
Unlike `tests/test_ocr_api_eval.py`, this one asserts **exact** output: native
extraction is deterministic (the same DOCX yields the same lines every run),
so exact assertions are both possible and correct here, whereas the OCR eval
next door scores aggregates because that engine is measurably nondeterministic
on Devanagari. The image case is deliberately excluded rather than re-scored
under looser assertions — that engine is already evaluated in
`test_ocr_api_eval.py`, and re-scoring it here would just import its
nondeterminism into a file whose whole point is exactness. 7 cases (`.txt`,
`.md`, `.json`, `.docx`, `.xlsx`, `.csv`, and a dedicated
native-carries-no-caveat check) plus one aggregate pass/fail so a partial
regression cannot hide inside an otherwise-green module.

**Pass rate measured 2026-08-23: 8/8** (`.venv/bin/pytest
tests/test_extract_api_eval.py -q` → `8 passed`).

**Feedback capture.** `api_key_usage` only, same columns and same absence of
content as `/v1/ocr`'s row — route, status, bytes in, `lines_out`, duration,
and (on a 200) a `request_id`. No document bytes and no extracted text are
ever stored.

**Review loop.** Folded into the same monthly review as `/v1/ocr`'s (the SQL
in "Operating" above, scoped to `route = 'POST /v1/extract'`): the status-code
split per key, a rising 422 share (more scanned PDFs arriving than expected —
a caller-side signal, not a code fault), and any nonzero 401/500 count, which
always needs a human. **Re-run the eval on any change to
`app/files/documents.py`, `app/files/readers.py`, or the
pypdf/openpyxl/python-docx version pins** — none of those files are covered
by a live model or an external service, so there is no excuse for a change to
any of them landing without a green `test_extract_api_eval.py` run.
