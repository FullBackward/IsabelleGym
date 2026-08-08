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
import re
import shutil
import sys
import time as time_mod
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import Config, load
from common.mcp_client import call_tool, list_tools, mcp_session_startup_retry
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient, no_tool_call_action
from common.problems import Problem, derive_session, load_problems, sanitize_for_isabelle
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


# ── Objective DONE check + evaluation polling (parity with the I/Q runner) ──

_DONE_SORRY_RE = re.compile(r"\b(sorry|oops)\b")

# Max times an attempt is sent back when the DONE gate rejects its claim
# (parity with the I/Q runner). After that the DONE is accepted and the
# arbiter judges the file as-is.
DONE_NUDGE_LIMIT = 2

# How long to wait for an in-flight evaluation to settle at DONE time.
DONE_SETTLE_TIMEOUT_S = 60.0
DONE_SETTLE_POLL_S = 3.0

# Setup warmup budget: the first evaluation of the seeded file may need to
# process heavy imports (I/Q's document is likewise processed in setup).
SETUP_EVAL_TIMEOUT_S = 300.0
SETUP_EVAL_POLL_S = 3.0


def parse_evaluation_snapshot(text: str) -> tuple[bool, bool]:
    """(settled, has_errors) from an isabelle-mcp evaluation snapshot.

    Snapshots are plain text: per-file "clean", "<file>: in progress", or
    indented rows like "  errors: 3-5", "  running: 9", "  pending: 2".
    """
    settled = True
    has_errors = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("errors:"):
            has_errors = True
        elif s.startswith(("running:", "pending:")) or ": in progress" in s:
            settled = False
    return settled, has_errors


async def wait_evaluation_settled(session, timeout: float, budget_s: float, poll_s: float) -> tuple[bool, str]:
    """Poll isabelle_evaluation_status until the evaluation settles or the
    budget is exhausted. Returns (settled, last_snapshot)."""
    deadline = time_mod.monotonic() + budget_s
    last = ""
    while True:
        out = await asyncio.wait_for(
            call_tool(session, "isabelle_evaluation_status", {}), timeout=timeout)
        last = out
        if not isinstance(out, str) or out.startswith("MCP tool error"):
            return True, out  # unverifiable — treat as settled, arbiter judges
        settled, _ = parse_evaluation_snapshot(out)
        if settled:
            return True, out
        if time_mod.monotonic() >= deadline:
            return False, out
        await asyncio.sleep(poll_s)


def _agent_host_file(cfg: Config, problem: Problem, host_thy_path: Path) -> Path:
    """The host-side file the agent's write_thy actually edits (mirrors the
    final-artifact source selection: the container bind-mount copy when
    container mode is active, else the work file)."""
    if cfg.isabelle_mcp_container.host_work_dir:
        mirrored = cfg.isabelle_mcp_container.host_work_dir / f"{problem.name}.thy"
        if mirrored.exists():
            return mirrored
    return host_thy_path


async def check_done_readiness(session, host_file: Path, problem: Problem, timeout: float) -> tuple[bool, str]:
    """Objective DONE check (parity with the I/Q runner's DONE gate).

    write_thy is harness-local, so the host file is authoritative for the
    static checks; the live evaluation snapshot must be settled and clean.
    """
    try:
        text = host_file.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        if _DONE_SORRY_RE.search(text):
            return False, "the file still contains sorry/oops"
        if f"theorem {problem.theorem_name}" not in text:
            return False, f"target theorem {problem.theorem_name} not found in the file"
    settled, snap = await wait_evaluation_settled(
        session, timeout, DONE_SETTLE_TIMEOUT_S, DONE_SETTLE_POLL_S)
    if not settled:
        return False, ("evaluation still shows running/pending commands after "
                       f"{DONE_SETTLE_TIMEOUT_S:.0f}s — running lines are NEVER "
                       "'background processing'")
    _, has_errors = parse_evaluation_snapshot(snap)
    if has_errors:
        return False, "the evaluation reports errors — errors are NEVER 'tooling artifacts'; fix them"
    return True, ""


async def run_attempt(problem: Problem, repeat: int, results_path: Path, prompt_name: str = "general") -> None:
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
            f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
            f"Discharge every `sorry` in {thy_path_str} — replace the `sorry` "
            f"keyword with a complete proof block.\n\n"
            f"WORKFLOW (follow this loop):\n"
            f"1. EDIT by calling write_thy(path, content) — it rewrites the WHOLE "
            f"file, so always pass the full theory text with your changes.\n"
            f"2. VERIFY by calling isabelle_evaluate_to(file_path=..., line=-1) — "
            f"this STARTS evaluation through the end of the file.  Evaluation is "
            f"ASYNCHRONOUS: afterwards call isabelle_evaluation_status() and, if it "
            f"still shows 'in progress' or 'running', keep polling it until the file "
            f"is reported clean or errors appear.\n"
            f"3. If errors are reported, read the failing command's message with "
            f"isabelle_command_output(file_path=..., line=<error line>) and fix that "
            f"line.  Inspect the open goal with isabelle_goal; search for lemmas "
            f"with isabelle_find_theorems.\n\n"
            f"SOLVER RULE (read first, it overrides your habits): NEVER use external "
            f"solvers (smt, metis, cvc5, vampire, eprover, z3, spass, verit, "
            f"zipperposition) directly in your proof text.  When "
            f"simp/auto/blast/force/linarith/presburger cannot close a goal, write "
            f"the single command `sledgehammer` at that goal, evaluate the file, "
            f"and read its suggestion via isabelle_command_output at that line.  "
            f"Then REMOVE the `sledgehammer` command and write the suggested proof "
            f"method instead.  If sledgehammer finds nothing, change strategy.\n"
            f"ESCALATION — after the SAME goal has failed twice, or an evaluation "
            f"keeps running on it, you MUST try sledgehammer before another manual "
            f"method.  If an evaluation gets STUCK (a command keeps running), cancel "
            f"it promptly with isabelle_cancel_evaluation, fix that command, and "
            f"re-evaluate — a stuck command burns CPU and blocks everything.\n\n"
            f"The theorem is proved ONLY when a full evaluation of the file reports "
            f"ZERO errors (file clean) and the file contains no sorry/oops.  When "
            f"your latest evaluation already shows this, reply with the single word "
            f"DONE immediately — no summary, no further confirmation calls are "
            f"required.  Running commands are "
            f"NEVER 'background processing' and errors are NEVER 'tooling "
            f"artifacts' — DONE is checked and rejected otherwise.\n\n"
            f"IMPORTANT: write ALL non-ASCII mathematical symbols using Isabelle's "
            f"\\<name> escape notation (\\<forall>, \\<exists>, \\<and>, \\<or>, "
            f"\\<Rightarrow>, \\<le>, \\<in>, ...) — avoid raw Unicode characters.\n\n"
            f"Note: the Isabelle session is ALREADY launched for you — never call "
            f"isabelle_launch or isabelle_terminate.\n\n"
            f"Theory: {problem.name}\nImports: {problem.imports}\n"
            f"Target theorem:\n{problem.statement}\n"
        )},
    ]

    logger = SessionLogger("isabelle_mcp", problem.name, repeat, cfg.paths.runs_dir)
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
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
    attempt_t0 = time_mod.time()  # setup_s reference: attempt start → timer start
    tokens = TokenAggregator()
    tool_times: list[float] = []
    round_latencies: list[float] = []
    final_thy_path = cfg.paths.runs_dir / "isabelle_mcp" / f"{problem.name}_rep{repeat}.thy"

    try:
        async with mcp_session_startup_retry(
            cfg.mcp_servers["isabelle_mcp"],
            on_retry=lambda n: logger.log_text(
                "SETUP retry", f"MCP session startup failed (attempt {n}); retrying in 3s"),
        ) as session:
            mcp_tools = await list_tools(session)
            # Guided variant: forward the vendor instructions the server shipped
            # in the MCP initialize handshake (Isabelle-MCP's instructions.py).
            if prompt_name == "guided":
                vendor = getattr(session, "vendor_instructions", None)
                if vendor:
                    # NOTE: messages[0] was already logged above (before the
                    # session existed), so the transcript's first message does
                    # NOT show this text — it IS sent to the model. Re-log the
                    # final message so the transcript contains the guidance too.
                    messages[0]["content"] += (
                        "\n\nADDITIONAL REFERENCE — vendor instructions served by "
                        "this MCP server:\n\n" + vendor)
                    logger.log_text(
                        "GUIDED_PROMPT",
                        "vendor instructions appended — model-visible first "
                        "user message re-logged below:")
                    logger.log_message(messages[0])
                else:
                    logger.log_text(
                        "NOTE", "guided prompt requested but the server served no instructions")
            # imports[0] is a THEORY (e.g. Complex_Main), not a session name —
            # derive the owning session as the arbiter does (audit H6).
            await call_tool(session, "isabelle_launch", {"session": derive_session(problem.imports)})

            # Setup warmup (parity with the other runners): process the seeded
            # work file to completion BEFORE the timer starts, so import
            # processing doesn't leak into the timed region.
            setup_timeout = cfg.budgets.tool_timeout_seconds
            await asyncio.wait_for(
                call_tool(session, "isabelle_evaluate_to", {"file_path": thy_path_str, "line": -1}),
                timeout=setup_timeout,
            )
            _, snap = await wait_evaluation_settled(
                session, setup_timeout, SETUP_EVAL_TIMEOUT_S, SETUP_EVAL_POLL_S)
            logger.log_text("SETUP evaluate(work file)", snap[:500])

            result.setup_s = round(time_mod.time() - attempt_t0, 2)
            timer.start()
            round_start: float = 0.0
            nudges_used = 0
            done_nudges_used = 0
            for _round in range(cfg.budgets.max_rounds):
                # Enforce per-problem wall cap
                elapsed = timer.elapsed()
                if elapsed >= cfg.budgets.problem_wall_cap_seconds:
                    result.error = f"problem wall cap exceeded ({cfg.budgets.problem_wall_cap_seconds}s)"
                    break

                round_result = await client.chat(messages, tools=mcp_tools, system_prompt=system_prompt)
                tokens.add(round_result.usage)
                result.rounds += 1
                if round_result.finish_reason == "length":
                    result.n_truncated_rounds += 1
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
                if round_result.reasoning_text:
                    logger.log_text("REASONING", round_result.reasoning_text[:2000])

                if not round_result.tool_calls:
                    action, payload = no_tool_call_action(round_result, nudges_used)
                    if action == "done":
                        # Objective DONE check (parity with the I/Q runner's DONE
                        # gate): a false DONE costs a nudge round, not the attempt.
                        if done_nudges_used < DONE_NUDGE_LIMIT:
                            ready, reason = await check_done_readiness(
                                session, _agent_host_file(cfg, problem, host_thy_path),
                                problem, cfg.budgets.tool_timeout_seconds)
                            if not ready:
                                done_nudges_used += 1
                                result.n_nudge_rounds += 1
                                payload = (f"[DONE not accepted: {reason}. Fix this and "
                                           f"re-verify with isabelle_evaluate_to + "
                                           f"isabelle_evaluation_status (the file must be "
                                           f"clean, settled, and sorry-free), then reply DONE.]")
                                text = (round_result.assistant_text or "").strip()
                                if text:
                                    messages.append({"role": "assistant", "content": text})
                                messages.append({"role": "user", "content": payload})
                                logger.log_message({"role": "user", "content": payload})
                                continue
                        result.agent_claimed_solved = True
                        break
                    if action == "stop":
                        result.error = payload
                        break
                    nudges_used += 1
                    result.n_nudge_rounds += 1
                    text = (round_result.assistant_text or "").strip()
                    if text:
                        messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": payload})
                    logger.log_message({"role": "user", "content": payload})
                    continue

                tool_outputs = []
                for tc in round_result.tool_calls:
                    result.n_tool_calls += 1
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        # Keep history consistent: every tool_call needs a tool reply (H3).
                        err = (f"ERROR: tool arguments were not valid JSON ({e}). "
                               f"Re-issue the call with complete, valid JSON.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": err})
                        logger.log_tool_result(tc.id, name, err)
                        continue
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
            result.first_tool_s = round(tool_times[0], 2) if tool_times else None
            result.round_latencies = round_latencies

            # Copy final file as artifact. Prefer the mirrored/container copy if it exists.
            source = _agent_host_file(cfg, problem, host_thy_path)
            shutil.copy(source, final_thy_path)
            result.final_thy_path = str(final_thy_path)
            # Release the Isabelle session inside the server (best-effort; the
            # per-attempt MCP process exits anyway, but the launched Isabelle
            # session may linger in container mode).
            try:
                await call_tool(session, "isabelle_terminate", {})
            except Exception:
                pass
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
        result.first_tool_s = round(tool_times[0], 2) if tool_times else None
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
    parser.add_argument("--prompt", choices=["general", "guided"], default="general",
                        help="general = minimal harness prompt; guided = also forward "
                             "the server's vendor instructions from the MCP initialize "
                             "handshake")
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
                await run_attempt(problem, repeat, results_path, prompt_name=args.prompt)
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