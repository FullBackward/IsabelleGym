#!/usr/bin/env python3
"""Isabelle-MCP comparison runner.

Supports two modes:
- Native: patched Isabelle + isabelle-mcp on host PATH.
- Container: MCP server runs inside a Docker container; harness writes files on the host
  and translates paths to the container's view (mirrors container/eval_harness.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time as time_mod
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import Config, load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient
from common.problems import Problem, load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger


def to_container_path(value: str, cfg: Config) -> str:
    host_dir = cfg.isabelle_mcp_container.host_work_dir
    container_dir = cfg.isabelle_mcp_container.container_work_dir
    if host_dir is None or not isinstance(value, str):
        return value
    try:
        hp = Path(value).resolve()
        hd = host_dir.resolve()
        rel = hp.relative_to(hd)
        return str(PurePosixPath(container_dir) / rel)
    except ValueError:
        return value


def to_host_path(value: str, cfg: Config) -> Path:
    host_dir = cfg.isabelle_mcp_container.host_work_dir
    container_dir = cfg.isabelle_mcp_container.container_work_dir
    if host_dir is None or not isinstance(value, str):
        return Path(value)
    cp = PurePosixPath(value)
    cwd = PurePosixPath(container_dir)
    if cp.is_absolute() and cwd.parts and cp.parts[: len(cwd.parts)] == cwd.parts:
        rel = cp.relative_to(cwd)
        return host_dir.resolve() / rel
    return Path(value)


def write_thy_on_host(path: str, content: str, cfg: Config) -> str:
    host_path = to_host_path(path, cfg)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    host_path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path}"


async def run_attempt(problem: Problem, repeat: int, results_path: Path) -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt

    # File that the agent/server will see. In container mode this is the container path;
    # the actual bytes are written to the host_work_dir mapping.
    work_dir = cfg.paths.runs_dir / "isabelle_mcp" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    host_thy_path = work_dir / f"{problem.name}.thy"
    host_thy_path.write_text(problem.full_text, encoding="utf-8")

    if cfg.isabelle_mcp_container.host_work_dir:
        # Mirror the starting file into the container's bind mount if needed.
        container_bind_host = cfg.isabelle_mcp_container.host_work_dir
        container_bind_host.mkdir(parents=True, exist_ok=True)
        mirrored = container_bind_host / f"{problem.name}.thy"
        shutil.copy(host_thy_path, mirrored)
        thy_path_str = to_container_path(str(mirrored), cfg)
    else:
        thy_path_str = str(host_thy_path.resolve())

    messages = [
        {"role": "user", "content": (
            f"Prove the target theorem below.  Write your proof into the theory file at "
            f"{thy_path_str} using write_thy.\n\n"
            f"Use the verification tool to check each edit.  If a command fails, read its "
            f"error and fix that line.  If a tactic loops (timeout), replace it.\n\n"
            f"IMPORTANT: the theorem is proved ONLY when the verification tool reports success "
            f"with zero errors.  Never use sorry/oops — they do not count as proved.  After "
            f"writing your final proof, call the verification tool to confirm it passes.\n\n"
            f"Reply DONE only after the verification tool reports success with no errors.\n\n"
            f"Theory: {problem.name}\nImports: {problem.imports}\n"
            f"Target theorem:\n{problem.statement}\n"
        )},
    ]

    logger = SessionLogger("isabelle_mcp", problem.name, repeat, cfg.paths.runs_dir)
    logger.log_message(messages[0])

    result = AttemptResult(
        system="isabelle_mcp",
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
    final_thy_path = cfg.paths.runs_dir / "isabelle_mcp" / f"{problem.name}_rep{repeat}.thy"

    try:
        async with mcp_session(cfg.mcp_servers["isabelle_mcp"]) as session:
            mcp_tools = await list_tools(session)
            await call_tool(session, "isabelle_launch", {"session": problem.imports[0]})

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
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        result.error = f"JSONDecodeError: {e}"
                        break
                    # Sanitize string args — DeepSeek may emit lone surrogates
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)

                    t0 = time_mod.time()
                    if name == "write_thy":
                        # write_thy is implemented locally so the file lands on the host
                        container_path = to_container_path(args["path"], cfg)
                        output = write_thy_on_host(container_path, args["content"], cfg)
                    else:
                        # Normalize paths for container-aware tools
                        for key in ("path", "file_path"):
                            if key in args and isinstance(args[key], str):
                                args[key] = to_container_path(args[key], cfg)
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
            result.round_latencies = round_latencies

            # Copy final file as artifact. Prefer the mirrored/container copy if it exists.
            source = host_thy_path
            if cfg.isabelle_mcp_container.host_work_dir:
                mirrored_source = cfg.isabelle_mcp_container.host_work_dir / f"{problem.name}.thy"
                if mirrored_source.exists():
                    source = mirrored_source
            shutil.copy(source, final_thy_path)
            result.final_thy_path = str(final_thy_path)
    except Exception as e:
        import traceback as _tb
        # Recursively unwrap nested ExceptionGroups to find the root cause
        while hasattr(e, "exceptions") and getattr(e, "exceptions"):
            subs = getattr(e, "exceptions")
            if subs:
                e = subs[0]
            else:
                break
        msg = f"{type(e).__name__}: {e}"
        _tb_str = _tb.format_exc()
        logger.log_text("ERROR", msg)
        logger.log_text("TRACEBACK", _tb_str)
        # Include traceback summary in the error so it reaches results.jsonl
        _lines = _tb_str.strip().split("\n")
        _summary = "\n".join(_lines[-5:]) if len(_lines) > 5 else _tb_str
        msg = f"{msg}\n[TRACEBACK]\n{_summary}"
        result.error = msg
        result.wall_s = round(timer.stop(), 2) if timer.t0 is not None else 0.0
        result.input_tokens = tokens.input_tokens
        result.output_tokens = tokens.output_tokens
        result.cached_tokens = tokens.cached_tokens
        result.prover_s = round(sum(tool_times), 2) if tool_times else None
        result.round_latencies = round_latencies
        # Try to preserve the theory file from the work directory
        try:
            if host_thy_path.exists():
                shutil.copy(host_thy_path, final_thy_path)
                result.final_thy_path = str(final_thy_path)
        except Exception:
            pass
    finally:
        # Arbiter (must be before logger.close())
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
    parser = argparse.ArgumentParser(description="Run Isabelle-MCP comparison")
    parser.add_argument("--thy-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--select")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    results_path = cfg.paths.runs_dir / "isabelle_mcp" / "results.jsonl"
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
                    system="isabelle_mcp",
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