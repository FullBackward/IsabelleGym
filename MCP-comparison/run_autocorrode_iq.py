#!/usr/bin/env python3
"""AutoCorrode I/Q comparison runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time as time_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient
from common.problems import load_problems
from common.session_logger import SessionLogger

# ── IQ prompt variants ─────────────────────────────────────────────────

def _general_prompt_body(thy_path: Path) -> str:
    return (
        f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
        f"Discharge every `sorry` in {thy_path.resolve()} — replace the `sorry` keyword "
        f"with a complete proof block.  Write your proof using write_file. You should complete "
        f"the proof before replying DONE. If you cannot give the reason of why.\n\n"
        f"After each edit, call get_diagnostics(wait_until_processed=true) to check for "
        f"errors.  Fix red lines before moving on.  The theorem is proved ONLY when there "
        f"are zero errors and get_sorry_positions reports count=0.\n\n"
        f"Before replying DONE, call get_sorry_positions to confirm count=0 and "
        f"get_diagnostics to confirm zero errors.\n\n"
        f"IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's\n"
        f"\\<name> escape notation — NEVER use raw Unicode characters.  Common escapes:\n"
        f"  \\<forall> = ∀    \\<exists> = ∃    \\<Rightarrow> = ⇒    \\<and> = ∧\n"
        f"  \\<or> = ∨       \\<not> = ¬    \\<equiv> = ≡    \\<noteq> = ≠\n"
        f"  \\<le> = ≤       \\<ge> = ≥    \\<in> = ∈      \\<subseteq> = ⊆\n"
        f"  \\<union> = ∪    \\<inter> = ∩   \\<forall>x. = ∀x.\n"
        f"For any other symbol, use \\<name> where name is its ASCII identifier.\n"
        f"Unicode characters will be REJECTED by Isabelle/save — always use \\<...>.\n"
        f"WARNING!!!! Everytime you generated a proof, recheck if it contains illegal UTF symbols!!!!\n\n"
        f"Note: I/R is not installed, do not use it.\n\n"
    )


def _restrictive_prompt_body(thy_path: Path) -> str:
    return (
        f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
        f"Discharge every `sorry` in {thy_path.resolve()} — replace the `sorry` keyword "
        f"with a complete proof block.  Write your proof using write_file. You should complete "
        f"the proof before replying DONE. If you cannot give the reason of why.\n\n"
        f"After each edit, call get_diagnostics(wait_until_processed=true) to check for "
        f"errors.  Fix red lines before moving on.  The theorem is proved ONLY when there "
        f"are zero errors and get_sorry_positions reports count=0.\n\n"
        f"Before replying DONE, call get_sorry_positions to confirm count=0 and "
        f"get_diagnostics to confirm zero errors.\n\n"
        f"IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's\n"
        f"\\<name> escape notation — NEVER use raw Unicode characters.  Common escapes:\n"
        f"  \\<forall> = ∀    \\<exists> = ∃    \\<Rightarrow> = ⇒    \\<and> = ∧\n"
        f"  \\<or> = ∨       \\<not> = ¬    \\<equiv> = ≡    \\<noteq> = ≠\n"
        f"  \\<le> = ≤       \\<ge> = ≥    \\<in> = ∈      \\<subseteq> = ⊆\n"
        f"  \\<union> = ∪    \\<inter> = ∩   \\<forall>x. = ∀x.\n"
        f"For any other symbol, use \\<name> where name is its ASCII identifier.\n"
        f"Unicode characters will be REJECTED by Isabelle/save — always use \\<...>.\n\n"
        f"SOLVER RULE: NEVER use external solvers (smt, metis, cvc5, vampire, eprover, z3, "
        f"spass, verit, zipperposition) directly in your proof text.  You MUST call "
        f"explore(query=\"sledgehammer\") on the current goal first.  If sledgehammer cannot "
        f"find a proof, the current approach is probably wrong — change strategy instead of "
        f"trying more solver calls manually.\n"
        f"WARNING!!!! Everytime you generated a proof, recheck if it contains illegal UTF symbols!!!!\n\n"
        f"Note: I/R is not installed, do not use it.\n\n"
    )


_PROMPTS = {
    "general":    _general_prompt_body,
    "restrictive": _restrictive_prompt_body,
}

# ═════════════════════════════════════════════════════════════════════════

async def run_attempt(problem, repeat: int, results_path: Path, prompt_name: str = "general") -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt

    # I/Q reads/writes files inside allowed roots; place the starting file there.
    work_dir = Path(os.environ.get("IQ_MCP_ALLOWED_ROOTS", cfg.paths.runs_dir / "autocorrode" / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    thy_path = work_dir / f"{problem.name}.thy"
    # Always start with a clean file — delete any leftover from a previous attempt.
    if thy_path.exists():
        thy_path.unlink()
    thy_path.write_text(problem.full_text, encoding="utf-8")

    token = os.environ.get("IQ_AUTH_TOKEN", "eval-secret-token")
    body_fn = _PROMPTS.get(prompt_name, _general_prompt_body)
    role_text = (
        "You are an expert interactive theorem prover assistant for Isabelle/HOL. "
        "Your job is to construct a complete, correct Isar proof of the target theorem, "
        "using the tools provided by the Isabelle MCP server you are connected to.\n\n"
    )
    content = role_text + body_fn(thy_path) + f"Authenticate with token: {token}"
    messages = [{"role": "user", "content": content}]

    logger = SessionLogger("autocorrode", problem.name, repeat, cfg.paths.runs_dir)
    logger.log_message(messages[0])

    result = AttemptResult(
        system="autocorrode",
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
    final_thy_path = cfg.paths.runs_dir / "autocorrode" / f"{problem.name}_rep{repeat}.thy"

    try:
        async with mcp_session(cfg.mcp_servers["autocorrode_iq"]) as session:
            mcp_tools = await list_tools(session)
            await call_tool(session, "authenticate", {"token": token})
            await call_tool(session, "open_file", {"path": str(thy_path.resolve())})
            # Force IQ's in-memory buffer to match the fresh file on disk.
            # open_file alone does not guarantee the buffer is reset — IQ
            # may retain cached content from a previous session.
            await call_tool(session, "write_file", {
                "path": str(thy_path.resolve()),
                "command": "write",
                "content": problem.full_text,
            })

            timer.start()
            round_start: float = 0.0
            for _round in range(cfg.budgets.max_rounds):
                # Enforce per-problem wall cap
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

                logger.log_message({
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in (round_result.tool_calls or [])
                    ],
                })

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
                    args = json.loads(tc.function.arguments)
                    # Force path to our scoped work file
                    if "path" in args and "file" not in args:
                        args["path"] = str(thy_path.resolve())
                    if "file" in args:
                        args["file"] = str(thy_path.resolve())
                    t0 = time_mod.time()
                    try:
                        output = await asyncio.wait_for(
                            call_tool(session, name, args),
                            timeout=cfg.budgets.tool_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        output = f"Tool call timed out after {cfg.budgets.tool_timeout_seconds}s"
                    tool_times.append(time_mod.time() - t0)

                    logger.log_tool_result(tc.id, name, output)

                    tool_outputs.append({
                        "tool_call_id": tc.id,
                        "role": "tool",
                        "name": name,
                        "content": output,
                    })

                messages.append({
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in round_result.tool_calls
                    ],
                })
                messages.extend(tool_outputs)

            result.wall_s = round(timer.stop(), 2)
            result.input_tokens = tokens.input_tokens
            result.output_tokens = tokens.output_tokens
            result.cached_tokens = tokens.cached_tokens
            result.prover_s = round(sum(tool_times), 2) if tool_times else None
            result.model_s = round(result.wall_s - (result.prover_s or 0), 2) if result.prover_s else None
            result.round_latencies = round_latencies

            # IQ server works in-memory; read the edited content before session
            # is torn down (which reverts the file on disk).  save_file is a
            # no-op on IQ, so we read back the current content and write it
            # ourselves.
            try:
                raw = await call_tool(session, "read_file", {
                    "path": str(thy_path.resolve()),
                    "mode": "Line",
                })
                # The IQ read_file Line mode returns JSON: {"content": " 1:...\n  2:..."}
                data = json.loads(raw)
                content = data.get("content", raw) if isinstance(data, dict) else raw
                # Strip line-number prefixes added by IQ's Line mode:
                #   "  1:theory ..." → "theory ..."
                import re as _re
                content = _re.sub(r"^[ \t]*\d+:", "", content, flags=_re.MULTILINE)
            except Exception:
                content = thy_path.read_text(encoding="utf-8")
            final_thy_path.write_text(content, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
    except Exception as e:
        # Recursively unwrap nested ExceptionGroups to find the root cause
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
        # Try to preserve the final theory file
        try:
            if thy_path.exists():
                shutil.copy(thy_path, final_thy_path)
                result.final_thy_path = str(final_thy_path)
        except Exception:
            pass
    finally:
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

        # Clean up the work file after the attempt so the next attempt gets a fresh copy.
        if thy_path.exists():
            thy_path.unlink()
        append_result(results_path, result)
        logger.close()
        print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
              f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run AutoCorrode I/Q comparison")
    parser.add_argument("--thy-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--select")
    parser.add_argument("--prompt", choices=list(_PROMPTS), default="general",
                        help="Which prompt variant to use (default: general)")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    if "IQ_AUTH_TOKEN" not in os.environ:
        print("Warning: IQ_AUTH_TOKEN not set; using default 'eval-secret-token'")

    results_path = cfg.paths.runs_dir / "autocorrode" / "results.jsonl"
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
                    system="autocorrode",
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