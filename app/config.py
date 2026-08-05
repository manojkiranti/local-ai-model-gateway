"""Application configuration (pydantic-settings, all via .env).

Security note: `database_url` and `jwt_secret` are REQUIRED and have no
in-code defaults on purpose — credentials/secrets must come from the
environment, never be baked into source.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: float = 120.0
    default_chat_model: str = "qwen2.5:latest"
    default_embed_model: str = "nomic-embed-text:latest"

    # --- Agent (hand-rolled tool-calling loop) ---
    agent_model: str = "qwen2.5:latest"
    agent_temperature: float = 0.1
    agent_num_ctx: int = 16384
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

    # --- CORS (frontend talks only to this gateway) ---
    cors_origins: str = "*"  # comma-separated, or "*" for all (dev)

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
