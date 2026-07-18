#!/usr/bin/env python3
"""AutoCorrode I/Q comparison runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time as time_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient, no_tool_call_action
from common.problems import load_problems, sanitize_for_isabelle
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
        f"Note: I/R is not installed, do not use it.\n"
        f"Note: the MCP session is ALREADY authenticated for you — never call authenticate.\n\n"
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
        f"Note: I/R is not installed, do not use it.\n"
        f"Note: the MCP session is ALREADY authenticated for you — never call authenticate.\n\n"
    )


_PROMPTS = {
    "general":    _general_prompt_body,
    "restrictive": _restrictive_prompt_body,
}

# ── I/Q auth token + setup helpers ──────────────────────────────────────
#
# The I/Q plugin MINTS ITS OWN token (a short-lived JWT shown in the jEdit I/Q
# panel) — there is no shared secret, and the token changes between jEdit/I/Q
# launches. Priority: IQ_AUTH_TOKEN env > IQ_AUTH_TOKEN_FILE env >
# MCP-comparison/iq_token.txt. Resolved PER ATTEMPT so you can paste a fresh
# token into the file while a batch is running.

_TOKEN_FILE_DEFAULT = Path(__file__).resolve().parent / "iq_token.txt"


def resolve_iq_token() -> tuple[str | None, str]:
    """Return (token, source-description); token is None if nothing is configured."""
    env_tok = os.environ.get("IQ_AUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok, "env IQ_AUTH_TOKEN"
    candidates = []
    file_env = os.environ.get("IQ_AUTH_TOKEN_FILE", "").strip()
    if file_env:
        candidates.append(Path(file_env))
    candidates.append(_TOKEN_FILE_DEFAULT)
    for p in candidates:
        try:
            if p.is_file():
                tok = p.read_text(encoding="utf-8").strip()
                if tok:
                    return tok, str(p)
        except OSError:
            continue
    return None, "not configured"


def tool_output_failed(output: str) -> bool:
    """True if a call_tool() return string reports an error (call_tool never raises)."""
    return output.startswith("MCP tool error") or "McpError" in output


_LINE_PREFIX_RE = re.compile(r"^[ \t]*\d+:", re.MULTILINE)


async def setup_call(session, name: str, args: dict, timeout: float) -> str:
    """call_tool with a hard timeout for SETUP-phase calls.

    The agent loop wraps its tool calls in wait_for(tool_timeout), but setup
    calls had no bound: one buffer-reset write hung for the bridge's internal
    7200 s Isabelle-server timeout — twice — wasting 4 h before failing.
    """
    try:
        return await asyncio.wait_for(call_tool(session, name, args), timeout=timeout)
    except asyncio.TimeoutError:
        return f"MCP tool error ({name}): setup call timed out after {timeout:.0f}s"


async def read_iq_buffer(session, path: str, timeout: float) -> tuple[str, int]:
    """Return (buffer text, line count) from I/Q's in-memory view of `path`.

    read_file mode=Line returns numbered lines ('  1:theory ...'); strip the
    prefixes. This is the BUFFER, not the disk file — the distinction is the
    whole point of the reset logic below.
    """
    raw = await setup_call(session, "read_file", {"path": path, "mode": "Line"}, timeout)
    if tool_output_failed(raw):
        raise RuntimeError(f"I/Q read_file failed during setup: {raw.strip()}")
    data = json.loads(raw)
    content = data.get("content", "") if isinstance(data, dict) else str(data)
    return _LINE_PREFIX_RE.sub("", content), len(content.splitlines())


async def reset_iq_buffer(session, path: str, fresh_text: str, logger, timeout: float) -> None:
    """Force I/Q's in-memory buffer to hold `fresh_text`.

    I/Q's write_file implements ONLY line/str_replace/insert — the previous
    reset used a nonexistent 'write' command and silently no-oped for every
    run (server replied 'command write not implemented'), which is how a
    finished proof survived in the buffer between attempts. Here: if the
    buffer differs, replace its whole line range via command='line'.
    Retries cover the 'not opened in jEdit' race right after open_file.
    """
    last_err = ""
    for attempt in range(3):
        buffer_text, n_lines = await read_iq_buffer(session, path, timeout)
        if buffer_text.strip() == fresh_text.strip():
            logger.log_text("SETUP buffer_reset", f"buffer already fresh ({n_lines} lines)")
            return
        out = await setup_call(session, "write_file", {
            "path": path,
            "command": "line",
            "start_line": 1,
            "end_line": max(1, n_lines),
            "new_str": fresh_text,
        }, timeout)
        logger.log_text("SETUP buffer_reset(line-replace)", out[:500])
        if not tool_output_failed(out):
            return
        last_err = out.strip()
        await asyncio.sleep(1.0)  # 'not opened in jEdit' race — let the buffer model attach
    raise RuntimeError(f"I/Q buffer reset failed after 3 tries: {last_err}")


_TOKEN_HELP = (
    "The I/Q plugin displays its current token in the jEdit I/Q panel; it is a "
    "short-lived JWT that changes between launches. Copy it and either "
    "`export IQ_AUTH_TOKEN=<token>` or paste it into "
    f"{_TOKEN_FILE_DEFAULT} (re-read every attempt, no restart needed)."
)

# ═════════════════════════════════════════════════════════════════════════

async def run_attempt(problem, repeat: int, results_path: Path, prompt_name: str = "general") -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt

    # I/Q reads/writes files inside allowed roots; place the starting file there.
    work_dir = Path(os.environ.get("IQ_MCP_ALLOWED_ROOTS", cfg.paths.runs_dir / "autocorrode" / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    thy_path = work_dir / f"{problem.name}.thy"
    # Write the problem to disk ONLY if the file doesn't exist yet. jEdit keeps
    # an open buffer for this path across attempts; deleting/rewriting the file
    # on disk behind its back triggers the modal "file has been modified on
    # disk by another program" dialog, which blocks jEdit's event thread and
    # stalls every I/Q call. The buffer reset below (through I/Q itself) is the
    # authoritative refresh and keeps buffer and disk in sync.
    if not thy_path.exists():
        thy_path.write_text(problem.full_text, encoding="utf-8")

    token, token_source = resolve_iq_token()
    body_fn = _PROMPTS.get(prompt_name, _general_prompt_body)
    # The prompt bodies already open with the expert-role sentence, and the
    # harness authenticates itself below — never route the token through the
    # model (one run was lost to the model mutating a hex digit of it).
    content = body_fn(thy_path)
    messages = [{"role": "user", "content": content}]

    logger = SessionLogger("autocorrode", problem.name, repeat, cfg.paths.runs_dir)
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
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

            # ── fail-fast setup ─────────────────────────────────────────
            # call_tool() never raises; it returns error STRINGS. Previously all
            # three setup results were ignored, so an expired/wrong token meant
            # the buffer reset silently failed and the agent then "discovered"
            # the PREVIOUS attempt's finished proof in IQ's in-memory buffer and
            # claimed DONE in seconds (phantom solve — see the 1-2/rep1 run).
            if token is None:
                raise RuntimeError(f"No I/Q auth token configured. {_TOKEN_HELP}")
            logger.log_text("IQ_TOKEN_SOURCE", token_source)
            setup_timeout = cfg.budgets.tool_timeout_seconds
            out = await setup_call(session, "authenticate", {"token": token}, setup_timeout)
            logger.log_text("SETUP authenticate", out)
            if tool_output_failed(out):
                raise RuntimeError(
                    f"I/Q authentication failed (token from {token_source}): "
                    f"{out.strip()}\n{_TOKEN_HELP}")
            out = await setup_call(session, "open_file", {"path": str(thy_path.resolve())}, setup_timeout)
            logger.log_text("SETUP open_file", out)
            if tool_output_failed(out):
                raise RuntimeError(f"I/Q open_file failed: {out.strip()}")
            # Force IQ's in-memory buffer to match the fresh file on disk —
            # open_file reuses an existing buffer (which may still hold the
            # previous attempt's finished proof). See reset_iq_buffer.
            await reset_iq_buffer(session, str(thy_path.resolve()), problem.full_text, logger, setup_timeout)
            # Verify the buffer REALLY holds the fresh problem: it must contain
            # the original `sorry`. count=0 before the agent has done anything
            # means the reset didn't take (stale buffer from a previous attempt).
            out = await setup_call(session, "get_sorry_positions", {"path": str(thy_path.resolve())}, setup_timeout)
            logger.log_text("SETUP get_sorry_positions", out)
            try:
                sorry_count = int(json.loads(out).get("count", -1))
            except (ValueError, TypeError, json.JSONDecodeError):
                sorry_count = -1
            if sorry_count == 0:
                raise RuntimeError(
                    "I/Q buffer reset verification failed: work file reports 0 sorries "
                    "BEFORE the attempt started — IQ's buffer still holds a previous "
                    "attempt's proof. Aborting to avoid a phantom solve.")

            timer.start()
            round_start: float = 0.0
            nudges_used = 0
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
                        result.agent_claimed_solved = True
                        break
                    if action == "stop":
                        result.error = payload
                        break
                    nudges_used += 1
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
                    if name == "authenticate":
                        # The harness authenticates the connection at setup; the model
                        # does not know the token (deliberately — see token-mutation
                        # incident) and its guessed tokens fail. Short-circuit instead
                        # of forwarding, so no rounds are wasted on auth errors.
                        output = ("Already authenticated by the harness — you do not need to "
                                  "call authenticate. Proceed with the other tools.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": output})
                        logger.log_tool_result(tc.id, name, output)
                        continue
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        # Keep history consistent: every tool_call needs a tool reply (H3).
                        err = (f"ERROR: tool arguments were not valid JSON ({e}). "
                               f"Re-issue the call with complete, valid JSON.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": err})
                        logger.log_tool_result(tc.id, name, err)
                        continue
                    # Sanitize string args — DeepSeek may emit lone surrogates.
                    # The filter only strips invalid UTF-16 halves; legitimate
                    # \<name> escapes pass through untouched.
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)
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

        # Deliberately DO NOT delete the work file: jEdit's open buffer would
        # detect the disk change and pop the modal "file modified on disk"
        # dialog, blocking I/Q on the next attempt. The next attempt's buffer
        # reset (via I/Q) restores the fresh problem text instead.
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

    token, token_source = resolve_iq_token()
    if token is None:
        print(f"WARNING: no I/Q auth token configured — every attempt will abort at setup.\n{_TOKEN_HELP}")
    else:
        print(f"I/Q auth token: {token_source} (re-read each attempt)")

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