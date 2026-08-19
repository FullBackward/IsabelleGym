"""LSP-like MCP server package: file-synced read-only tools + scratch execution.

Strictly additive over IsabelleGymAsyncClient — no server-core edits. State is
keyed by CANONICAL FILE PATH (not MCP connection): each open file binds to a
leased session, re-synced from disk before every query. See README.md.
"""
