"""Shared types for local (in-process) tools.

Kept in its own module so both the engine (`registry.py`) and each tool module
can import `LocalToolSpec`/`LocalFn` without a circular import: the engine imports
the aggregated `LOCAL_TOOLS` from this package, while the tool modules import only
these types from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# A local tool: async fn taking the model-supplied args dict, returning a string
# that goes back to the model as the tool result.
LocalFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class LocalToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the function's arguments
    func: LocalFn
