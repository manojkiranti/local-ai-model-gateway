# Department-scoped RAG — Design

**Date:** 2026-08-08
**Status:** design, awaiting review

## Goal

A user opens the **HR** tab, asks a question, and the assistant answers from HR
documents only — with citations, and with a refusal when the answer isn't in the
corpus. Same for IT, Finance, and any department added later.

Two properties are non-negotiable because this is a bank:

1. **Department isolation is a database invariant, not a convention.** A Finance
   user must not be able to reach HR content through any path, including a
   prompt-injected tool call.
2. **Ungrounded answers are failures.** "I couldn't find this in the HR
   documents" is a correct response. A plausible answer from the base model's
   parameters is not.

## Approach (chosen forks)

- **Access via `user_departments` join table**, not a column on `users`. People
  sit across HR and Finance; a single-department column breaks on the first
  manager. Rejected: no data-layer access control (any bug leaks across tabs).
- **Hybrid retrieval — dense + full-text, RRF-fused** — from day one, because the
  lexical channel is a *column* on `document_chunks` and adding it later means a
  table rewrite. Banking queries are full of exact tokens (product codes, policy
  numbers, acronyms) that dense embeddings blur.
- **A reranker is in scope, not deferred**, because abstention depends on it. RRF
  scores are rank-derived and carry no absolute relevance signal, so nothing in
  the fusion output can support "I don't know" — see Reranking and abstention.
- **Docling for PDF/DOCX parsing**, accepting a heavy dependency, because the
  `page_number`/`section`/`element_type` columns are only as good as the parser
  that fills them. Constrained by lazy-import and a worker split.
- **One `document_chunks` table, no partitioning.** See "Measured, not assumed"
  below — at realistic v1 scale the planner does exact KNN and never touches
  HNSW. Partition when measurement says to, not on a chunk-count rule of thumb.
- **Admin-only ingestion via a separate path.** Department corpora are curated
  org knowledge with a different ownership model than per-user `generated_files`
  (which CASCADEs on user delete — deleting an employee must not delete a policy).
- **Ingestion is async via `ingest_jobs` + Postgres as the queue.** No Redis in
  this project; `FOR UPDATE SKIP LOCKED` gives a correct multi-worker claim with
  zero new infrastructure.

## Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- 0.8.5 verified available

CREATE TABLE departments (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(32)  NOT NULL UNIQUE,   -- 'hr','it','finance' — what the tab sends
    name        VARCHAR(128) NOT NULL,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The permission boundary. Absence of a row = no access.
CREATE TABLE user_departments (
    user_id       INT NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    department_id INT NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    granted_by    INT          REFERENCES users(id)       ON DELETE SET NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, department_id)
);

CREATE TABLE documents (
    id             VARCHAR(32) PRIMARY KEY,       -- uuid4().hex, matches chat/files convention
    department_id  INT NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
    title          VARCHAR(512) NOT NULL,
    source         VARCHAR(16)  NOT NULL,         -- 'upload' | 'manual'
    file_type      VARCHAR(16)  NOT NULL,         -- pdf|docx|xlsx|csv|text
    file_name      VARCHAR(255),                  -- NULL for typed-in text
    storage_key    VARCHAR(1024),                 -- RELATIVE key under RAG_DOCS_DIR
    content_hash   CHAR(64)     NOT NULL,         -- sha256 of bytes, or of the typed text
    status         VARCHAR(16)  NOT NULL DEFAULT 'pending',  -- pending|ready|failed|archived
    CONSTRAINT ck_documents_status CHECK (status IN ('pending','ready','failed','archived')),
    CONSTRAINT ck_documents_source CHECK (source IN ('upload','manual')),
    embed_model    VARCHAR(128),                  -- audit: what actually embedded this doc
    embed_dim      INT,
    chunk_count    INT NOT NULL DEFAULT 0,
    meta           JSONB NOT NULL DEFAULT '{}'::jsonb,   -- attr `meta`, column "metadata"
    uploaded_by    INT REFERENCES users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, department_id)            -- target for the chunk composite FK
);
CREATE INDEX ix_documents_department ON documents (department_id);
CREATE INDEX ix_documents_status     ON documents (status);

-- Dedup among NON-archived rows only. Archiving deletes chunks but KEEPS the
-- row for audit, so a plain UNIQUE (department_id, content_hash) would
-- permanently block re-adding a file that was once archived.
CREATE UNIQUE INDEX ux_documents_active_content
    ON documents (department_id, content_hash) WHERE status <> 'archived';

CREATE TABLE document_chunks (
    id             BIGSERIAL PRIMARY KEY,
    document_id    VARCHAR(32) NOT NULL,
    department_id  INT NOT NULL,
    chunk_index    INT NOT NULL,
    content        TEXT NOT NULL,
    embedding      vector(1536) NOT NULL,          -- Qwen3 2560 → MRL-truncated
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    token_count    INT,
    page_number    INT,                            -- PDF only
    section        VARCHAR(512),                   -- "Leave Policy > Annual Leave"
    element_type   VARCHAR(32),                    -- text|heading|table|list
    meta           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index),
    FOREIGN KEY (document_id, department_id)
        REFERENCES documents (id, department_id) ON DELETE CASCADE
);

CREATE INDEX ix_chunks_embedding ON document_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX ix_chunks_tsv        ON document_chunks USING gin (tsv);
CREATE INDEX ix_chunks_department ON document_chunks (department_id);

CREATE TABLE ingest_jobs (
    id            VARCHAR(32) PRIMARY KEY,
    document_id   VARCHAR(32) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status        VARCHAR(16) NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed
    CONSTRAINT ck_ingest_jobs_status
        CHECK (status IN ('queued','running','succeeded','failed')),
    chunks_total  INT,
    chunks_done   INT NOT NULL DEFAULT 0,   -- EMBEDDING progress; inserts all land at COMMIT
    attempts      INT NOT NULL DEFAULT 0,
    error         TEXT,
    heartbeat_at  TIMESTAMPTZ,              -- drives the stale-job sweep
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ingest_jobs_status ON ingest_jobs (status, created_at);

-- SKIP LOCKED stops two workers claiming the same ROW; it does nothing about
-- two distinct active JOBS for the same document, which would both run the
-- replacement transaction. Enqueue must catch this violation and return 409.
CREATE UNIQUE INDEX ux_ingest_jobs_active_document
    ON ingest_jobs (document_id) WHERE status IN ('queued', 'running');

-- A chat session is bound to the tab it was opened in.
ALTER TABLE chat_sessions ADD COLUMN department_id INT
    REFERENCES departments(id) ON DELETE RESTRICT;   -- NULL = general chat, no RAG
```

### Why the composite FK is load-bearing

`(document_id, department_id) → documents (id, department_id)` makes a chunk's
`department_id` *provably* its document's. You get the cheap unjoined
`WHERE department_id = ?` filter, and it cannot drift or be forged by a buggy
insert. This is why `documents` carries the otherwise-redundant
`UNIQUE (id, department_id)`.

**Verified**, not assumed — inserting a chunk with `document_id='doc1'` (an HR
document) and `department_id=2` (Finance) fails:

```
ERROR:  insert or update on table "document_chunks" violates foreign key constraint
DETAIL:  Key (document_id, department_id)=(doc1, 2) is not present in table "documents".
```

### Two consequences worth stating out loud

- **`ON DELETE RESTRICT` on both `documents.department_id` and
  `chat_sessions.department_id` makes a department permanently undeletable** once
  anything references it. For a bank this is correct — deleting a department must
  not silently rewrite an old HR session into a general one. `is_active = false`
  is the only retirement path.
- **`embedding` is `NOT NULL`** so a chunk can never be unsearchable. That is a
  *different* guarantee from "the document is complete", which the atomic
  replacement transaction below provides.

## Embedding — `qwen3-embedding:4b-q8_0`

Native 2560 dimensions, MRL-truncated to **1536**.

**Truncation is forced, not stylistic.** pgvector's HNSW index is capped at
**2000 dimensions** for the `vector` type — `vector(2560)` would create fine and
then fail at `CREATE INDEX`. (`halfvec` allows 4000 and was considered:
`halfvec(2560)` is 5120 bytes/chunk vs 6144 for `vector(1536)`, keeping all
dimensions for less disk. Rejected for portability — 1536 is a standard dimension
across pgvector, Qdrant and Pinecone; halfvec is pgvector-specific.)

Three implementation requirements:

1. **Truncate and normalize in the gateway — as a portability contract, not
   because the server can't.** Measured against the live Ollama 0.32.5:

   ```
   no dimensions param   -> dims: 768
   dimensions: 256       -> dims: 256,  L2 norm: 1.000000
   ```

   Ollama honors `dimensions` *and* renormalizes. But whether a backend
   renormalizes after truncating is backend-specific, and a non-normalized MRL
   sub-vector silently breaks `<#>`/`<->`. So we request native dimensions and do
   the slice ourselves, giving byte-identical vectors across Ollama today and
   vLLM later — consistent with the rule that swapping `OLLAMA_BASE_URL` needs no
   edits outside `app/ollama/client.py`.
2. **Re-normalize to unit length after slicing.** An MRL sub-vector is not
   unit-norm. Strictly optional for `vector_cosine_ops` (cosine divides by the
   norms anyway) but mandatory the moment anyone uses `<#>` or `<->`. Normalizing
   once at write time removes the trap permanently. **Assert `len(vec) == 1536`
   before every insert** — `vector(1536)` is the real backstop and will reject a
   wrong-width vector, but the assertion fails the batch with a clear message
   instead of a constraint error mid-transaction.
3. **Queries and documents are embedded differently.** Qwen3-Embedding is
   asymmetric — queries take an instruction prefix, documents do not:

   ```
   Instruct: Given a search query, retrieve relevant passages that answer the query
   Query: {question}
   ```

   Embedding both sides identically is the most common way to lose accuracy with
   this model family, and it fails *silently* — you just get mediocre results.
   The embed helper therefore takes `mode: "query" | "document"` from day one, and
   the eval set locks it.

`documents.embed_model` / `embed_dim` exist for audit: when the model is
eventually swapped, they identify which documents still hold stale vectors.

## Ingestion

**Formats:** PDF, DOCX, XLSX, CSV, and typed-in text (`source='manual'`, with
`file_name`/`storage_key` NULL).

**Parser: Docling** for PDF and DOCX (PPTX/HTML come free). It is the right fit
because this schema already assumes a Docling-class parser — `page_number`,
`section`, and `element_type` are exactly its output, and its layout analysis and
table-structure recognition are the difference between a usable and a useless PDF
chunk. Its `HybridChunker` is tokenizer-aware, so chunk boundaries respect both
document structure and the embedding model's window.

**The dependency cost is large and was measured, not estimated:**

```
docling -> 90 packages, including torch, torchvision, transformers, accelerate,
           opencv-python, scipy, tokenizers, and the full NVIDIA CUDA stack
```

Several GB, into a gateway whose current dependencies are `openpyxl`, `fpdf2`,
`python-docx`. Three conditions follow:

1. **Lazy-import Docling inside the ingest path** so the API process never loads
   torch unless it actually ingests.
2. **XLSX/CSV keep using `app/files/readers.py`**, not Docling. One spreadsheet
   normalizer is shared with `read_excel`/`aggregate_excel`; a second would
   diverge from the tools that already read spreadsheets in this project, and
   Docling buys nothing on a plain grid.
3. **This is the forcing function for splitting the ingest worker out of the API
   process.** The `SKIP LOCKED` queue below already makes that a deployment
   change rather than a rewrite — do it as soon as Docling lands, so the
   request-serving process stays light.

**Spreadsheets are a known-weak fit and are handled explicitly.** Rows aren't
prose and chunk badly. Mitigation: every XLSX/CSV chunk repeats its header row so
each chunk is self-describing. **Limitation, stated plainly: corpus spreadsheets
are searchable as text but not aggregatable in v1.** `aggregate_excel` resolves
files through `resolve_file` → `generated_files`, scoped to the caller; corpus
documents live in a different table and directory, so the resolvers are disjoint
and `aggregate_excel` cannot reach a corpus file at all. Totals therefore work on
spreadsheets a user *attaches to the chat*, not on corpus documents. Adding
corpus aggregation later requires a `resolve_department_document(id)` that goes
through `rag_context` — until that exists, the cross-department risk cannot
materialize.

**Storage:** `RAG_DOCS_DIR`, separate from `FILES_DIR`. Corpus documents are not
per-user files and must not share a directory tree. `documents.storage_key` holds
a **relative** key under that directory, never an absolute path (unlike
`generated_files.path`) — rows stay portable across hosts, and the same value
becomes the bucket key if this moves to object storage.

### Atomic replacement

All parsing and embedding happens *outside* the transaction; the database work is
one short transaction:

```
parsed     = parse(file)
chunks     = chunk(parsed)
embeddings = embed(chunks, mode="document")   # slow, no lock held

BEGIN
  DELETE FROM document_chunks WHERE document_id = :id;
  INSERT INTO document_chunks (...)  -- batched ~500 rows/statement, same transaction
  UPDATE documents SET status='ready', chunk_count=:n,
         embed_model=:model, embed_dim=1536, updated_at=now()
   WHERE id = :id;
COMMIT
```

Batching inside the one transaction keeps per-statement memory bounded without
losing atomicity. On failure, `ROLLBACK`: a new document exposes zero chunks, and
a re-ingest keeps serving the previous complete version until the replacement
commits. All embeddings must be resident before `BEGIN`, which the existing 10 MB
upload cap makes safe.

**Archiving runs the same transaction with zero rows in.** `status='archived'`
lives on `documents` but chunks carry no status, and HNSW filters before a join
would be reachable — so an archived policy must have its chunks physically
removed or it keeps being cited. The `documents` row and `chunk_count` survive
for audit.

### The job queue, without Redis

`BackgroundTasks` runs in whichever uvicorn worker took the request. With
`--workers 2`, a naive startup sweep in worker B will reset a job worker A is
actively running. Postgres is the lock:

```sql
UPDATE ingest_jobs SET status='running', started_at=now(), attempts=attempts+1
WHERE id = (SELECT id FROM ingest_jobs WHERE status='queued'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

Stale sweep: `status='running' AND heartbeat_at < now() - interval '10 minutes'`
→ `failed`, retryable.

## Retrieval

### The security invariant

**`:dept` is derived from the authorized department context — not trusted
directly from the request body.** The tab code *does* originate in the request; it
becomes trusted only after `resolve_department` validates it.

`POST /v1/chat` gains a `department` field. Before the agent loop starts, the
router resolves it and installs it as a contextvar — the same pattern
`turn_files(user_id, session_id)` already uses for file ownership, and for the
same reason (it must be set *inside* the async generator Starlette iterates, or
it is invisible while the loop runs):

```python
# chat/router.py, inside the streaming generator
dept = await resolve_department(db, user, body.department, session)
with rag_context(dept):
    async for event in stream_turn(...):
```

`resolve_department` enforces four things: the caller owns the session
(re-checked, not assumed — 404), the department exists and is active (404),
the caller has a grant unless they are an admin (403), and the request agrees
with `chat_sessions.department_id` (409). Without that last check an HR session
could be continued with `department: "finance"` on turn five and the transcript
would read as one coherent HR conversation.

One case is easy to miss and is its own 409: **an existing session whose
`department_id` is NULL must not be given a department.** A brand-new session
(no row yet) and an existing general session both present as "no department", but
only the first may open in a department tab — relabelling an in-progress general
thread as HR would misrepresent every prior turn as departmentally grounded.

### Where the department comes from, and what it costs

**A chat session is bound to exactly one department, and retrieval always uses
the server-side `chat_sessions.department_id`** — never a value read back out of
the request body. The request's `department` exists to *open* a session in a tab
and to be cross-checked against the bound one; it is never the source of truth
for a turn that already has a session.

**The authorization check costs no additional round trip.** `open_turn` already
loads the `ChatSession`; slice 3 extends that one query to return the session,
its department, and the caller's grant together. Postgres stays the live source
of truth and revocation takes effect on the very next turn.

Measured, to size the decision honestly:

```
get_department_by_code                 0.250 ms
has_department_access                  0.268 ms
  -> resolve_department total          0.518 ms
get_current_user's user lookup         0.244 ms   (every authed request, unavoidable)
```

**Rejected: department claims in the JWT.** It would remove 0.518 ms from a turn
whose dominant cost is seconds of model inference, while leaving the request
DB-bound anyway (`get_current_user` selects the user row on every authenticated
request). More importantly it trades immediate revocation for a propagation
window — and this project has no refresh-token flow (`/auth/register` and
`/auth/login` are the only auth routes; tokens last 24h), so "short-lived tokens"
would mean building refresh, rotation, and logout revocation first. In a bank, a
revoked HR grant that keeps working for up to a day is a worse outcome than half
a millisecond. Also rejected for the same reason: in-process authorization
caching, which adds invalidation state and a multi-worker correctness problem to
buy the same half millisecond.

The tool therefore has **no department parameter** — the model has nowhere to put
one, so a prompt injection has no surface:

```python
SPEC = ToolSpec(
    name="search_department_docs",
    parameters={
        "query":  {"type": "string",  "maxLength": 1000},
        "top_k":  {"type": "integer", "minimum": 1, "maximum": 20, "default": 12},
    },
)
```

**JSON Schema bounds are advisory — clamp again in Python.** `top_k` is
model-controlled even though `department` is not; coerce to int, clamp to
`[1, 20]`, and truncate `query` to 1000 chars before embedding regardless of what
the model sends.

### The query

`SET LOCAL` only applies within its transaction, so it and the SELECT must run on
**the same pooled connection inside one explicit transaction** — in SQLAlchemy,
one `async with session.begin():` block.

```sql
BEGIN;
SET LOCAL hnsw.iterative_scan = relaxed_order;   -- fixed literal, safe as SET LOCAL
-- SET LOCAL cannot take a bind parameter (it wants a literal, and interpolating
-- would be an injection surface). set_config() is a function call, so it can.
-- Third argument = is_local, i.e. scoped to this transaction.
SELECT set_config('hnsw.ef_search', CAST(:ef_search AS text), true);

WITH dense_candidates AS MATERIALIZED (
    SELECT id, embedding <=> :qvec AS distance
    FROM document_chunks
    WHERE department_id = :dept
    ORDER BY embedding <=> :qvec
    LIMIT :pool          -- RAG_CANDIDATE_POOL, default 50
),
dense AS (
    SELECT id, distance, ROW_NUMBER() OVER (ORDER BY distance) AS rank
    FROM dense_candidates
),
lexical_candidates AS MATERIALIZED (
    SELECT c.id, ts_rank_cd(c.tsv, q.query) AS lexical_score
    FROM document_chunks c
    CROSS JOIN LATERAL (SELECT websearch_to_tsquery('english', :question) AS query) q
    WHERE c.department_id = :dept AND c.tsv @@ q.query
    ORDER BY lexical_score DESC
    LIMIT :pool
),
lexical AS (
    SELECT id, lexical_score, ROW_NUMBER() OVER (ORDER BY lexical_score DESC) AS rank
    FROM lexical_candidates
),
fused AS (
    SELECT COALESCE(d.id, l.id) AS id,
           d.distance      AS dense_distance,   -- raw signals, kept for eval only
           l.lexical_score AS lexical_score,    -- NEVER used as a threshold
           COALESCE(1.0 / (:rrf_k + d.rank), 0)
         + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS rrf_score
    FROM dense d FULL OUTER JOIN lexical l USING (id)
)
SELECT c.id AS chunk_id, c.document_id, doc.title,
       c.content, c.page_number, c.section,
       fused.rrf_score, fused.dense_distance, fused.lexical_score
FROM fused
JOIN document_chunks c   ON c.id = fused.id
JOIN documents       doc ON doc.id = c.document_id
ORDER BY fused.rrf_score DESC
LIMIT :rerank_pool;    -- RAG_RERANK_POOL, default 20 → handed to the reranker
COMMIT;
```

Three details, each verified against a live database (see below):

- **`websearch_to_tsquery`, never `to_tsquery`.** It cannot raise on arbitrary
  user text. `to_tsquery` throws on a stray `&`, turning a user's question into a
  500.
- **RRF `k` is configurable (`RAG_RRF_K=60`).** 60 is the value from Cormack et
  al. 2009, not a universal constant — the eval sweeps it. RRF is used precisely
  so no weight has to be tuned between a cosine distance and a `ts_rank_cd` score,
  which are not on the same scale.
- **Materialize candidates before ranking.** See below for the measured reason.

### Reranking and abstention

**RRF cannot carry an abstention threshold, and an empty result set is not the
negative signal.** RRF scores are computed from *ranks only* — bounded in
`[0, 2/(k+1)]` and identical whether the top hit is a perfect match or the least
irrelevant chunk in a department that contains nothing on the topic. Dense
retrieval returns candidates whenever the department has any chunks at all, so
`fused` is empty essentially only when the corpus is empty. A design that relies
on either signal will confidently answer every out-of-corpus question.

So the fused top-20 goes through **Qwen3-Reranker-4B**, a cross-encoder producing
a *calibrated* per-pair relevance score. Abstention thresholds that score, tuned
on the negative eval cases.

**Serving path — verified against the live Ollama 0.32.5:**

```
POST /api/rerank  -> 404
POST /v1/rerank   -> 404          # Ollama has no rerank endpoint
/v1/chat/completions logprobs -> YES, with top_logprobs
  {'token':'Yes','logprob':-0.00076,'top_logprobs':[Yes -0.0008, yes -7.20, No -11.26]}
```

Qwen3-Reranker is natively a yes/no logit read, so it runs as a 1-token
completion with `logprobs: true, top_logprobs: 5`, scoring
`P(relevant) = softmax(logprob_yes, logprob_no)`. This keeps the wire format
inside `app/ollama/client.py` (a new `rerank()` method) and swaps to vLLM's native
`/rerank` later without touching anything above it.

**Latency is the risk**: one forward pass per candidate over HTTP. Rerank the top
`RAG_RERANK_POOL` (default 20), not the full 50 candidates, with bounded
concurrency, and measure before raising it. `RAG_RERANK_ENABLED=false` falls back
to RRF order with abstention disabled — usable for development, **not** for a
department where a wrong confident answer matters.

`dense_distance` and `lexical_score` are carried through the query and logged to
`rag_queries` so the eval can compare channels and re-tune the threshold. They are
diagnostic only — neither is ever an absolute relevance threshold, for the same
reason RRF isn't: cosine distance has no corpus-independent meaning and
`ts_rank_cd` is unnormalized.

### Grounding

The tool returns `document_id`, `title`, `page_number`, `section` so the model can
cite. When every candidate falls below `RAG_RELEVANCE_THRESHOLD` — or nothing was
retrieved — the tool returns an explicit "no matching documents in this
department", not an empty list, which reads to the model as an unremarkable result
and invites an answer from parameters. The system prompt requires a citation for
every substantive claim and requires refusal without one.

This interacts with the organisation's regulated-advice rule: corpus answers stay
educational/general and cite the source document; they do not become personal
financial or tax advice.

Citations resolve through `GET /v1/documents/{id}`, which re-checks
`user_departments` — the same 404-unless-yours rule as `GET /v1/files/{id}`, since
a document id leaking into a transcript must not become a read primitive for
another department.

## Measured, not assumed

Built on Postgres 16.14 + pgvector 0.8.5, 20,000 chunks across two departments
(HR = 2,000, Finance = 18,000), HNSW `m=16, ef_construction=64`.

| Claim | Result |
|---|---|
| Composite FK rejects a forged `department_id` | **Confirmed** — FK violation |
| `websearch_to_tsquery` survives `'... & leave \|\| policy ???'` | **Confirmed** — → `'leav' & 'polici'` |
| Stopword-only query is safe | **Confirmed** — NOTICE, empty query, 0 lexical rows; dense carries it |
| `'english'` stems English *and* leaves Devanagari intact | **Confirmed** — `'कर्मचारी' 'नीति' 'बिदा' 'loan'`; `loans`→`loan` |
| Materialized form beats naive | **Confirmed** — 8.4 ms vs 14.7 ms |
| `ef_search=40` under-returns at `LIMIT 50` | **Refuted** — returned all 50; iterative scan compensates |
| `relaxed_order` scrambles ranks without materialization | **Not reproduced** — see open question |

**Why `'english'` over `'simple'`:** `to_tsvector('english', 'कर्मचारी बिदा नीति
loans')` → `'loan':4 'कर्मचारी':1 'नीति':3 'बिदा':2`. Devanagari passes through
untouched (it matches neither the English stemmer nor the stopword dictionary)
while English still stems. `'simple'` keeps `loans` and `loan` as distinct
lexemes, costing recall on exactly the English policy documents that dominate the
corpus, and gains nothing on Nepali.

**Why materialize** — the plans show a mechanism neither review predicted:

```
NAIVE:         Sort Method: quicksort        Memory: 127kB
MATERIALIZED:  Sort Method: top-N heapsort   Memory: 28kB
```

SQL evaluates window functions *before* `ORDER BY`/`LIMIT` at the same query
level, so a `ROW_NUMBER()` beside the `LIMIT` blocks the top-N heapsort and sorts
the whole department every query.

**The most important measurement: HNSW was never used.** Every plan chose
`Index Scan using ix_chunks_department` (and a Seq Scan when that index was
dropped) followed by an exact sort. At this scale Postgres correctly judges
brute-force exact KNN over 2,000 department rows cheaper than an approximate
index — which is *better*, since exact search is 100% recall. `ix_chunks_department`
therefore earns its place immediately; `ix_chunks_embedding` is insurance for
later. **Do not partition on a chunk-count rule.** Measure per-department latency,
recall@k and `EXPLAIN ANALYZE` on the real corpus, and revisit only when the
planner actually starts choosing HNSW.

## Configuration

Every retrieval knob is a setting, not a literal, because the eval sweeps them and
the review loop re-fits them.

```bash
RAG_DOCS_DIR=rag_documents            # separate from FILES_DIR
RAG_EMBED_MODEL=qwen3-embedding:4b-q8_0
RAG_EMBED_DIM=1536                    # MRL truncation target; must match vector(1536)
RAG_RERANK_ENABLED=true
RAG_RERANK_MODEL=qwen3-reranker:4b
RAG_CANDIDATE_POOL=50                 # per channel, before fusion
RAG_RERANK_POOL=20                    # fused candidates sent to the reranker
RAG_TOP_K=12                          # chunks handed to the model (clamp: 1..20)
RAG_RRF_K=60                          # Cormack et al. 2009; not a universal constant
RAG_HNSW_EF_SEARCH=40                 # recall knob; must be >= RAG_TOP_K
RAG_RELEVANCE_THRESHOLD=              # fitted to the negative eval cases, not guessed
RAG_MAX_QUERY_CHARS=1000
```

`RAG_EMBED_DIM` and the `vector(1536)` column must agree; a startup check should
fail loudly rather than let a mismatch surface as an insert error under load.

## Testing

- **Unit, pure:** MRL truncation + renormalization (norm == 1.0); query-vs-document
  prefixing; chunkers per format; the `top_k`/query-length clamps against hostile
  model output (`top_k: 100000`, `top_k: "5"`, a 1 MB query).
- **Integration, real Postgres:** the composite FK rejects a forged department;
  the atomic replacement leaves the prior version intact on failure; archiving
  removes chunks from retrieval **and the same file can then be re-uploaded**
  (the regression `ux_documents_active_content` exists to prevent); re-uploading
  a `failed` document returns 409 rather than a raw constraint error;
  `SKIP LOCKED` prevents double-claiming a job row, and
  `ux_ingest_jobs_active_document` prevents two active jobs for one document
  (enqueue returns 409 "ingest already in progress").
- **Retrieval, real Postgres:** `set_config('hnsw.ef_search', …, true)` actually
  applies within the transaction and is gone after `COMMIT`; a non-integer
  `ef_search` is rejected in Python before it reaches SQL.
- **Abstention:** with the reranker stubbed to a fixed low score, every query
  abstains; stubbed high, none do. This makes the threshold testable without the
  model, and proves abstention is driven by the reranker rather than by RRF or by
  an empty result set.
- **Security, non-negotiable:** a Finance-only user gets 403 on `department: "hr"`;
  a session opened in HR rejects a Finance `department` on a later turn;
  `GET /v1/documents/{id}` 404s across departments; the tool schema has no
  department field (a test asserting this, so it can't be added casually).

## Evaluation & Improvement

**1. Success metric.** Grounded-answer rate: the share of department questions
answered with a citation and no thumbs-down. Retrieval proxy, measured offline:
**recall@12** — is the gold chunk among what we hand the model.

**2. Eval.** 40 labelled cases, 8 per department, following the `aggregate_excel`
eval precedent (`d9d84f7`). Stored as JSON: `{question, department, expected_document_ids,
expected_answer_substring}`. Scored by a script reporting recall@12, MRR@12, and
refusal accuracy. Three kinds of case, and the last two matter most:

- **Positive** — answer is in the corpus; the gold document must be retrieved.
- **Negative** — answer is *not* in the corpus; correct output is "I couldn't find
  this in the HR documents." Catches confident hallucination.
- **Cross-department** — an HR-only user asks a Finance question; must be refused,
  never answered from Finance. This turns the security boundary into a test that
  runs on every change.

Baseline recorded at implementation; no pass rate yet.

**3. Feedback capture.** `rag_queries` logs every retrieval (user, department,
question, retrieved chunk ids + scores, `answered`, latency); `rag_feedback`
stores a per-answer thumbs up/down and optional comment. Thumbs-down rows with
their retrieved sets are the queue for new eval cases. The same tables are the
bank's audit trail for who asked what, of which department, and what was returned.

```sql
CREATE TABLE rag_queries (
    id            VARCHAR(32) PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES users(id)       ON DELETE RESTRICT,
    department_id INT NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
    session_id    VARCHAR(32) REFERENCES chat_sessions(id) ON DELETE SET NULL,
    question      TEXT NOT NULL,
    retrieved     JSONB NOT NULL,       -- [{chunk_id, document_id, score}]
    top_score     NUMERIC,
    answered      BOOLEAN NOT NULL,     -- answered, or refused for lack of grounding
    latency_ms    INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_rag_queries_dept_time ON rag_queries (department_id, created_at DESC);

CREATE TABLE rag_feedback (
    query_id   VARCHAR(32) PRIMARY KEY REFERENCES rag_queries(id) ON DELETE CASCADE,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    rating     SMALLINT NOT NULL,       -- +1 / -1
    comment    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**4. Review loop.** Monthly review of thumbs-down rows and refusal rate per
department. Plus a hard gate: any change to chunk size/overlap, embedding model,
reranker model, `top_k`, `RAG_RRF_K`, `RAG_HNSW_EF_SEARCH`, `RAG_CANDIDATE_POOL`,
`RAG_RERANK_POOL`, or `RAG_RELEVANCE_THRESHOLD` re-runs the eval, and a regression
in **either** recall@12 **or** refusal accuracy blocks the change. Both gates are
needed: a threshold tuned only for recall answers everything, and one tuned only
for refusals answers nothing.

`RAG_RELEVANCE_THRESHOLD` is not a constant to pick once. It is fitted to the
negative cases at implementation, recorded with its measured
recall/refusal trade-off, and re-fitted whenever the corpus or either model
changes — reranker calibration drifts with both.

## Open questions

1. **`rag_queries.question` retention.** It stores raw user text, which in a bank
   will eventually contain account numbers or customer names. This needs a stated
   retention window and a purge job. Not decided here — it is a compliance call,
   not an engineering one.
2. **`ON DELETE RESTRICT` on `rag_queries.user_id`** keeps the audit trail intact
   but means a user with query history cannot be deleted. `users.is_active` is the
   soft-delete path. Confirm this matches the bank's retention vs. erasure policy.
3. **The `relaxed_order` ordering claim is unverified.** The planner never chose
   HNSW at 20k chunks, so it could not be measured. The materialized form is
   adopted on measured performance grounds and pgvector's documented
   recommendation — but re-measure on the real corpus before treating the ordering
   argument as established.
4. **Model prerequisites are not yet met.** The live Ollama has only
   `nomic-embed-text`; neither `qwen3-embedding:4b-q8_0` nor a Qwen3-Reranker is
   pulled. Both must be available (locally or on the GPU host) before slice 3 can
   be evaluated. Reranker throughput on the target hardware is unmeasured and is
   the main latency risk in the design.
5. **Bulk backfill of an existing document library** may exceed what
   `BackgroundTasks` should carry. The `SKIP LOCKED` queue makes moving to a real
   worker process a deployment change, not a rewrite.
