"""MCP client bridge (ported from local-ai-model).

The gateway is the MCP client: it connects to a remote MCP server over the
streamable HTTP transport, lists its tools, applies a safety filter, converts
the survivors into Ollama's tool format, and executes tool calls for the agent
loop. Ollama itself is NOT an MCP client — it only picks which tool to call.

The session is opened per logical operation (a /v1/tools describe, or one full
agent run) via ``session()``, keeping each session inside a single task to avoid
anyio cancel-scope issues. The auth token builds the Authorization header and is
never logged.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from . import grants
from .grants import McpIdentity

logger = logging.getLogger("app.mcp")

# Cap how much of a tool result we ever hand back to the model.
MAX_TOOL_RESULT_CHARS = 8000

# Pre-flight reachability probe timeout (seconds). Kept short: it only needs to
# learn "is anything listening?", not to complete the MCP handshake.
PROBE_TIMEOUT = 5.0


class MCPUnavailableError(Exception):
    """Raised when the MCP server can't be reached or isn't configured."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def tokenize(name: str) -> list[str]:
    """Split a tool name into lowercase tokens across camelCase and separators.

    ``getUserById`` -> ['get', 'user', 'by', 'id']
    ``hubspot-create-deal`` -> ['hubspot', 'create', 'deal']
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]


@dataclass
class ExposedTool:
    name: str
    description: str
    ollama_schema: dict[str, Any]


@dataclass
class ExcludedTool:
    name: str
    reason: str


@dataclass
class ToolSet:
    exposed: list[ExposedTool]
    excluded: list[ExcludedTool]

    @property
    def exposed_names(self) -> set[str]:
        return {t.name for t in self.exposed}

    @property
    def ollama_tools(self) -> list[dict[str, Any]]:
        return [t.ollama_schema for t in self.exposed]


class MCPClient:
    def __init__(
        self,
        *,
        server_url: str,
        auth_token: str,
        tool_mode: str,
        allowlist: list[str],
        read_prefixes: list[str],
        write_keywords: list[str],
    ) -> None:
        self.server_url = server_url
        self.tool_mode = tool_mode
        self.allowlist = set(allowlist)
        self.read_prefixes = set(read_prefixes)
        self.write_keywords = set(write_keywords)
        # Base auth header built once; the raw token is not stored beyond this.
        self._auth_headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._has_auth = bool(auth_token)

    @property
    def configured(self) -> bool:
        return bool(self.server_url)

    def _session_headers(self, identity: McpIdentity | None) -> dict[str, str]:
        """Auth header plus the caller's identity and grants.

        The gateway is the front door, so it — not the model — asserts who is
        asking and what they hold. The identity is a per-call ARGUMENT and never
        client state: this object is a process-wide singleton, so storing it
        would let two concurrent turns race each other's grants.

        A fresh dict every call, for the same reason.
        """
        headers = dict(self._auth_headers)
        headers.update(grants.header_values(identity))
        return headers

    # ---- filtering (applied before any tool is shown to the model) ----
    def _classify(self, name: str) -> tuple[bool, str]:
        if self.tool_mode == "all":
            return True, "all mode (writes enabled)"
        if self.tool_mode == "allowlist":
            if name in self.allowlist:
                return True, "in allowlist"
            return False, "not in allowlist"

        # read_only (default): must look read-y and must not look write-y.
        tokens = tokenize(name)
        write_hit = next((t for t in tokens if t in self.write_keywords), None)
        if write_hit:
            return False, f"write-like token '{write_hit}'"
        read_hit = next((t for t in tokens if t in self.read_prefixes), None)
        if read_hit:
            return True, f"read-like token '{read_hit}'"
        return False, "no read-like token"

    def _to_ollama(self, tool: Any) -> dict[str, Any]:
        schema = tool.input_schema or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "parameters": schema,
            },
        }

    def filter_tools(self, tools: list[Any]) -> ToolSet:
        exposed: list[ExposedTool] = []
        excluded: list[ExcludedTool] = []
        for tool in tools:
            ok, reason = self._classify(tool.name)
            if ok:
                exposed.append(
                    ExposedTool(
                        name=tool.name,
                        description=(tool.description or "").strip(),
                        ollama_schema=self._to_ollama(tool),
                    )
                )
            else:
                excluded.append(ExcludedTool(name=tool.name, reason=reason))
        return ToolSet(exposed=exposed, excluded=excluded)

    # ---- connection ----
    async def _probe_reachable(self) -> None:
        """Fail fast (as MCPUnavailableError) when nothing is listening.

        Why this exists: if the server is down, the streamable-HTTP transport
        reports the failed connect by cancelling its internal task group, which
        reaches us as an ``asyncio.CancelledError``. That's a BaseException, so
        the ``except Exception`` around the handshake below does NOT catch it —
        it escapes as a raw CancelledError and corrupts the anyio cancel-scope
        teardown. A plain GET surfaces "connection refused" as an ordinary
        httpx error we can convert cleanly, before we touch that machinery.
        Any HTTP response at all (even 401/405) counts as reachable.

        Uses a plain httpx client on purpose: the MCP SDK's transport ships a
        vendored httpx (httpx2), so its ConnectError is a *different* class than
        the one we could catch here. This probe only needs raw TCP/HTTP reach.
        """
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                await client.get(self.server_url)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise MCPUnavailableError(
                f"Cannot reach MCP server at {self.server_url}: {exc}"
            ) from exc
        except httpx.HTTPError:
            # Reached the host but the probe GET itself was unhappy (e.g. read
            # timeout on an SSE endpoint). Reachable enough — let the real
            # handshake below decide.
            return

    async def ensure_reachable(self) -> None:
        """Pre-flight for streaming callers: raise MCPUnavailableError now (so it
        can become a clean 502) rather than mid-stream. No-op if not configured."""
        if self.configured:
            await self._probe_reachable()

    @asynccontextmanager
    async def session(self, *, identity: McpIdentity | None = None) -> AsyncIterator[ClientSession]:
        """Open an initialized MCP session for the duration of the block.

        `identity` is forwarded to the server as `x-user-email`/`x-user-roles`/
        `x-user-permissions` so it can scope per-user business tools AND gate
        which tools this caller may even see. Connection/initialize failures
        are raised as MCPUnavailableError; errors raised by the caller inside
        the block propagate unchanged.
        """
        if not self.configured:
            raise MCPUnavailableError("MCP_SERVER_URL is not configured.")

        await self._probe_reachable()

        headers = self._session_headers(identity)
        stack = AsyncExitStack()
        try:
            http_client = create_mcp_http_client(headers=headers or None)
            await stack.enter_async_context(http_client)
            read, write = await stack.enter_async_context(
                streamable_http_client(self.server_url, http_client=http_client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:  # connection/handshake failure
            await stack.aclose()
            raise MCPUnavailableError(
                f"Cannot reach MCP server at {self.server_url}: {exc}"
            ) from exc

        try:
            yield session
        finally:
            await stack.aclose()

    # ---- operations ----
    async def load_toolset(self, session: ClientSession) -> ToolSet:
        result = await session.list_tools()
        toolset = self.filter_tools(list(result.tools))
        logger.info(
            "MCP tools (mode=%s): exposed=%s | filtered_out=%s",
            self.tool_mode,
            [t.name for t in toolset.exposed],
            [f"{t.name} ({t.reason})" for t in toolset.excluded],
        )
        return toolset

    async def describe(self, *, identity: McpIdentity | None = None) -> ToolSet:
        """Connect, list, filter — for the GET /v1/tools endpoint."""
        async with self.session(identity=identity) as session:
            return await self.load_toolset(session)

    async def call_tool(
        self, session: ClientSession, name: str, arguments: dict[str, Any]
    ) -> tuple[str, bool]:
        """Execute a tool. Returns (text_result, is_error)."""
        result = await session.call_tool(name, arguments)
        return _summarize_result(result), bool(getattr(result, "is_error", False))


def _summarize_result(result: Any) -> str:
    """Flatten an MCP CallToolResult into plain text for the model/trace."""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(getattr(item, "type", item)))
    if not parts:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                parts.append(json.dumps(structured))
            except (TypeError, ValueError):
                parts.append(str(structured))
    return "\n".join(parts).strip()
