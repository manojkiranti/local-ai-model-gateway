"""Per-user tool access: which tools each user's model may see and call.

Local tools (create_pptx, fetch_url, ...): `policy` + `user_tool_access`.
MCP tools (HRMS/iZone/EMS): `mcp_policy` + `user_mcp_levels` (a none/read/full
level per system) + `user_mcp_tool_access` (per-tool overrides). Both are
VISIBILITY decided by the gateway; the MCP grants (`app/mcp/grants.py`) are a
separate, server-enforced check.
"""
