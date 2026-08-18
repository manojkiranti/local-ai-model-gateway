# Image upload + OCR in chat (`read_image`)

A user can upload an image (`.png/.jpg/.jpeg/.webp/.tif/.tiff/.bmp`) to
`POST /v1/files`, attach it to a turn with `file_ids`, and the model reads its
text with the **`read_image`** tool. English and Nepali/Devanagari.

## §1 Status

| | |
|---|---|
| Upload + validation + attachment summary | **done**, always available |
| `read_image` tool (OCR) | **done**, behind an optional build flag |
| Eval | **9/9** (2026-08-18), `tests/test_image_ocr_eval.py` |
| Live end-to-end (`POST /v1/files` → `POST /v1/chat`) | **verified** 2026-08-18 on both an English payslip and a real Nepali scan — the model called `read_image` unprompted and picked `lang` itself |
| Container, `INSTALL_OCR=true` | **verified** 2026-08-18 — byte-identical output to the venv as uid 10001 with `--network none` |
| Caveat actually relayed to the user by the model | **NOT verified** — see §5 |
| Scanned **PDF** OCR | **not done** — deliberately out of scope (§6) |
| Caching of extracted text | **not done** — measure first (§6) |
| Devanagari conversion *correctness* | **still unmeasured** — the open §15 Nepali review of `docs/nrb-integration.md` |

## §2 Why not the three obvious options

**Not docling**, even though `app/nrb/ocr.py` already runs PP-OCRv5 through it.
Docling is in that path because its input is a PDF *page* and docling is what
rasterises it. Measured in this venv that path costs `torch` 1.1 G +
`transformers` 93 M + docling, and `Dockerfile` installs `requirements.txt`
alone precisely so the API image can never acquire it. `RapidOCR.__call__`
accepts `str | bytes | ndarray | Path`, so for a bare image docling is pure
overhead. **`app/files/image_ocr.py` imports no part of docling.**

**Not a VL model — as the DEFAULT.** Measured head-to-head 2026-08-18 against
the VL models pulled on the dev box, because the blanket claim "VL models invent
numbers" turned out to be too broad and the real trade-off is different.

> **READ THE LATENCY COLUMNS WITH THIS CAVEAT.** Every number below was measured
> on the DEV LAPTOP — Ryzen 7 7445HS (12 threads), **RTX 4050 Laptop with 6 GB
> VRAM**, nothing resident in Ollama. A 7B VL model does not fit usefully in 6 GB,
> so the VL rows are largely CPU inference and their times are a **property of
> this hardware, not of the models**. The live box is 2× A40 (≈92 GB VRAM, §2 of
> `server-and-models.md`), where they would be seconds, and `qwen3-vl:2b`'s
> timeout below is almost certainly the same artifact. **The latency argument
> against a VLM does not survive onto the real server and must not be cited as
> if it did.** What survives is hardware-independent: no confidence signal,
> non-determinism, and text that cannot be told apart from invention. OCR's own
> times DO transfer (rapidocr is CPU-only and the server has 32 Xeon threads).

*Clean English payslip* (ground truth known exactly, 4 figures):

| | figures | latency |
|---|---:|---:|
| PP-OCRv5 (shipped) | **4/4** | **0.86 s** |
| `qwen3-vl:2b` | 4/4 | 15.6 s |
| `qwen2.5vl:3b` | 4/4 | 10.6 s |
| `qwen2.5vl:latest` | 4/4 | 19.3 s |
| `llava:latest` | **0/4** | 23.9 s |

Modern VL models read clean English figures correctly. The danger is not that they
always fail, it is that **when they fail the failure is invisible**: `llava`
returned confident, well-formatted, wholly fabricated values (`Gross 8,500.00`,
`Net 6,532.60`, an invented employer and date) and closed by apologising that the
image was "a bit blurry" — it was a crisp synthetic render. OCR *drops* text and
says so (line count + per-line confidence); a VL model *substitutes* text with
nothing for an ingestion boundary to catch.

*Real scanned Nepali page* (`438c55304da5-p001.jpg`):

| | result | latency |
|---|---|---:|
| PP-OCRv5 | 30 lines, 881 Devanagari chars, 5/5 anchors | **1.63 s** |
| `qwen2.5vl:latest` | 1107 Devanagari chars, 5/5 anchors | **114 s** |
| `qwen3-vl:2b` | **failed — read timeout** | >900 s |

**This is the honest case FOR a VLM, and it is not weak.** `qwen2.5vl:latest`
produced `नेपाल राष्ट्र बैंक` with the ष्ट्र conjunct **correct**, where v5 gives
`राष्टर`; likewise `सञ्चालन` against v5's `सज्चालन`. On orthography — the very
metric §16.6 used to reject PP-OCRv4 — the VL model is better in places.

What keeps it out of the default path — and one argument that does NOT:

1. **Its extra text is unverifiable.** 1107 Devanagari characters against 881 is
   either better recall or confident invention, and **nothing in the output
   distinguishes them.** It also duplicated a header line and wrote `सुचना` for
   `सूचना`. That lands exactly on §15's still-open Nepali review: without a
   reader you cannot tell recovery from fabrication — and a VLM makes that
   question harder, not easier. Hardware-independent, and the real reason.
2. **No confidence signal at all**, per line or otherwise, so an ingestion
   boundary has nothing to read. OCR reports mean/min per line.
3. **Non-deterministic**, so the 9/9 eval in §8 could not exist in this form.
4. **No VL model is pulled on the live box** (only the four in
   `server-and-models.md` §3), and pulling one means touching `nic_ollama` — a
   container in an unrelated compose stack we are told not to treat as ours.
5. **NOT a reason: latency.** The 114 s and the `qwen3-vl:2b` timeout above are
   dev-laptop artifacts (6 GB VRAM, see the caveat at the top of this section).
   On 2× A40 these would be seconds, and VRAM contention with
   `qwen3.5:35b-a3b` is comfortable inside ≈92 GB. This was cited as a reason
   when the decision was taken and is **retracted** — recorded here so the
   decision rests only on 1–4.

So: OCR is the default because it is bounded, auditable and evaluable.
**A VLM is the right instrument for a different job** — interpretation ("what does
this chart show"), which OCR cannot do at all — and belongs beside `read_image`
as an opt-in second tool reusing the same `resolve_file` plumbing, not replacing
it. This measurement is also fresh evidence for revisiting §16.6's **deferred
PaddleOCR-VL**, whose whole premise was logically-ordered Devanagari.

Separately, and independent of the engine: putting the image through
`AGENT_MODEL` itself was rejected because four layers are `str`-typed
(`chat/schemas.py`, `history/models.py`, `history/repository.py`,
`agent/loop.py`), and `AGENT_MODEL` would have to become a VL model whose
tool-calling is weaker than `qwen3.5:35b-a3b`'s.

**Not a new engine.** What is reused is the expensive part: the model + backend
decision in `docs/nrb-integration.md` §16.6. `rapidocr` 3.9.2 needs **no torch**
(`pyclipper, opencv_python, numpy, Shapely, PyYAML, Pillow, tqdm, omegaconf,
requests, colorlog`), and `LangRec` carries both `en` and `devanagari`.

## §3 The dangerous default

`rapidocr/config.yaml` ships `Det.lang_type: "ch"`, `Rec.lang_type: "ch"` and
`ocr_version: "PP-OCRv6"`. **An omitted key does not mean "engine default", it
means Chinese PP-OCRv6** — the same trap `app/nrb/ocr.py` documents for the RAG
converter ("would point a Chinese/English-dictionary recogniser at every
uploaded PDF"). rapidocr also *refuses* plain strings (`The value of
Det.engine_type must be Enum Type`), so a key that fails to convert silently
falls back.

`image_ocr.ocr_config()` therefore declares all eight keys as plain strings —
asserted by a test that runs **even where rapidocr is not installed**, which is
exactly the environment where a silent fallback would go unnoticed — and
`ocr_params()` converts them to enums, with a second test proving every key
survives conversion.

Detection is held **identical to the measured NRB configuration** (`ch`, v5,
mobile): a detector finds text *boxes*, not characters, so it is script-agnostic
and §16.6's evidence carries over unchanged. `lang` moves the recogniser only.

`devanagari` is the default because it is the only single recogniser that reads
**both** scripts — its dictionary includes ASCII, so English comes back exact
(measured: `45,320.75`, `0123456789`), while a latin-only model returns
*nothing* for Nepali. Degraded latin beats absent Devanagari.

## §4 Two findings from building it

**rapidocr returns one box per WORD, not per line.** `Total Amount: 45,320.75`
comes back as three boxes sharing one row. Emitting one word per line would
destroy every table and separate every figure from its label, so
`image_ocr.group_lines` groups boxes into rows by vertical position and orders
rows top-to-bottom, words left-to-right. **Reading order comes from the
geometry, never from the order rapidocr emitted.** A line's score is the
*minimum* of its boxes': a line is only as trustworthy as its worst word.
A blank image returns `txts=None`, not an empty tuple.

**There was no pixel-bomb guard.** `router.py`'s zip-bomb check covers only the
OOXML paths, so a ~200-byte PNG declaring 40000×40000 passed both it and the
10 MB wire cap and would have been decoded. `images.MAX_IMAGE_PIXELS` is checked
**before any decode**, and it does not rely on Pillow's own exception: Pillow
only *raises* above 2× its limit and merely warns between 1× and 2×, so a 1.5×
bomb would have got through. `_KINDS` doubles as a **decoder allowlist** on the
*sniffed* format, so a GIF renamed `.png` never reaches the GIF decoder —
Pillow reaches dozens of decoders and has a history of CVEs in the obscure ones.
Both are in `requirements.txt`-level code (Pillow), not the optional stack: they
are security controls on the upload path and must work with OCR absent.

**A multi-page `.tif` was silently losing pages.** A document scanner's normal
output is a multi-frame TIFF, and the engine reads **frame 1 only** — measured, a
2-frame TIFF returned page 1's text and page 2's simply vanished. So
`ImageSummary.frames` is a reported fact and `read_image` emits a `PARTIAL:` line
naming how many frames were not read, the same honesty rule as
`read_document`'s "pages X–Y were not read". Returning frame 1 as though it were
the whole document is precisely the silent truncation this codebase rejects.

## §5 What the model is told, and why

`read_image`'s result leads with metadata and **carries the caveat in the
header**, not the footer, because `agent/loop.py` truncates from the END — a
trailing caveat is the first thing lost on exactly the long results that most
need it. The text reaching the model is:

> CAVEAT: this is machine-read text (OCR), not a transcription — words and whole
> lines can be dropped or misread. VERIFY every figure, date, account number and
> contact detail against the image itself before relying on it, and say so when
> you quote one.

Per-line confidence is **reported and never enforced**. §16.6 declines to invent
a pass/fail threshold from an orthography measurement, so nothing in
`image_ocr.py` compares a score to a numeric constant — asserted by AST, because
the temptation to add `if score < 0.6: withhold` is precisely what that section
forbids.

**Live finding, 2026-08-18 (`qwen2.5:latest`, 7B):** the caveat reaches the
model, but the model did not pass it on. Asked "what is the net pay on this
payslip?", it called `read_image` unprompted, read `Net Pay: 6,518.00` exactly,
and answered "**NPR** 6,518.00" — inventing a currency that is nowhere in the
image and relaying no verification warning, despite the caveat's "say so when you
quote one". The extraction was right and the *presentation* was not. Two things
follow: this must be re-checked against the production model
(`qwen3.5:35b-a3b`), and if it reproduces there the fix belongs in
`build_system_prompt`, not in a longer tool caveat — the same place
`DATE_PROMPT` already forbids answering time-varying figures from memory.
Recorded, not fixed.

Routing is by tool description and by the attachment note, both of which name
`read_image`; `read_document` refuses an image and points here, and this tool
refuses a PDF and points back — a PDF has a text layer, and OCR'ing page 1 as a
picture would discard it silently.

## §6 Deliberately not done

- **Scanned PDFs.** `read_document` keeps saying "OCR is not available yet". The
  boundary is shaped so they drop in: the engine takes an image, and a future
  caller feeds rasterised pages.
- **Caching extracted text.** `generated_files` has no metadata column, so a
  cache means a migration. `agent/loop.py`'s `call_cache` covers within-turn
  repeats; a *later* turn re-OCRs (~0.5–1 s for a screenshot). Measure first.
- **An `OCR_ENABLED` runtime switch.** The build flag is the switch, and
  `Settings` uses `extra="ignore"` so a typo'd env var is silently dropped. Easy
  to add if a deployment needs to disable OCR without a rebuild.
- **A `lang` default setting.** Unnecessary: the default recogniser reads English
  correctly, and the tool takes a per-call `lang`.
- **Any VL/vision model — DECIDED, not merely unbuilt (2026-08-18, user's call).**
  No `VISION_MODEL` setting, no `describe_image` tool, no image content parts in
  `/v1/chat`. §2 holds the measured comparison, including the parts that favour a
  VLM, so this is a decision taken against the evidence rather than in ignorance
  of it. Do not reopen it from that table alone. Note that one argument made at
  the time — latency — was **retracted** once it was established the benchmark
  ran on a 6 GB dev laptop rather than the live 2× A40 box (§2, point 5); the
  decision stands on the surviving reasons, all of which are hardware-independent.
  **Reopen only on one of:**
  (a) users actually asking for image *interpretation* (charts, dashboards,
  screenshots) rather than transcription — that is a job OCR cannot do at all,
  and the answer is an additive second tool, never a change to `AGENT_MODEL`;
  (b) the §15 Nepali reader review finding v5's conjunct errors
  (`राष्टर` for राष्ट्र) disqualifying — in which case the candidate is §16.6's
  already-deferred **PaddleOCR-VL**, not a general chat VL model.

## §7 Operating it

```bash
# venv
.venv/bin/pip install -r requirements-ocr.txt

# container (default build has NO OCR stack)
docker compose build --build-arg INSTALL_OCR=true gateway
```

**Verify a build by its OUTPUT on a known image, never by "the upload
succeeded"** — §18's rule. A missing native lib, an unwritable model directory
and an absent model all present as a *clean* deployment; the Dockerfile's warm
step is deliberately not `|| true` so a build that cannot fetch weights fails
loudly instead.

Without the flag, uploads still work and `read_image` answers
`ERROR: image OCR is not enabled on this deployment.`

## §8 Evaluation & Improvement

**Success metric.** Share of image-upload turns where the user does not
immediately re-ask, re-upload, or correct the extracted text — the nearest
available proxy for "the image was usable". Reported next to the raw count of
`read_image` calls, so a good rate on a tiny denominator is not read as success.

**Eval.** `tests/test_image_ocr_eval.py` — **9 labelled cases, 9/9 as of
2026-08-18.** Four English renderings (clean, rotated 3°, low-contrast, 16 pt),
**three real scanned NRB pages** committed at `docs/nrb/ocr-spike-pages/`, one
blank image, one non-image renamed `.png`. English is scored on exact figures;
Devanagari on aggregates (line count, Devanagari character count) plus an
any-of word set, **never on a fixed transcription** — the same fixture returned
`नेपाल राषट्र बैंक` on one run and `h राष्ट्र नंक` on another, and pinning a
transcription would encode a bug as an expectation and fail on an upgrade that
improved the average. Thresholds sit below measured (18/30/37 lines,
506/881/1423 Devanagari chars); every case passed before the numbers were
written down.

**Feedback capture.** `read_image` returns dimensions, engine, lang, line count
and mean/min confidence in every result, and the whole result is persisted in
`chat_messages.trace` (the audit record, written whether or not `EXPOSE_TRACE`
is on), so a disputed extraction can be reconstructed without storing anything
new. No new table.

**Review loop.** Monthly: pull the lowest-confidence decile from the traces,
read 10, and choose one of — leave it, add a case to the eval set, or change the
recogniser default. Two triggers escalate outside that cadence: the eval
dropping below 9/9, and **any report of an OCR'd figure being taken as fact**,
which is a caveat-wording bug rather than an accuracy bug.

**Known unmeasured.** Devanagari *correctness* is still the open §15 Nepali
review. This feature does not close it — which is exactly why the caveat ships
inside the tool's output rather than in this document.
