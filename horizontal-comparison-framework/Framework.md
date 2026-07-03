# Horizontal comparison of MCPs interacting Isabelle prover

This is a unified framework for horizonally comparing current (2026.06) MCP servers for inetracting with Isabelle prover.

## Compared products
- Isabelle-MCP (https://github.com/xqyww123/Isabelle-MCP)
- Autocorrode-I/Q (https://github.com/awslabs/AutoCorrode/tree/main/iq)
- IsabelleGym (https://github.com/FullBackward/IsabelleGym/tree/server-cli)


## Global constants

Held identical across every system, problem, and repeat (the fairness controls).

- `model` — the model id, served via the facility's OpenAI-compatible endpoint (e.g. GPT-5.5). Same for all three systems.
- `settings` — decoding: `temperature`, `max_tokens` (+ reasoning effort if the model exposes it).
- `max_rounds` — cap on agent rounds per attempt (e.g. 40); hitting it ⇒ the attempt counts as unsolved.
- `repeats` — number of independent runs per problem (e.g. 3), for variance and pass@1.
- `problem_wall_cap`, `tool_timeout` — per-problem and per-tool wall limits (identical for all systems).

## Metrics

Each result row is identified by the **dimensions** `system` × `problem` × `repeat` (indices, not
metrics).

### Solved (outcome)
- `arbiter_solved` (bool) — ground-truth success from the neutral arbiter (final `.thy` builds clean, no `sorry`/`oops`, target lemma proved). **The only success signal used in headline numbers.**
- Aggregated over the `repeats` runs as **pass@1** (fraction solved).
- *(optional)* `agent_claimed_solved` — the MCP's own success signal; kept only to report self-report-vs-arbiter agreement.

### Rounds
- `rounds` — agent inference turns used for the attempt (≤ `max_rounds`).

### Number of tool calls
- `n_tool_calls` — total MCP tool invocations in the attempt (a round may issue several); complements `rounds`.

### Latency
- **Wall-clock**: `wall_s` (seconds) — **PRIMARY**: the headline latency; always measurable; drives every conclusion.
- **Prover-clock**: `prover_s` (seconds) — **SECONDARY (differentiator)**: the model is held constant across all three systems, so prover/MCP time is what actually separates them. Captured directly in the SDK cross-check; approximated under OpenCode from per-tool step timing.
- **Round-latencies**: `round_latencies` (list) — **SECONDARY (diagnostic)**: per-round profile; explains where time goes.
- **Model-clock**: `model_s` (seconds) — **DERIVED / optional**: `model_s ≈ wall_s − prover_s`. Demoted — with the model fixed it barely discriminates between systems; report only if the model/prover split is available.

#### Formulas

**Primary metric — per-problem wall-clock.** A single wall-clock delta spanning the whole agent loop (every model call *and* every prover/tool evaluation in between):

```
wall_s = round(t_end − t_0, 2)
```

- `t_0`   — `time.time()` captured **just before the first model call**.
- `t_end` — `time.time()` captured **after the agent loop finishes**.

Units: seconds, 2 dp. Provider-independent. This is the definition used by `claude-work/impl-mcp-server/bench_mcp_agent.py` (`latency_s`) and is adopted verbatim for all three systems.

**Equivalent decomposition** over rounds `r` and the tool calls `t` within each round:

```
wall_s ≈ Σ_r ( model_call_time_r + Σ_t tool_exec_time_{r,t} ) + overhead
```

**Prover-clock (secondary, the differentiator) and model-clock (derived):**

```
prover_s = Σ_{r,t} ( t_after_tool   − t_before_tool )          # summed MCP tool/eval time
model_s  = Σ_r    ( t_after_create_r − t_before_create_r )     # summed model-call time
                                                               #   (DERIVED ≈ wall_s − prover_s)
wall_s   ≈ model_s + prover_s + small_overhead
```

**Round-latencies** — one delta per round; each bundles the tool/prover work triggered by the previous round plus the next model call:

```
round_latency_r = t(model response r) − t(model response r−1)
```

**Aggregate for reporting** — mean over repeats, conditioned on solved (unsolved runs hit the round cap and would inflate the mean):

```
mean_wall_s(solved) = mean{ wall_s : repeats where arbiter_solved }
```
### Token usage
- `input_tokens`, `output_tokens` — prompt / completion tokens summed over rounds (provider `usage`).
- `total_tokens` — `input_tokens + output_tokens` (derived).
- `cached_tokens` — cached prompt tokens (OpenAI `prompt_tokens_details.cached_tokens`); a subset of `input_tokens`. No separate cache-creation field on OpenAI-compatible endpoints.

### Demo report json
Constants (`model`, `settings`, `max_rounds`, `repeats`, timeouts) live in **Global constants**, not
in each row.

```json
{
  // dimensions (identify the row; not metrics)
  "system": "isabellegym | isabelle-mcp | autocorrode",
  "problem": "putnam_1988_b1",
  "repeat": 0,

  // outcome
  "arbiter_solved": true,         // ground truth (neutral arbiter) — the only success used in headline numbers
  "agent_claimed_solved": true,   // optional — the MCP's own signal, for self-report-vs-truth agreement

  // effort
  "rounds": 7,                    // agent inference turns (≤ max_rounds)
  "n_tool_calls": 11,             // total MCP tool invocations (a round may issue several)

  // latency
  "wall_s": 83.4,                 // PRIMARY — per-problem wall-clock = round(t_end − t_0, 2)
  "prover_s": 30.1,               // SECONDARY (differentiator) — Σ tool/eval time
  "round_latencies": [12.1, 18.7, 10.4],  // SECONDARY (diagnostic) — per-round deltas
  "model_s": 51.2,                // DERIVED/optional — ≈ wall_s − prover_s; null if split unavailable

  // token usage
  "input_tokens": 41201,
  "output_tokens": 2310,
  "total_tokens": 43511,          // input_tokens + output_tokens
  "cached_tokens": 33880,         // cached prompt tokens (subset of input_tokens)

  // bookkeeping (not compared)
  "final_thy_path": "runs/isabellegym/putnam_1988_b1_rep0.thy",
  "error": null
}
```

## Common prerequirements

## Specialised prerequirements and alternations

