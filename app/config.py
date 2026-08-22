"""Application configuration (pydantic-settings, all via .env).

Security note: `database_url` and `jwt_secret` are REQUIRED and have no
in-code defaults on purpose — credentials/secrets must come from the
environment, never be baked into source.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (app/config.py -> app -> repo). Used to anchor relative paths so they
# do not depend on a process's working directory — the API, the ingest worker and
# the citation download route are separate readers that must resolve the corpus
# to the SAME place. `app/nrb/filestore.base_dir()` already does this for the NRB
# blob tree, for exactly the same reason.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database (SQLAlchemy async URL, e.g. postgresql+asyncpg://...) ---
    database_url: str  # required; supplied via .env

    # --- Auth / JWT ---
    jwt_secret: str  # required; supplied via .env
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24h

    # Admin bootstrap: emails in this comma-separated allowlist register as
    # admin. Additionally, if the users table is empty, the first registrant
    # becomes admin so there's always a way in.
    admin_emails: str = ""

    # --- Active Directory authentication -------------------------------------
    # An internal HTTP shim in front of AD. It answers ONLY "Success" or
    # "Failed": no email, no display name, no group membership. So it decides
    # authentication and nothing else — `role` and department grants stay in
    # Postgres, granted by an admin. See app/auth/directory.py.
    #
    # Off by default: with `ad_auth_enabled=false` login behaves exactly as it
    # did before AD existed.
    ad_auth_enabled: bool = False
    # Base URL of the shim's .asmx service. The /AD_Authentication method name
    # is HARDCODED in app/auth/directory.py — like NRB's /rates, the host is
    # config and the path is not, so nothing user-supplied reaches the URL.
    ad_auth_base_url: str = ""
    ad_auth_connect_timeout: float = 5.0
    ad_auth_read_timeout: float = 10.0

    # --- Login rate limiting -------------------------------------------------
    # Applies to BOTH providers. It exists mainly because of AD: an unthrottled
    # /auth/login is a remote way to trip the DOMAIN lockout counter on every
    # account in the company. Keep `login_max_attempts` below the domain's own
    # lockout threshold. Counters are per PROCESS — see app/auth/throttle.py.
    login_max_attempts: int = 5
    login_attempt_window_seconds: int = 300
    login_lockout_seconds: int = 900

    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: float = 120.0
    default_chat_model: str = "qwen2.5:latest"
    default_embed_model: str = "nomic-embed-text:latest"

    # --- Assistant identity (deployment branding) ---
    # What the assistant calls itself. Defaults stay generic so the same build
    # serves any deployment; a bank sets ASSISTANT_NAME/ASSISTANT_ORG in .env.
    # This is product branding, NOT a security boundary — the real model id is
    # still in the /v1/chat response body, and a determined user can elicit the
    # base model by prompting.
    assistant_name: str = "AI Assistant"
    assistant_org: str = ""  # blank = no organisation clause

    # --- Response transparency ("how it worked" in the UI) ---
    # The per-iteration execution trace is ALWAYS persisted to
    # chat_messages.trace (audit); this only controls whether it is sent to the
    # client. false = /v1/chat (both modes) and /v1/sessions/{id} omit it, so a
    # production UI has nothing to render a "how it worked" panel from. Live
    # tool_call/tool_result stream events are NOT affected — those are the
    # in-flight "using tool X" indicator, not the post-hoc trace.
    expose_trace: bool = True

    # --- Agent (hand-rolled tool-calling loop) ---
    agent_model: str = "qwen2.5:latest"
    agent_temperature: float = 0.1
    agent_max_iterations: int = 8

    # --- MCP (remote tools; the gateway is the MCP client) ---
    # Token read from env only, never hardcoded, never logged. Blank = no auth.
    mcp_server_url: str = ""
    mcp_auth_token: str = ""
    # Tool exposure policy applied BEFORE any tool reaches the model.
    mcp_tool_mode: Literal["read_only", "allowlist", "all"] = "read_only"
    mcp_tool_allowlist: str = ""  # comma-separated exact names, for allowlist mode
    mcp_read_prefixes: str = "get,list,search,read,fetch,find,view"
    mcp_write_keywords: str = (
        "create,update,delete,send,remove,archive,"
        "associate,advance,propose,apply,register"
    )

    # --- Files (create_excel output; served only via GET /v1/files/{id}) ---
    files_dir: str = "generated_files"
    # Spreadsheet upload (POST /v1/files): hard byte cap on the upload, and the
    # zip-bomb guard's ceiling on total UNCOMPRESSED xlsx content.
    upload_max_bytes: int = 10 * 1024 * 1024        # 10 MB on the wire
    upload_xlsx_max_uncompressed: int = 200 * 1024 * 1024  # 200 MB expanded

    # --- fetch_url tool (SSRF-guarded outbound HTTP GET) ---
    # Enabled by default; internal/private IPs are ALWAYS blocked regardless.
    fetch_url_enabled: bool = True
    # Optional host allowlist: if non-empty, ONLY these domains (and subdomains)
    # may be fetched. Blank = any public host (still IP-filtered).
    fetch_url_allowlist: str = ""

    # --- Nepal Rastra Bank (get_nrb_forex tool) ---
    # The official Forex API base. Config, not a tool argument: the model can
    # never point this tool at a host of its choosing (that is fetch_url's job,
    # with fetch_url's SSRF guards). The `/rates` path is hardcoded in the client.
    nrb_api_base_url: str = "https://www.nrb.org.np/api/forex/v1"
    # The public website, for sitemap discovery (Phase 2 inventory — not a tool,
    # not model-reachable). Separate from the Forex API base because that one is
    # a versioned API path; this is the site root. Its HOST is also the trust
    # boundary for discovery: every sitemap and every URL, including ones NRB's
    # own sitemap points at, must be on this host or it is reported and skipped.
    nrb_site_base_url: str = "https://www.nrb.org.np"
    # Pause between requests when enumerating NRB's REST API or probing pages.
    # The ONE tunable in the discovery layer: byte caps, timeouts and page bounds
    # are module constants (app/nrb/sitemap.py, wp_api.py, page.py) because they
    # follow from the site's measured shape, whereas how hard we may lean on a
    # central bank's website is an operational judgement that varies by
    # environment and permission. 0 disables pacing.
    nrb_crawl_delay_seconds: float = 0.25
    # Where downloaded NRB attachments live (Phase 5). A SEPARATE tree from
    # rag_docs_dir: these are raw upstream artefacts, not department corpus
    # documents — only Phase 7 decides which of them becomes a `documents` row.
    # Relative values are anchored to the REPO ROOT, not the process CWD, by
    # `app/nrb/filestore.base_dir` (the fetch command is run from wherever a
    # developer is standing, and two CWDs must not mean two corpora). The layout
    # is content-addressed, so this directory holds one blob per distinct sha256.
    # Byte caps and timeouts stay module constants in `app/nrb/fetch.py` because
    # they follow from the corpus's measured shape (largest file observed: 46 MB).
    nrb_files_dir: str = "nrb_files"

    # --- RAG: department corpus ingestion ---
    # Corpus documents are org knowledge, NOT per-user files — separate tree.
    rag_docs_dir: str = "rag_documents"
    # Native output is 2560; we MRL-truncate to 1536 because pgvector's HNSW
    # index caps at 2000 dimensions. Must match vector(1536) in the schema.
    rag_embed_model: str = "qwen3-embedding:4b-q8_0"
    rag_embed_dim: int = 1536
    rag_embed_batch: int = 32  # texts per /v1/embeddings request
    rag_chunk_max_chars: int = 2000
    rag_chunk_overlap_chars: int = 200
    # Applied AFTER merging, so it only ever sees orphaned fragments. 40 is an
    # empirically chosen default for the current corpus, NOT a universally safe
    # threshold — the smallest real content observed was a 45-char glossary
    # definition, and post-merge bodies average ~1481 chars. Re-check the
    # body-length distribution before changing it for a different corpus.
    rag_chunk_min_body_chars: int = 40
    # Front matter, matched against the FIRST segment of a chunk's heading path.
    rag_skip_sections: str = "table of contents,contents,index"
    # Worker loop timing.
    rag_ingest_poll_seconds: float = 2.0
    rag_ingest_stale_minutes: int = 10  # running + stale heartbeat -> failed
    # Must be comfortably below stale_minutes*60 — a big PDF spends far longer
    # than the stale window in parse+embed, and without beats the sweep would
    # fail a job that is working fine.
    rag_ingest_heartbeat_seconds: float = 30.0

    # --- RAG: retrieval (slice 3) ---
    rag_top_k: int = 12  # chunks handed to the model; also the tool's clamp ceiling
    rag_candidate_pool: int = 50  # per channel, before fusion
    # Fused candidates the reranker scores. This is ALSO what retrieval fetches
    # (`search_chunks(limit=...)`) -- handed only `rag_top_k`, a reranker could do
    # nothing but reorder what fusion already liked.
    #
    # MUST be >= rag_top_k. The pool is the candidate set top_k is selected FROM,
    # so a smaller pool makes top_k unreachable: at pool 10 and top_k 12 the tool
    # can never present 12 passages, reranking enabled or not. Keeping it >= top_k
    # also means a RERANK-DISABLED deployment is byte-identical to the pre-ranking
    # behaviour -- fusion orders by rrf_score DESC, so taking the first top_k of a
    # larger pool returns the same rows in the same order as fetching top_k did.
    # The eval sweep measures 10 vs 20; until it has run this stays at 20.
    rag_rerank_pool: int = 20
    # Cormack et al. 2009's constant, not a universal law — kept configurable so
    # an eval can sweep it. RRF is used precisely so no weight has to be tuned
    # between a cosine distance and a ts_rank_cd score, which share no scale.
    rag_rrf_k: int = 60
    # Recall knob for the ANN index. Must be >= the per-channel pool or the dense
    # side can return fewer candidates than asked for.
    rag_hnsw_ef_search: int = 100
    rag_max_query_chars: int = 1000
    # Budget for the serialized tool result. Deliberately under the agent loop's
    # MAX_TOOL_RESULT_CHARS (8000): a bare cut there would sever citation headers
    # mid-line, so the tool trims its own bodies instead and says that it did.
    rag_tool_result_max_chars: int = 7000
    # Reranking supplies the calibrated per-pair score that RRF cannot, and is
    # therefore what makes abstention possible. Keep it FALSE until the eval
    # sweep has fitted a threshold: enabling it on the placeholder below would
    # ship refusals nobody measured. False is a supported configuration and
    # behaves exactly as the system did before ranking existed.
    rag_rerank_enabled: bool = False
    rag_rerank_model: str = "qwen3-reranker:4b"
    # PLACEHOLDER until `scripts/rag_eval_sweep.py` fits it. 0.5 is also
    # disqualified on principle: it is exactly what `rerank.score_from_logprobs`
    # returns for "no signal", so at 0.5 the least informative case sits on the
    # boundary.
    rag_relevance_threshold: float = 0.5

    # --- CORS (frontend talks only to this gateway) ---
    cors_origins: str = "*"  # comma-separated, or "*" for all (dev)

    # --- validation ---
    @model_validator(mode="after")
    def _check_ad_auth(self) -> "Settings":
        """Fail at import, not at the first login attempt.

        A deployment that switches AD on but forgets the URL would otherwise
        look configured and then answer 503 to every directory user.
        """
        if self.ad_auth_enabled and not self.ad_auth_base_url.strip():
            raise ValueError(
                "AD_AUTH_ENABLED is true but AD_AUTH_BASE_URL is empty; "
                "set the .asmx service base URL or disable AD auth"
            )
        if self.login_max_attempts < 1:
            raise ValueError("LOGIN_MAX_ATTEMPTS must be at least 1")
        return self

    # --- parsed helpers ---
    @staticmethod
    def _csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return self._csv(self.cors_origins)

    @property
    def tool_allowlist(self) -> list[str]:
        return self._csv(self.mcp_tool_allowlist)

    @property
    def read_prefixes(self) -> list[str]:
        return self._csv(self.mcp_read_prefixes)

    @property
    def write_keywords(self) -> list[str]:
        return self._csv(self.mcp_write_keywords)

    @property
    def fetch_url_allowed_hosts(self) -> list[str]:
        return [h.lower() for h in self._csv(self.fetch_url_allowlist)]

    @property
    def rag_docs_base(self) -> str:
        """Absolute corpus directory. A relative RAG_DOCS_DIR is anchored to the
        PROJECT ROOT, never the process CWD, so the API (which writes uploads),
        the worker (which reads them) and the download route (which serves them)
        resolve the same tree no matter how each was launched. An absolute value
        is used verbatim.

        A CWD-relative read is the §18 failure class: the call "succeeds", the
        row says `ready`, and nothing is ever served.
        """
        path = Path(self.rag_docs_dir)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    @property
    def rag_skipped_sections(self) -> set[str]:
        # Normalization (case, whitespace, punctuation) happens at the point of
        # comparison in parsing._is_skipped_section, so both the configured
        # entries and the heading are normalized by the same function.
        return set(self._csv(self.rag_skip_sections))


@lru_cache
def get_settings() -> Settings:
    return Settings()
