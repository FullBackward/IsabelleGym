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
from common.problems import load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger


async def run_attempt(problem, repeat: int, results_path: Path) -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt

    # I/Q reads/writes files inside allowed roots; place the starting file there.
    work_dir = Path(os.environ.get("IQ_MCP_ALLOWED_ROOTS", cfg.paths.runs_dir / "autocorrode" / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    thy_path = work_dir / f"{problem.name}.thy"
    thy_path.write_text(problem.full_text, encoding="utf-8")

    token = os.environ.get("IQ_AUTH_TOKEN", "eval-secret-token")
    messages = [
        {"role": "user", "content": (
            f"Discharge every `sorry` in {thy_path.resolve()}.\n"
            f"Authenticate with token: {token}\n"
            f"Use get_diagnostics(wait_until_processed=true) and get_sorry_positions to verify.\n"
            f"IMPORTANT: use Isabelle ASCII escapes (e.g. \\\\Rightarrow) instead of Unicode math symbols, "
            f"because Isabelle/jEdit rejects some Unicode characters when saving."
        )},
    ]

    logger = SessionLogger("autocorrode", problem.name, repeat, cfg.paths.runs_dir)
    logger.log_message(messages[0])

    result = AttemptResult(
        system="autocorrode",
        problem=problem.name,
        repeat=repeat,
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
                    # Sanitize ALL string arguments — DeepSeek may emit lone surrogates
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

            shutil.copy(thy_path, final_thy_path)
            result.final_thy_path = str(final_thy_path)
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

    verdict = check(problem, final_thy_path, isabelle_bin=cfg.arbiter_isabelle_bin, cleanup=cfg.arbiter_cleanup)
    result.arbiter_solved = verdict["solved"]
    if not result.arbiter_solved and result.error is None:
        result.error = verdict.get("error")

    append_result(results_path, result)
    print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
          f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run AutoCorrode I/Q comparison")
    parser.add_argument("--thy-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--select")
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
                await run_attempt(problem, repeat, results_path)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAILED {problem.name} rep{repeat}: {e}")
                res = AttemptResult(
                    system="autocorrode",
                    problem=problem.name,
                    repeat=repeat,
                    error=str(e),
                )
                append_result(results_path, res)


if __name__ == "__main__":
    asyncio.run(main())