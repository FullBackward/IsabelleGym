#!/usr/bin/env python3
"""IsabelleGym MCP comparison runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time as time_mod
from pathlib import Path

# Allow importing from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient
from common.problems import load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger

# ── Four prompt variants (as 1st user-message bodies) ──────────────────

def _theory_note(problem) -> str:
    return (
        f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
        f"The theory '{problem.name}' is already entered with imports "
        f"{', '.join(problem.imports)}.  Do NOT call enter_theory again — just "
        f"start proving.  If you ever need to restart, use the EXACT call:\n"
        f"enter_theory(name='{problem.name}', "
        f"imports=[{', '.join(repr(i) for i in problem.imports)}])\n\n"
        f"!!! The exact theorem "
        f"'{problem.statement}' "
        f"must appear in the final source.!!!\n\n"
    )


def _general_prompt_body() -> str:
    return """\
CRITICAL RULES:
----------
1. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

2. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False.  Call source()
   to confirm that the CORRECT target theorem (exactly as given) appears in
   the final source — auxiliary lemmas do NOT count.  Reply DONE only after
   verifying the target theorem is present.

3. NEVER use `sorry` or `oops` — they invalidate your proof.
!!! WARNING You have to recheck every rules when you generated a proof !!!
"""


def _restrictive_prompt_body() -> str:
    return """\
CRITICAL RULES:
----------
1. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
   vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
   reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
     a. Submit your proof UP TO that subgoal.
     b. Verify.  If proof_open=True, call sledgehammer() on the open goal.
     c. Use sledgehammer's EXACT output to close the goal.  Do NOT write your
        own solver invocation.  If sledgehammer returns nothing, change
        strategy.

2. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  Do NOT call
   rollback() after a failed verify_chunk — just fix your proof text and call
   verify_chunk again.

3. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False.  Call source()
   to confirm that the CORRECT target theorem (exactly as given) appears in
   the final source — auxiliary lemmas do NOT count.  Reply DONE only after
   verifying the target theorem is present.

4. NEVER use `sorry` or `oops` — they invalidate your proof.

!!! WARNING You have to recheck every rules when you generated a proof !!!
"""


def _stepwise_prompt_body() -> str:
    return """\
CRITICAL RULES (read carefully — violating any of these will fail the proof)
----------

1. INCREMENTAL SUBMISSION — NEVER submit the entire proof at once.  Start by
   submitting only the structural skeleton up to the first `sorry` or open
   subgoal.  Inspect the open subgoals (verify_chunk shows them when
   proof_open=True), understand what needs to be proved, THEN decide how to
   close each subgoal one at a time.

2. SOLVER RULE — NEVER call external solvers (smt, metis, cvc5, vampire,
   z3, verit, e, spass, etc.) directly under any circumstances.  Even if you
   believe the entire proof is correct and uses a solver, you must instead:
     a. Push the proof only up to the point just BEFORE the solver line.
     b. Verify that chunk.  If proof_open=True, call sledgehammer() on the
        open goal.
     c. Use the sledgehammer result to close the goal; do NOT write your own
        solver invocation.  If sledgehammer returns nothing, change strategy.

3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

4. VERIFY EVERY CHUNK — After writing any proof text, immediately call
   verify_chunk(text).  Read the per-command status report.  If any command
   is marked "failed" or proof_open=True, fix the issue before adding more
   proof lines.  Never stack multiple unverified chunks.

5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False.  Call source()
   to confirm that the CORRECT target theorem (exactly as given) appears in
   the final source — auxiliary lemmas do NOT count.  Reply DONE only after
   verifying the target theorem is present.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

LAYERED INCREMENTAL WORKFLOW — HOW TO BUILD A PROOF STEP BY STEP
----------

Each verify_chunk(text) APPENDS commands to the theory source.  Build the
proof in layers — NEVER submit the entire proof at once.  NEVER include
`sorry` or `oops` in any verify_chunk (they mark the proof as invalid and
auto-rollback will leave you with nothing to inspect).

A concrete illustration (using a TRIVIAL theorem — THIS IS NOT YOUR TASK):

  [Layer 1 — structural outline]
  verify_chunk("
    theorem trivial: \\"(1::nat) + 1 = 2\\"
    proof -
      have step1: \\"Suc 0 + 1 = 2\\" by simp
  ")
  → success=True, proof_open=True → call proof_state() to inspect subgoals!

  [Layer 2 — continue reasoning]
  verify_chunk("
      also have \\"Suc 0 + 1 = Suc (0 + 1)\\" by simp
  ")
  → success=True, proof_open=True (still open — more to prove)

  [Layer 3 — close the proof]
  verify_chunk("
      finally show ?thesis by simp
    qed
  ")
  → success=True, proof_open=False, used_sorry=False → DONE!

LAYER RULES:
- Layer 1: theorem statement + proof - + initial reasoning (5-8 lines, no qed)
- Middle layers: derive intermediate facts (5-10 lines each)
- Last layer: close with qed (2-3 lines)
- After EVERY successful layer with proof_open=True, inspect subgoals via proof_state()
- Use simp/linarith/argo/auto/presburger for routine steps
- Call sledgehammer() ONLY when these methods fail on a subgoal
- NEVER submit both the theorem declaration AND qed in the SAME verify_chunk
- NEVER use `sorry` or `oops` — they invalidate your proof
"""


def _segment_prompt_body() -> str:
    return """\
CRITICAL RULES:
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
   to confirm that the CORRECT target theorem (exactly as given) appears in
   the final source — auxiliary lemmas do NOT count.  Reply DONE only after
   verifying the target theorem is present.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

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
- If a segment times out (180s), try breaking it into smaller pieces.
"""


_PROMPTS = {
    "general":    _general_prompt_body,
    "restrictive": _restrictive_prompt_body,
    "stepwise":   _stepwise_prompt_body,
    "segment":    _segment_prompt_body,
}

# ═════════════════════════════════════════════════════════════════════════

async def run_attempt(
    problem,
    repeat: int,
    results_path: Path,
    prompt_name: str = "segment",
) -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt
    body_fn = _PROMPTS.get(prompt_name, _segment_prompt_body)
    messages = [
        {"role": "user", "content": (
            _theory_note(problem) +
            body_fn() + "\n\n" +
            problem.statement + "\n"
        )},
    ]

    result = AttemptResult(
        system="isabellegym",
        problem=problem.name,
        repeat=repeat,
        model_id=cfg.model.model_id,
        model_provider=cfg.model.provider,
        model_temperature=cfg.model.temperature,
    )
    timer = Timer()
    tokens = TokenAggregator()
    tool_times: list[float] = []
    round_latencies: list[float] = []

    runs_dir = cfg.paths.runs_dir / "isabellegym"
    runs_dir.mkdir(parents=True, exist_ok=True)
    final_thy_path = runs_dir / f"{problem.name}_rep{repeat}.thy"
    logger = SessionLogger("isabellegym", problem.name, repeat, cfg.paths.runs_dir)
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
    logger.log_message(messages[0])

    try:
        async with mcp_session(cfg.mcp_servers["isabellegym"]) as session:
            mcp_tools = await list_tools(session)
            await call_tool(session, "enter_theory", {
                "name": problem.name,
                "imports": problem.imports,
            })

            timer.start()
            round_start: float = 0.0
            for _round in range(cfg.budgets.max_rounds):
                elapsed = timer.elapsed()
                if elapsed >= cfg.budgets.problem_wall_cap_seconds:
                    result.error = f"problem wall cap exceeded ({cfg.budgets.problem_wall_cap_seconds}s)"
                    break

                round_result = await client.chat(messages, tools=mcp_tools, system_prompt=system_prompt)
                tokens.add(round_result.usage)
                result.rounds += 1
                now = timer.elapsed()
                round_latencies.append(round(now - round_start, 2))
                round_start = now

                assistant_message = {
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in round_result.tool_calls
                    ],
                }
                logger.log_message(assistant_message)

                if not round_result.tool_calls:
                    if not (round_result.assistant_text or "").strip():
                        result.error = "Model returned empty response (likely content filter)"
                    if "DONE" in (round_result.assistant_text or ""):
                        result.agent_claimed_solved = True
                    break

                tool_outputs = []
                for tc in round_result.tool_calls:
                    result.n_tool_calls += 1
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        result.error = f"JSONDecodeError: {e}"
                        break
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)
                    t0 = time_mod.time()
                    try:
                        output = await asyncio.wait_for(
                            call_tool(session, name, args),
                            timeout=cfg.budgets.tool_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        output = f"Tool call timed out after {cfg.budgets.tool_timeout_seconds}s"
                    tool_times.append(time_mod.time() - t0)
                    tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": output})
                    logger.log_tool_result(tc.id, name, output)
                    if name == "verify_chunk":
                        result.agent_claimed_solved = (
                            "success=True" in output and "proof_open=False" in output and "used_sorry=False" in output
                        )

                messages.append(assistant_message)
                messages.extend(tool_outputs)
                if result.agent_claimed_solved:
                    break

            result.wall_s = round(timer.stop(), 2)
            result.input_tokens = tokens.input_tokens
            result.output_tokens = tokens.output_tokens
            result.cached_tokens = tokens.cached_tokens
            result.prover_s = round(sum(tool_times), 2) if tool_times else None
            result.round_latencies = round_latencies

            source_json = await call_tool(session, "source", {})
            try:
                src = json.loads(source_json).get("source", source_json)
            except Exception:
                src = source_json
            if not src.rstrip().endswith("end"):
                src = src.rstrip() + "\nend\n"
            final_thy_path.write_text(src, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
            logger.log_text("FINAL_SOURCE", src)
            await call_tool(session, "close_theory", {})

    except Exception as e:
        while hasattr(e, "exceptions") and getattr(e, "exceptions"):
            subs = getattr(e, "exceptions")
            if subs:
                e = subs[0]
            else:
                break
        msg = f"{type(e).__name__}: {e}"
        logger.log_text("ERROR", msg)
        result.error = msg
        result.wall_s = round(timer.stop(), 2) if timer.t0 is not None else 0.0
        result.input_tokens = tokens.input_tokens
        result.output_tokens = tokens.output_tokens
        result.cached_tokens = tokens.cached_tokens
        result.prover_s = round(sum(tool_times), 2) if tool_times else None
        result.round_latencies = round_latencies
        # Ensure the server session is released even on error
        try:
            await call_tool(session, "close_theory", {})
        except Exception:
            pass
    finally:
        # Always release the server session to prevent idle-session buildup
        try:
            await call_tool(session, "close_theory", {})
        except Exception:
            pass
        # Arbiter (must be before logger.close() to keep the file descriptor open)
        if final_thy_path.exists():
            verdict = await check(problem, final_thy_path, gym_url=cfg.arbiter_gym_url)
            logger.log_text("ARBITER_VERDICT", (
                f"solved={verdict['solved']} "
                f"error={verdict.get('error')} "
                f"build_log={verdict.get('build_log', '')}"
            ))
            result.arbiter_solved = verdict["solved"]
            if not result.arbiter_solved and result.error is None:
                result.error = verdict.get("error")
        else:
            result.arbiter_solved = False
            if result.error is None:
                result.error = "no final theory file available for arbiter"

        append_result(results_path, result)
        logger.close()
        print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
              f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run IsabelleGym MCP comparison")
    parser.add_argument("--thy-dir", required=True, type=Path, help="Directory containing .thy problems")
    parser.add_argument("--repeats", type=int, default=None, help="Overrides config repeats")
    parser.add_argument("--select", help="Only run problems whose name contains this substring")
    parser.add_argument("--prompt", choices=list(_PROMPTS), default="segment",
                        help="Which prompt variant to use (default: segment)")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    results_path = cfg.paths.runs_dir / "isabellegym" / "results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    for problem in problems:
        for repeat in range(repeats):
            try:
                await run_attempt(problem, repeat, results_path, prompt_name=args.prompt)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAILED {problem.name} rep{repeat}: {e}")
                res = AttemptResult(
                    system="isabellegym",
                    problem=problem.name,
                    repeat=repeat,
                    model_id=cfg.model.model_id,
                    model_provider=cfg.model.provider,
                    model_temperature=cfg.model.temperature,
                    error=str(e),
                )
                append_result(results_path, res)


if __name__ == "__main__":
    asyncio.run(main())
