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
THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")
IMPORTS_RE = re.compile(r"(?s)\bimports\b(.*?)\bbegin\b")
IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')


@dataclass
class StepResult:
    step_kind: str
    preview: str
    accepted: bool
    elapsed_sec: float
    proof_done: Optional[bool]
    error: Optional[str] = None


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    startup_sec: float
    wall_time_sec: float
    ok: bool
    total_steps: int
    accepted_steps: int
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


def split_theory(text: str) -> tuple[list[str], str]:
    stripped = text.strip()
    header_match = HEADER_RE.search(stripped)
    end_match = END_RE.search(stripped)
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    body = stripped[header_match.end():end_match.start()].strip()
    return split_top_level_blocks(body), stripped[end_match.start():end_match.end()].strip()


def extract_imports(text: str) -> list[str]:
    m = IMPORTS_RE.search(text)
    if not m:
        return ["Main"]
    out: list[str] = []
    for token in IMPORT_TOKEN_RE.findall(m.group(1)):
        token = token.strip().strip('"')
        if token and token not in {"imports", "begin", "theory", "keywords"}:
            out.append(token)
    return sorted(set(out)) or ["Main"]


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
        description="Benchmark small-step verification via QIsabelle."
    )
    ap.add_argument("--qisabelle-root", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--session-name", default="HOL-Analysis")
    ap.add_argument("--output", type=Path, default=Path("smallstep_qisabelle_results.json"))
    args = ap.parse_args()

    sys.path.insert(0, str(args.qisabelle_root))
    from client.session import QIsabelleSession

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    results: list[TheoryResult] = []

    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    shared_startup_t0 = time.perf_counter()
    with QIsabelleSession(session_name=args.session_name, session_roots=[]) as session:
        shared_session_startup_sec = time.perf_counter() - shared_startup_t0

        for thy_file in files:
            text = thy_file.read_text(encoding="utf-8")
            theory_name = determine_theory_name(thy_file, text)
            imports = extract_imports(text)
            blocks, end_kw = split_theory(text)

            theory_wall_t0 = time.perf_counter()
            step_results: list[StepResult] = []
            ok = True

            t0 = time.perf_counter()
            try:
                session.new_theory(
                    theory_name=theory_name,
                    new_state_name=f"{theory_name}_0",
                    imports=imports,
                    only_import_from_session_heap=False,
                )
                current_state = f"{theory_name}_0"
                next_idx = 1
                startup = time.perf_counter() - t0

                for kind, command in [("body", b + "\n") for b in blocks] + [("end", end_kw)]:
                    next_state = f"{theory_name}_{next_idx}"
                    next_idx += 1
                    t1 = time.perf_counter()
                    try:
                        proof_done, goals = session.execute(current_state, command, next_state)
                        elapsed = time.perf_counter() - t1
                        accepted = True
                        err = None
                        current_state = next_state
                    except Exception as exc:
                        elapsed = time.perf_counter() - t1
                        proof_done, goals = None, []
                        accepted = False
                        err = str(exc)

                    step_results.append(
                        StepResult(kind, preview(command), accepted, elapsed, proof_done, err)
                    )
                    if not accepted:
                        ok = False
                        break
            except Exception as exc:
                startup = time.perf_counter() - t0
                ok = False
                step_results.append(
                    StepResult("startup", preview(theory_name), False, 0.0, None, str(exc))
                )

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
        "tool": "qisabelle",
        "benchmark_kind": "local_smallstep_verification_benchmark",
        "corpus": str(args.corpus),
        "qisabelle_root": str(args.qisabelle_root),
        "session_name": args.session_name,
        "shared_session_startup_sec": shared_session_startup_sec,
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
    print(json.dumps({k: v for k, v in payload.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
