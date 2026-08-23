# /v1/ocr eval fixtures

This directory holds no image files of its own. `tests/test_ocr_api_eval.py`
drives `POST /v1/ocr` using the **same** fixtures and thresholds as the
existing 9-case tool eval, `tests/test_image_ocr_eval.py`:

- the four synthetic English renders and three real scanned NRB pages
  (`docs/nrb/ocr-spike-pages/*.jpg`) — imported as `CASES` / `_payload`, not
  copied, so the two evals cannot silently drift apart;
- plus five API-shaped cases that have no tool equivalent at all (corrupt
  image, pixel bomb, wrong content type, oversized upload, scoped-out key).

**This eval does NOT assert that the API and the `read_image` tool return the
same lines for the same image.** An earlier version of this plan proposed
exactly that equality as "the real regression guard." It is wrong: an API
call and a tool call are two separate OCR engine invocations, and
`test_image_ocr_eval.py`'s own docstring records, from measurement, that the
engine's output is not stable run to run on the same bytes (the same
Devanagari fixture returned different text on different runs). An
equality-between-two-invocations assertion would therefore be flaky by
construction — red for reasons that are not regressions, green only by luck.

What this eval actually checks: the API is held to the **same measured
predicates** already frozen in `test_image_ocr_eval.CASES` — the
`expect_all` / `expect_any` / `min_lines` / `min_devanagari` thresholds — now
applied to `POST /v1/ocr`'s JSON body. That establishes the HTTP surface does
not degrade what the tool already achieves; it does not, and cannot, promise
byte-identical output between the two paths.

The two `expect_error` cases in the tool's table (`blank_image_has_no_text`,
`not_really_an_image`) are tool-level text messages ("no text was detected
in this image", "could not read the image (...)") produced by `read_image`'s
own error branches — the HTTP route never emits either string verbatim, so
they are mapped to what the route actually answers instead: a blank image is
a legitimate **200** with empty `lines`/`text` and a fully populated `engine`
block (proof the engine ran and genuinely found nothing — the route's own
docstring is explicit that a missing engine is 503, never an empty 200); a
file that is not really an image is a **400** naming "could not read the
image", raised before the OCR engine is ever reached.

Target: every case in the module passes, as a PR gate. Run with
`OCR_LIVE_TESTS=1` (and `DATABASE_URL` set); skipped otherwise, because the
text cases need the OCR stack, a real model load, and a database to mint an
API key against.
