# MCP comparison harness

This directory compares three Isabelle MCP servers on the same set of `.thy` problems:

1. **IsabelleGym MCP** (`run_isabellegym.py`) — this repo
2. **Isabelle-MCP** (`run_isabelle_mcp.py`) — `~/GitHub/Isabelle-MCP`
3. **AutoCorrode I/Q** (`run_autocorrode_iq.py`) — `~/GitHub/AutoCorrode`

The harness uses a **shared Kimi API client** (OpenAI-compatible endpoint) and the same agent-loop
structure for all three. Only the MCP-specific tool dispatch differs.

---

## Directory layout

```
MCP-comparison/
├── README.md                    # this file
├── config.yaml                  # default configuration
├── config.local.yaml            # (optional) your local overrides
├── common/                      # shared utilities
│   ├── config.py                # load config + env vars
│   ├── model.py                 # Kimi/OpenAI-compatible chat client
│   ├── mcp_client.py            # MCP stdio client helpers
│   ├── problems.py              # parse .thy files
│   ├── metrics.py               # result schema, JSONL, timing
│   └── arbiter.py               # neutral isabelle build checker
├── run_isabellegym.py           # IsabelleGym runner
├── run_isabelle_mcp.py          # Isabelle-MCP runner
├── run_autocorrode_iq.py        # AutoCorrode I/Q runner
├── analyze.py                   # print summary tables
└── runs/                        # results + final .thy artifacts
```

---

## Prerequisites

### Python dependencies

From the repo root:

```bash
pip install -r mcp_server/requirements.txt   # mcp + httpx
pip install openai pyyaml
```

### Environment variables

```bash
export KIMI_API_KEY="your-moonshot-key"
```

Optional, for I/Q:

```bash
export IQ_AUTH_TOKEN="eval-secret-token"
export IQ_MCP_ALLOWED_ROOTS="/abs/path/to/MCP-comparison/runs/autocorrode_iq/work"
```

### Backends

| System | Required backend |
|---|---|
| IsabelleGym | IsabelleGym HTTP server running on `http://localhost:8000` |
| Isabelle-MCP | Either patched Isabelle on PATH, **or** a running Docker container built from `Isabelle-MCP/container/` |
| AutoCorrode I/Q | Isabelle/jEdit running with I/Q plugin listening on `127.0.0.1:8765` |

---

## Configuration

Edit `MCP-comparison/config.yaml` or create `MCP-comparison/config.local.yaml` to override without
touching versioned defaults.

Key knobs:

```yaml
model:
  model_id: kimi-for-coding      # fixed Kimi Code model ID
  temperature: 1.0                # kimi-for-coding only supports 1
  max_tokens: 4096

budgets:
  max_rounds: 40
  problem_wall_cap_seconds: 900   # 15 min
  tool_timeout_seconds: 300       # 5 min
  repeats: 3

mcp_servers:
  isabelle_mcp:
    command: [isabelle-mcp]
  autocorrode_iq:
    command: [python, /abs/path/to/AutoCorrode/iq/iq_bridge.py]
```

---

## Prepare problems

Each problem is a single `.thy` file containing exactly one `theorem … sorry`:

```isabelle
theory Putnam_1988_B1
  imports Main
begin

theorem putnam_1988_b1:
  fixes ...
  shows "..."
  sorry

end
```

Place them in `claude-work/compare-mcps/problems/` (or any directory passed with `--thy-dir`).

---

## Run the comparison

### 1. IsabelleGym

Start the IsabelleGym server first:

```bash
python -m server.app.main
```

Then run:

```bash
python MCP-comparison/run_isabellegym.py --thy-dir claude-work/compare-mcps/problems
```

### 2. Isabelle-MCP

#### Option A — native (patched Isabelle on host)

Ensure patched Isabelle and `isabelle-mcp` are on PATH, then:

```bash
python MCP-comparison/run_isabelle_mcp.py --thy-dir claude-work/compare-mcps/problems
```

#### Option B — Docker container (recommended, host-safe)

Build and start the container from `~/GitHub/Isabelle-MCP/container/`:

```bash
cd ~/GitHub/Isabelle-MCP/container
mkdir -p work
docker build -t isabelle-eval .
docker run -d --name isabelle-eval -v "$PWD/work:/work" isabelle-eval sleep infinity
```

Then configure `MCP-comparison/config.local.yaml`:

```yaml
mcp_servers:
  isabelle_mcp:
    command: [docker, exec, -i, isabelle-eval, isabelle-mcp]

isabelle_mcp_container:
  container_name: isabelle-eval
  host_work_dir: /c/Users/winst/GitHub/Isabelle-MCP/container/work
  container_work_dir: /work
```

Run:

```bash
python MCP-comparison/run_isabelle_mcp.py --thy-dir claude-work/compare-mcps/problems
```

The harness writes `.thy` files to `host_work_dir` and translates paths to `/work/...` for the in-container server.

### 3. AutoCorrode I/Q

Start jEdit with I/Q autostarting:

```bash
export IQ_AUTH_TOKEN="eval-secret-token"
export IQ_MCP_ALLOWED_ROOTS="/abs/path/to/MCP-comparison/runs/autocorrode_iq/work"
make jedit   # from AutoCorrode/iq or AutoCorrode root
```

Then run:

```bash
python MCP-comparison/run_autocorrode_iq.py --thy-dir claude-work/compare-mcps/problems
```

### Run a subset

```bash
python MCP-comparison/run_isabellegym.py --thy-dir claude-work/compare-mcps/problems --select putnam_1988
```

### Override repeat count

```bash
python MCP-comparison/run_isabellegym.py --thy-dir claude-work/compare-mcps/problems --repeats 5
```

---

## Arbiter

Each runner calls the neutral arbiter automatically after every attempt. You can also run it
manually:

```bash
python -m common.arbiter claude-work/compare-mcps/problems/Putnam_1988_B1.thy \
                         MCP-comparison/runs/isabellegym/Putnam_1988_B1_rep0.thy
```

The arbiter checks:
1. `isabelle build` succeeds on a throwaway session importing the problem's imports.
2. No `sorry`/`oops` in the final file.
3. The target theorem name is present.

---

## Analyze results

```bash
python MCP-comparison/analyze.py
```

This prints a Markdown table and per-problem pass@1 breakdown from `MCP-comparison/runs/*/results.jsonl`.

---

## Output schema

Each runner appends one JSON line per `(system, problem, repeat)` to its `results.jsonl`:

```json
{
  "system": "isabellegym",
  "problem": "Putnam_1988_B1",
  "repeat": 0,
  "rounds": 7,
  "n_tool_calls": 11,
  "wall_s": 83.4,
  "model_s": 51.2,
  "prover_s": 30.1,
  "round_latencies": [12.1, 18.7, 10.4],
  "input_tokens": 41201,
  "output_tokens": 2310,
  "total_tokens": 43511,
  "agent_claimed_solved": true,
  "arbiter_solved": true,
  "final_thy_path": "MCP-comparison/runs/isabellegym/Putnam_1988_B1_rep0.thy",
  "error": null,
  "cached_tokens": 33880
}
```

Headline numbers use `arbiter_solved`.

---

## System prompts

No system prompts are used. The model sees only the user prompt plus the MCP tool descriptions from `tools/list`. This keeps the comparison focused on the tool surface.


## Notes and caveats

- **Sequential only.** Each runner processes problems one at a time; Isabelle-MCP and I/Q are
  single-session by design.
- **Fresh session per problem.** Each repeat of each problem starts fresh.
- **Kimi API.** The harness expects an OpenAI-compatible endpoint at `https://api.kimi.com/coding/v1`.
  If Kimi changes its URL or response shape, update `common/model.py` and `config.yaml`.
- **Tool schemas.** The model is given the raw tool list returned by each MCP server's
  `tools/list`. If a model struggles with a particular schema, add a per-tool wrapper or a
  system prompt locally.
- **Timeouts.** Long Isabelle checks can exceed default `tool_timeout_seconds`; raise it in
  `config.yaml` for hard problems.
