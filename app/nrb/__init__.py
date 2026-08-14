"""Nepal Rastra Bank integration (official NRB APIs and published sitemap).

Two pieces, both with the same trust model: the host is application config
(`NRB_API_BASE_URL`, `NRB_SITE_BASE_URL`) and paths are hardcoded — the model
never supplies a host, so neither is a second `fetch_url`.

  * `client` — the Forex API, behind the `get_nrb_forex` tool (Phase 1).
  * `http` — the shared host guard, URL normalization and `FetchError` vocabulary.
    One trust boundary for all of the below.
  * `sitemap` + `classify` — sitemap discovery and URL classification (Phase 2:
    what URLs exist).
  * `wp_api` + `documents` + `attachments` + `page` — document discovery through
    NRB's WordPress REST API (Phase 3: what those URLs are and where their files
    live).
  * `discovery` + `records` + `models` + `catalog` + `sync` — the persistent
    catalog (Phase 4: what NRB publishes, reconciled into Postgres). `discovery`
    reads the whole corpus, `records` turns it into rows (pure), `models` is the
    schema, `catalog` is set-based data access, `sync` is the idempotent
    reconciliation. **Nothing is downloaded** — Phase 4 records where files are,
    not what is in them.
  * `sniff` + `filestore` + `fetch` + `locks` — the file download (Phase 5: the
    bytes themselves). `sniff` types a body from its magic bytes (pure, stdlib
    only), `filestore` is the content-addressed blob store, `fetch` is one
    resumable pass, `locks` is the advisory-lock rule shared with `sync`.
    **Nothing is parsed** — a stored blob is a raw artefact; what it *says* is
    Phase 6's question.
  * `report` — aggregation and rendering for the inventories, a sync run and a
    fetch run.

Only `client` is model-facing. Nothing else here is registered in `LOCAL_TOOLS` or
reachable from any endpoint; the inventories, the sync and the fetch are run by
hand via `scripts/nrb_sitemap_inventory.py`, `scripts/nrb_document_inventory.py`,
`scripts/nrb_sync.py` and `scripts/nrb_fetch.py`.
"""

from .client import ForexDay, NRBError, Rate, fetch_forex_rates

__all__ = ["ForexDay", "NRBError", "Rate", "fetch_forex_rates"]
