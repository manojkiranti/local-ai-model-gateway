"""Schemas for the exposed-after-filtering tool list (ported)."""

from pydantic import BaseModel


class ExposedToolInfo(BaseModel):
    name: str
    description: str
    backend: str  # "mcp" | "local"


class FilteredToolInfo(BaseModel):
    name: str
    reason: str


class ToolsResponse(BaseModel):
    mode: str
    server_url: str
    exposed: list[ExposedToolInfo]
    filtered_out: list[FilteredToolInfo]
