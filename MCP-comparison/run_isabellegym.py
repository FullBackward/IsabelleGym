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


async def run_attempt(
    problem,
    repeat: int,
    results_path: Path,
) -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt
    messages = [
        {"role": "user", "content": f"Prove this Isabelle/HOL theorem:\n\n{problem.statement}\n"},
    ]

    result = AttemptResult(
        system="isabellegym",
        problem=problem.name,
        repeat=repeat,
    )
    timer = Timer()
    tokens = TokenAggregator()
    tool_times: list[float] = []
    round_latencies: list[float] = []

    runs_dir = cfg.paths.runs_dir / "isabellegym"
    runs_dir.mkdir(parents=True, exist_ok=True)
    final_thy_path = runs_dir / f"{problem.name}_rep{repeat}.thy"
    logger = SessionLogger("isabellegym", problem.name, repeat, cfg.paths.runs_dir)
    logger.log_message(messages[0])

    try:
        async with mcp_session(cfg.mcp_servers["isabellegym"]) as session:
            # Discover available tools for the model
            mcp_tools = await list_tools(session)
            await call_tool(session, "enter_theory", {
                "name": problem.name,
                "imports": problem.imports,
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

                assistant_message = {
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
                }
                logger.log_message(assistant_message)

                if not round_result.tool_calls:
                    # Model gave final text response
                    if "DONE" in (round_result.assistant_text or ""):
                        result.agent_claimed_solved = True
                    break

                # Execute tool calls
                tool_outputs = []
                for tc in round_result.tool_calls:
                    result.n_tool_calls += 1
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    # Sanitize string args — DeepSeek may emit lone surrogates
                    # (e.g. U+DCA0) that Isabelle's UTF-8-Isabelle encoding rejects.
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
                    tool_outputs.append({
                        "tool_call_id": tc.id,
                        "role": "tool",
                        "name": name,
                        "content": output,
                    })
                    logger.log_tool_result(tc.id, name, output)
                    # Capture source on successful verify_chunk or close to end
                    if name == "verify_chunk":
                        result.agent_claimed_solved = (
                            "success=True" in output
                            and "proof_open=False" in output
                            and "used_sorry=False" in output
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

            # Save final source
            source_json = await call_tool(session, "source", {})
            try:
                src = json.loads(source_json).get("source", source_json)
            except Exception:
                src = source_json
            final_thy_path.write_text(src, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
            logger.log_text("FINAL_SOURCE", src)

    except Exception as e:
        logger.log_text("ERROR", f"{type(e).__name__}: {e}")
        result.error = f"{type(e).__name__}: {e}"
        result.wall_s = round(timer.stop(), 2) if timer.t0 is not None else 0.0
        result.input_tokens = tokens.input_tokens
        result.output_tokens = tokens.output_tokens
        result.cached_tokens = tokens.cached_tokens
        result.prover_s = round(sum(tool_times), 2) if tool_times else None
        result.round_latencies = round_latencies
        if final_thy_path.exists():
            result.final_thy_path = str(final_thy_path)
    finally:
        logger.close()

    # Arbiter
    verdict = check(problem, final_thy_path, isabelle_bin=cfg.arbiter_isabelle_bin, cleanup=cfg.arbiter_cleanup)
    result.arbiter_solved = verdict["solved"]
    if not result.arbiter_solved and result.error is None:
        result.error = verdict.get("error")

    append_result(results_path, result)
    print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
          f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run IsabelleGym MCP comparison")
    parser.add_argument("--thy-dir", required=True, type=Path, help="Directory containing .thy problems")
    parser.add_argument("--repeats", type=int, default=None, help="Overrides config repeats")
    parser.add_argument("--select", help="Only run problems whose name contains this substring")
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
                await run_attempt(problem, repeat, results_path)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAILED {problem.name} rep{repeat}: {e}")
                # Record failure so analysis can see it
                res = AttemptResult(
                    system="isabellegym",
                    problem=problem.name,
                    repeat=repeat,
                    error=str(e),
                )
                append_result(results_path, res)


if __name__ == "__main__":
    asyncio.run(main())