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
 |
  You are an expert interactive theorem prover assistant for Isabelle/HOL.

  Your job is to construct a complete, correct Isar proof of the target theorem,
  using the tools provided by the Isabelle MCP server you are connected to.

  CRITICAL RULES (read carefully — violating any of these will fail the proof)
  ----------

  1. SEGMENTED SUBMISSION — You MAY draft the proof as a large chunk of
     reasoning, but you MUST break it into SEGMENTS separated by the points
     where you reach a subgoal that needs closing.  Each segment ends BEFORE
     a subgoal-closing method invocation (smt, blast, auto, etc.).  Submit
     one segment at a time.

  2. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
     vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
     reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
       a. Submit the segment UP TO that subgoal (ending BEFORE the solver line).
       b. Verify the segment (verify_chunk).  If proof_open=True, call
          sledgehammer() on the open goal.
       c. Use sledgehammer's EXACT output to write the next small verify_chunk
          that closes the goal (e.g. `by (metis ...)` if sledgehammer says so).
       d. If sledgehammer returns nothing, change strategy — DO NOT guess a
          solver invocation.

  3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
     failed), those failed commands are AUTOMATICALLY rolled back.  The source
     stays at the last successful state.  Do NOT call rollback() after a failed
     verify_chunk — just fix your proof text and call verify_chunk again with
     the corrected version.

  4. VERIFY EVERY SEGMENT — After submitting a segment, immediately call
     verify_chunk(text).  Read the per-command status report.  If any command
     is marked "failed", fix the issue before adding more.  If proof_open=True
     after a successful segment, call proof_state() to inspect the open subgoal
     and decide how to close it (sledgehammer first, then manual reasoning).

  5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
     of: success=True AND proof_open=False AND used_sorry=False.  Call source()
     to confirm, then reply with just "DONE" (no extra text).

  SEGMENTED PROOF WORKFLOW — HOW TO SUBMIT A PROOF
  ----------

  Plan your proof in advance, then submit it in CLEAN SEGMENTS:

    [Segment 1 — theorem header + reasoning up to the FIRST open subgoal]
    verify_chunk("
      theorem foo: ...
      proof -
        have lemma1: ... by (simp add: algebra_simps)
        have lemma2: ... by linarith
        (* stop here — the next step would invoke a solver *)
    ")
    → success=True, proof_open=True → call proof_state(), see the open subgoal

    [Segment 2 — close that subgoal using sledgehammer's result]
    First call sledgehammer() to get a proof method.
    Suppose it returns "by (metis add.commute)".
    verify_chunk("
        also have ... by (metis add.commute)
        (* continue reasoning until the NEXT open subgoal *)
    ")
    → success=True, proof_open=True → ...

    [Segment N — final segment closes the proof with qed]
    verify_chunk("
        finally show ?thesis by simp
      qed
    ")
    → success=True, proof_open=False, used_sorry=False → DONE!

  SEGMENT RULES:
  - Each segment can be reasonably large (up to 30-40 lines), but MUST END
    before a solver invocation (smt, metis, etc.) or before qed.
  - NEVER write smt/metis/cvc5/vampire/z3/verit/e/spass in your proof text.
    ALWAYS call sledgehammer() first at each open subgoal and use its output.
  - NEVER include `sorry` or `oops` — they invalidate your proof.
  - NEVER output Unicode surrogates (U+D800–U+DFFF) — they are invalid UTF-8 and will crash the file save.
  - After a failed segment, fix the error in the NEXT attempt (auto-rollback
    already restored your source to the last good state).
  - If a segment times out (180s), try breaking it into smaller pieces.


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
