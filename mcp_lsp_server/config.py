"""Configuration for the IsabelleGym LSP-like MCP server (all via env)."""
from __future__ import annotations

import os


class Config:
    # Where the running IsabelleGym HTTP server lives.
    GYM_URL: str = os.environ.get("ISABELLE_MCP_LSP_GYM_URL", "http://localhost:8000")
    DEFAULT_FIELD: str = os.environ.get("ISABELLE_MCP_LSP_FIELD", "HOL")
    DEFAULT_TASK_GROUP: str = os.environ.get("ISABELLE_MCP_LSP_TASK_GROUP", "default")

    # httpx timeout for the underlying client (must exceed load/build budgets).
    HTTP_TIMEOUT: float = float(os.environ.get("ISABELLE_MCP_LSP_HTTP_TIMEOUT", "600"))
    # Wall budget (s) for load_document syncs and per-candidate attempt verification.
    LOAD_TIMEOUT: float = float(os.environ.get("ISABELLE_MCP_LSP_LOAD_TIMEOUT", "120"))
    ATTEMPT_TIMEOUT: float = float(os.environ.get("ISABELLE_MCP_LSP_ATTEMPT_TIMEOUT", "180"))

    # Concurrency cap for multi_attempt fan-out (bounded by the server pool).
    MAX_PARALLEL: int = int(os.environ.get("ISABELLE_MCP_LSP_MAX_PARALLEL", "4"))
    # Warm scratch sessions kept per context (task_group, heap, imports, field);
    # reused across calls — each use is a load_document reset.
    SCRATCH_POOL_SIZE: int = int(os.environ.get("ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE", "4"))

    # When true, isabelle_close destroys the session (immediate teardown,
    # freeing memory) instead of the default warm release back to the pool.
    # The per-call `destroy` argument on isabelle_close overrides this.
    CLOSE_DESTROYS: bool = os.environ.get(
        "ISABELLE_MCP_LSP_CLOSE_DESTROYS", "false"
    ).lower() in {"1", "true", "yes", "on"}

    # Transport: "stdio" (local) or "streamable-http" (remote).
    TRANSPORT: str = os.environ.get("ISABELLE_MCP_LSP_TRANSPORT", "stdio")
    HOST: str = os.environ.get("ISABELLE_MCP_LSP_HOST", "127.0.0.1")
    PORT: int = int(os.environ.get("ISABELLE_MCP_LSP_PORT", "8849"))
