"""Configuration for the IsabelleGym MCP server (all via env, with sane defaults)."""
from __future__ import annotations

import os


class Config:
    # Where the running IsabelleGym HTTP server lives.
    GYM_URL: str = os.environ.get("ISABELLE_MCP_GYM_URL", "http://localhost:8000")
    DEFAULT_FIELD: str = os.environ.get("ISABELLE_MCP_FIELD", "HOL")

    # Concurrency cap for verify_batch fan-out. Kept conservative to respect the server's
    # memory admission gate (the per-session `threads` cap is currently inert — see
    # claude-work/impl-parallel-sessions/). Never exceeds the server pool size in practice.
    MAX_PARALLEL: int = int(os.environ.get("ISABELLE_MCP_MAX_PARALLEL", "4"))

    # Default single wall budget (s) for verify_chunk / each verify_batch item.
    CHUNK_TIMEOUT: float = float(os.environ.get("ISABELLE_MCP_CHUNK_TIMEOUT", "180"))
    # httpx timeout for the underlying client (must exceed CHUNK_TIMEOUT + server grace).
    HTTP_TIMEOUT: float = float(os.environ.get("ISABELLE_MCP_HTTP_TIMEOUT", "600"))

    # Transport: "stdio" (local) or "streamable-http" (remote).
    TRANSPORT: str = os.environ.get("ISABELLE_MCP_TRANSPORT", "stdio")
    HOST: str = os.environ.get("ISABELLE_MCP_HOST", "127.0.0.1")
    PORT: int = int(os.environ.get("ISABELLE_MCP_PORT", "8848"))
