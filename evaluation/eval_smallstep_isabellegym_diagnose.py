#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import platform
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")


TOP_LEVEL_KEYWORDS = (
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    "definition", "fun", "function", "primrec", "inductive", "inductive_set",
    "coinductive", "abbreviation", "notation", "no_notation", "declare",
    "context", "locale", "interpretation", "instantiation", "lift_definition",
    "datatype", "codatatype", "record", "typedef", "class", "instance",
    "text", "text_raw", "ML", "ML_file", "SML_export", "setup",
    "method_setup", "termination", "end",
)
PROOF_OPENERS = ("proof", "proof -", "proof (")
PROOF_CLOSERS = ("qed", "by", "done", "oops", "sorry")


@dataclass
class StepResult:
    step_kind: str
    command: str
    preview: str
    accepted: bool
    elapsed_sec: float
    open_subgoals: int
    subgoals: list[str] = field(default_factory=list)
    error: Optional[str] = None
    exception_class: Optional[str] = None
    step_index: int = -1


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    startup_sec: float
    wall_time_sec: float
    ok: bool
    total_steps: int
    accepted_steps: int
    expected_steps: int
    completed_all_commands: bool
    reached_end_command: bool
    last_recorded_step_kind: Optional[str]
    last_recorded_preview: Optional[str]
    startup_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def determine_theory_name(thy_file: Path, text: str) -> str:
    declared = extract_theory_name(text)
    return declared if declared is not None else thy_file.stem


def _starts_with_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    stripped = line.strip()
    return any(re.match(rf"^{re.escape(keyword)}(\b|\s|\(|$)", stripped) for keyword in keywords)


def split_top_level_blocks(body_text: str) -> list[str]:
    text = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    blocks: list[str] = []
    current: list[str] = []
    proof_depth = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        starts_new_block = bool(current) and proof_depth == 0 and stripped != "" and _starts_with_keyword(stripped, TOP_LEVEL_KEYWORDS)
        if starts_new_block:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
        current.append(line)
        if _starts_with_keyword(stripped, PROOF_OPENERS):
            proof_depth += 1
        elif _starts_with_keyword(stripped, PROOF_CLOSERS):
            proof_depth = max(0, proof_depth - 1)
    block = "\n".join(current).strip()
    if block:
        blocks.append(block)
    return blocks


def split_theory(text: str) -> tuple[str, str, str]:
    header_match = HEADER_RE.search(text)
    end_match = END_RE.search(text.strip())
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    stripped = text.strip()
    header = stripped[:header_match.end()].strip() + "\n"
    body = stripped[header_match.end():end_match.start()].strip()
    end_kw = stripped[end_match.start():end_match.end()].strip()
    return header, body, end_kw


def preview(text: str, n: int = 100) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def safe_mean(values: list[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def safe_median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)

    xs = sorted(values)
    rank = (len(xs) - 1) * (p / 100.0)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return xs[lo]
    weight = rank - lo
    return xs[lo] * (1.0 - weight) + xs[hi] * weight


def summarize_metric(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p90": None,
            "p95": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": safe_mean(values),
        "median": safe_median(values),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Diagnostic evaluator for local IsabelleGym. Records whether the loop reached final end and captures per-step exceptions."
    )
    ap.add_argument("--repo-root", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--output", type=Path, default=Path("smallstep_isabellegym_diagnostic_results.json"))
    ap.add_argument("--print-steps", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(args.repo_root))
    from evaluation.local_gym.isabelle_gym import IsabelleGym
    from server_gym.success_checker import get_error_message, is_syntax_successful

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    results: list[TheoryResult] = []
    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    for thy_file in files:
        text = thy_file.read_text(encoding="utf-8")
        theory_name = determine_theory_name(thy_file, text)
        header, body, end_kw = split_theory(text)
        blocks = split_top_level_blocks(body)
        command_stream = [("header", header)] + [("body", b + "\n") for b in blocks] + [("end", end_kw)]

        theory_wall_t0 = time.perf_counter()
        step_results: list[StepResult] = []
        gym = None
        startup = 0.0
        startup_error = None
        completed_all_commands = False
        reached_end_command = False

        try:
            t0 = time.perf_counter()
            gym = IsabelleGym(show_states=False)
            gym.enter_thy(theory_name)
            startup = time.perf_counter() - t0

            for idx, (kind, command) in enumerate(command_stream):
                if kind == "end":
                    reached_end_command = True
                t1 = time.perf_counter()
                try:
                    repl_result = gym.step(command)
                    elapsed = time.perf_counter() - t1
                    accepted = bool(is_syntax_successful(repl_result))
                    subgoals = list(gym.open_subgoals())
                    err = None if accepted else get_error_message(repl_result)
                    exc_cls = None
                except Exception as exc:
                    elapsed = time.perf_counter() - t1
                    accepted = False
                    subgoals = list(gym.open_subgoals()) if gym is not None else []
                    err = str(exc)
                    exc_cls = exc.__class__.__name__

                step_results.append(
                    StepResult(
                        step_kind=kind,
                        command=command.rstrip("\n"),
                        preview=preview(command),
                        accepted=accepted,
                        elapsed_sec=elapsed,
                        open_subgoals=len(subgoals),
                        subgoals=subgoals,
                        error=err,
                        exception_class=exc_cls,
                        step_index=idx,
                    )
                )

                if args.print_steps:
                    print(f"[{theory_name}] step={idx} kind={kind} accepted={accepted} preview={preview(command)}")
                    if err:
                        print(f"  error={err.splitlines()[0]}")
                    if subgoals:
                        print(f"  open_subgoals={len(subgoals)}")

                if not accepted:
                    break
            else:
                completed_all_commands = True

        except Exception as exc:
            startup_error = str(exc)
            if not step_results:
                step_results.append(
                    StepResult(
                        step_kind="startup",
                        command=theory_name,
                        preview=preview(theory_name),
                        accepted=False,
                        elapsed_sec=0.0,
                        open_subgoals=0,
                        subgoals=[],
                        error=str(exc),
                        exception_class=exc.__class__.__name__,
                        step_index=-1,
                    )
                )
        finally:
            try:
                if gym is not None:
                    gym.close()
            except Exception:
                pass

        ok = completed_all_commands and all(s.accepted for s in step_results)
        last = step_results[-1] if step_results else None
        theory_wall = time.perf_counter() - theory_wall_t0

        results.append(
            TheoryResult(
                file=str(thy_file),
                theory_name=theory_name,
                startup_sec=startup,
                wall_time_sec=theory_wall,
                ok=ok,
                total_steps=len(step_results),
                accepted_steps=sum(1 for s in step_results if s.accepted),
                expected_steps=len(command_stream),
                completed_all_commands=completed_all_commands,
                reached_end_command=reached_end_command,
                last_recorded_step_kind=(last.step_kind if last else None),
                last_recorded_preview=(last.preview if last else None),
                startup_error=startup_error,
                steps=step_results,
            )
        )

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0

    startup_times = [r.startup_sec for r in results]
    theory_wall_times = [r.wall_time_sec for r in results]
    step_elapsed_times = [s.elapsed_sec for r in results for s in r.steps]
    steps_per_theory = [float(r.total_steps) for r in results]
    accepted_steps_per_theory = [float(r.accepted_steps) for r in results]

    payload = {
        "tool": "local-isabellegym",
        "benchmark_kind": "local_smallstep_verification_benchmark_diagnostic",
        "corpus": str(args.corpus),
        "repo_root": str(args.repo_root),
        "total_files": len(results),
        "successes": sum(1 for r in results if r.ok),
        "failures": sum(1 for r in results if not r.ok),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_startup_time_sec": sum(startup_times),
        "total_theory_wall_time_sec": sum(theory_wall_times),
        "total_step_elapsed_time_sec": sum(step_elapsed_times),
        "files_per_minute": (len(results) / total_wall_time_sec * 60.0) if total_wall_time_sec > 0 else None,
        "mean_wall_time_sec_per_file": safe_mean(theory_wall_times),
        "median_wall_time_sec_per_file": safe_median(theory_wall_times),
        "mean_startup_sec_per_file": safe_mean(startup_times),
        "median_startup_sec_per_file": safe_median(startup_times),
        "mean_step_elapsed_sec": safe_mean(step_elapsed_times),
        "median_step_elapsed_sec": safe_median(step_elapsed_times),
        "startup_time_stats_sec": summarize_metric(startup_times),
        "theory_wall_time_stats_sec": summarize_metric(theory_wall_times),
        "step_elapsed_time_stats_sec": summarize_metric(step_elapsed_times),
        "steps_per_theory_stats": summarize_metric(steps_per_theory),
        "accepted_steps_per_theory_stats": summarize_metric(accepted_steps_per_theory),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "results": [
            {
                **asdict(r),
                "steps": [asdict(s) for s in r.steps],
            }
            for r in results
        ],
    }
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    suspicious = [
        r.theory_name for r in results
        if (not r.ok) and r.steps and all(s.accepted for s in r.steps)
    ]
    print(json.dumps({
        "total_files": payload["total_files"],
        "successes": payload["successes"],
        "failures": payload["failures"],
        "suspicious_ok_false_all_recorded_steps_accepted": suspicious,
    }, indent=2))


if __name__ == "__main__":
    main()
