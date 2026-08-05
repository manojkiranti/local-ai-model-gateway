# create_chart — server-rendered SVG chart tool — Design

_2026-08-04 · Local LLM Gateway_

## Goal
A `create_chart` local tool alongside `create_excel`/`create_html`: the model
supplies **structured data**, the gateway **renders a static SVG**, saved to the
file store and returned as a `GET /v1/files/{id}` link. Bar, line, pie.

## Decisions (locked with user)
- **Server-rendered SVG** (not JS/Chart.js, not matplotlib/PNG). Rationale: keeps
  "model supplies data, gateway renders" (same as create_excel), needs no new
  dependency, and runs **zero script** — so it's safe under the existing sandbox
  and safe as an `<img>`. Interactivity (hover tooltips) is knowingly out of scope:
  a downloadable static image can't be interactive. Acceptable tradeoff.
- **Types:** bar, line, pie.
- **Palette:** the dataviz skill's validated categorical palette (light surface).
  Validated via `validate_palette.js` — ALL CHECKS PASS; the contrast WARN is
  discharged by always shipping a legend (≥2 series) + axis/value/slice labels, so
  identity is never color-alone.

## Tool contract
Args (model-supplied; validation returns a friendly `ERROR:` string, never raises):
```jsonc
{ "chart_type": "bar"|"line"|"pie",       // required
  "title": "…",                            // optional
  "labels": ["Q1","Q2","Q3","Q4"],         // required, non-empty strings
  "series": [{"name":"2025","data":[…]}],  // required, ≥1; data numeric
  "filename": "chart.svg" }                 // optional (default chart.svg; .svg forced)
```
- bar/line: one mark-group/polyline per series; multi-series → legend.
- pie: uses `series[0]`; each value → a slice over `labels`; negatives rejected.
- Errors: bad/absent `chart_type`; empty `labels`/`series`; non-numeric data;
  any `series.data` length ≠ `labels` length; pie with negative/zero-sum data.

Return string mirrors excel/html exactly:
`Created chart '<file>' (<n> bytes, <type>). Download it at: GET /v1/files/{id}`

## Rendering (app/tools/local/_svg.py, pure + sync)
- Self-contained SVG: `viewBox`, inline styles, **no `<script>`, no external refs**.
- Light surface `#fcfcfb`, ink `#0b0b0b`/`#52514e`, recessive gridlines.
- `role="img"` + `<title>`/`<desc>` for accessibility.
- Marks per skill: 2px lines, ≥8px line markers, rounded/​gapped bars, legend for
  ≥2 series, selective direct labels (values on bars when few; % on pie slices).
- Saved with `media_type="image/svg+xml"` (new `SVG_MEDIA_TYPE` in files/store);
  `/v1/files/{id}` already sends `nosniff`.

## Frontend
Fetch `GET /v1/files/{id}` with the bearer header → blob → **render via
`<img src={blobURL}>`** (an `<img>`-loaded SVG never executes scripts) + a Download
button. Parse the link from the tool result / trace, same as excel/html. Branch on
`Content-Type: image/svg+xml`.

## Testing (TDD, offline)
`tests/test_create_chart.py`: (a) each type renders a non-empty `<svg …>` with no
`<script>`; (b) validation errors for bad type / empty labels / length mismatch /
non-numeric / negative pie; (c) the return string carries a `/v1/files/{id}` link
and the file is retrievable from the store with the SVG media type.

## Out of scope
Interactivity/hover, dark-mode variants (an `<img>` can't adapt), stacked bars,
axis-scale/log options, >8 series (fold to "Other" later), scatter/area.
