# IsabelleGym MCP server

Exposes the running IsabelleGym HTTP server to LLM agents via the **Model Context Protocol**.
It is a thin layer over `client.async_client.IsabelleGymAsyncClient` (no core edits beyond the
client wrappers added in Phase 0); it does **not** modify or patch Isabelle.

## What it offers

- **Auto-managed, per-connection sessions** — the agent never handles `session_id`/leases;
  each MCP connection gets its own isolated Isabelle session.
- **Parallelism, both levels**:
  - intra-proof — `verify_chunk` checks a whole chunk with `parallel_proofs=2` and returns
    per-command status (`ok/failed/running/unprocessed`) under a **single** wall budget,
    naming the `stuck_line` on timeout (no opaque timeouts);
  - inter-session — `verify_batch` fans out many independent chunks **concurrently** across
    the server's session pool in one call (bounded by `max_parallel`).

### Tools
`enter_theory`, `verify_chunk` (the one execution tool — one command or a whole proof),
`proof_state`, `source`, `sledgehammer`, `checkpoint`, `restore`, `rollback`,
`close_theory`, `verify_batch`.
### Resources
`isabellegym://health`, `isabellegym://sessions`.
### Prompts
`prove_theorem`.

## Run

Prereq: a running IsabelleGym server (default `http://localhost:8000`).

```bash
pip install -r mcp_server/requirements.txt   # + the repo's client deps (httpx)
export PYTHONPATH=$PWD                        # so `client` imports

# local (stdio) — for Claude Desktop/Code, Cursor:
python -m mcp_server.app

# remote (Streamable HTTP):
ISABELLE_MCP_TRANSPORT=streamable-http ISABELLE_MCP_PORT=8848 python -m mcp_server.app
```

### Register (stdio, e.g. Claude Code / Desktop `mcp` config)
```json
{
  "mcpServers": {
    "isabellegym": {
      "command": "python",
      "args": ["-m", "mcp_server.app"],
      "env": { "PYTHONPATH": "/path/to/IsabelleGym", "ISABELLE_MCP_GYM_URL": "http://localhost:8000" }
    }
  }
}
```

## Config (env)
`ISABELLE_MCP_GYM_URL` (default `http://localhost:8000`), `ISABELLE_MCP_FIELD` (`HOL`),
`ISABELLE_MCP_MAX_PARALLEL` (`4`), `ISABELLE_MCP_CHUNK_TIMEOUT` (`180`),
`ISABELLE_MCP_HTTP_TIMEOUT` (`600`), `ISABELLE_MCP_TRANSPORT` (`stdio`|`streamable-http`),
`ISABELLE_MCP_HOST`, `ISABELLE_MCP_PORT` (`8848`).
