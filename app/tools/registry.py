"""Unified tool registry (ported).

Holds tools from two backends behind one interface so the agent loop doesn't
care where a tool comes from:
  - "mcp"   discovered from the remote MCP server (already filtered by
            MCP_TOOL_MODE inside the MCP client).
  - "local" plain in-process async Python functions.

The agent asks the registry for two things only:
  - list_ollama_tools()  -> the merged array handed to the model
  - dispatch(name, args) -> run a tool (routed by backend), return a string
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..mcp.client import MCPClient
# Local tools live in the `local/` package; the engine only aggregates them.
# Re-exported below so `from .registry import LOCAL_TOOLS / LocalToolSpec` keeps working.
from .local import LOCAL_TOOLS, LocalFn, LocalToolSpec

logger = logging.getLogger("app.tools")

__all__ = [
    "ToolRegistry",
    "RegisteredTool",
    "UnknownToolError",
    "LOCAL_TOOLS",
    "LocalToolSpec",
    "LocalFn",
]


class UnknownToolError(Exception):
    """Raised by dispatch() when a name isn't registered."""

    def __init__(self, name: str, valid: list[str]) -> None:
        self.name = name
        self.valid = valid
        super().__init__(f"unknown tool: {name}")


def _ollama_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@dataclass
class RegisteredTool:
    name: str
    ollama_schema: dict[str, Any]
    backend: str  # "mcp" | "local"
    func: Optional[LocalFn] = None  # set for local tools only


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._mcp: Optional[MCPClient] = None
        self._session: Any = None

    def register_local_tools(self) -> None:
        for spec in LOCAL_TOOLS:
            self._tools[spec.name] = RegisteredTool(
                name=spec.name,
                ollama_schema=_ollama_schema(spec.name, spec.description, spec.parameters),
                backend="local",
                func=spec.func,
            )

    async def load_mcp_tools(self, mcp: MCPClient, session: Any) -> None:
        self._mcp = mcp
        self._session = session
        toolset = await mcp.load_toolset(session)
        for tool in toolset.exposed:
            self._tools[tool.name] = RegisteredTool(
                name=tool.name, ollama_schema=tool.ollama_schema, backend="mcp"
            )

    def list_ollama_tools(self) -> list[dict[str, Any]]:
        return [t.ollama_schema for t in self._tools.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def backend_of(self, name: str) -> Optional[str]:
        tool = self._tools.get(name)
        return tool.backend if tool else None

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(name, self.tool_names())
        if tool.backend == "local":
            assert tool.func is not None
            return await tool.func(args)
        assert self._mcp is not None and self._session is not None
        text, _is_error = await self._mcp.call_tool(self._session, name, args)
        return text
