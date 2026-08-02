"""Local LLM Gateway — the single front door between the frontend and the
inference (Ollama), data (Postgres), and tools (MCP + local) tiers.

This slice implements auth + users + db + health. The ollama/, mcp/, tools/,
agent/, chat/, and files/ packages are seams for later slices.
"""

__version__ = "0.1.0"
