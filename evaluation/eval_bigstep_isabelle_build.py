#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from eval_stats import safe_mean, safe_median, summarize_metric
from theory_splitter import determine_theory_name

try:
    import resource  # Unix-only
except ImportError:
    resource = None


@dataclass
class FileResult:
    file: str
    theory_name: Optional[str]
    ok: bool
    wall_time_sec: float
    child_cpu_user_sec: Optional[float]
    child_cpu_system_sec: Optional[float]
    child_cpu_total_sec: Optional[float]
    cpu_wall_ratio: Optional[float]
    return_code: Optional[int]
    error: Optional[str]
    stdout_tail: Optional[str]
    stderr_tail: Optional[str]


def get_children_rusage_snapshot() -> Optional[tuple[float, float]]:
    """
    Return cumulative (user_cpu_sec, system_cpu_sec) for child processes.
    Only available on Unix-like platforms with the `resource` module.
    """
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (float(usage.ru_utime), float(usage.ru_stime))


def diff_rusage(
    before: Optional[tuple[float, float]],
    after: Optional[tuple[float, float]],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if before is None or after is None:
        return None, None, None
    user = max(0.0, after[0] - before[0])
    system = max(0.0, after[1] - before[1])
    return user, system, user + system


def build_one(
    thy_file: Path,
    isabelle_bin: str,
    parent_session: str,
    jobs: int,
) -> FileResult:
    theory_text = thy_file.read_text(encoding="utf-8")
    theory_name = determine_theory_name(thy_file, theory_text)

    if not theory_name:
        return FileResult(
            file=str(thy_file),
            theory_name=None,
            ok=False,
            wall_time_sec=0.0,
            child_cpu_user_sec=None,
            child_cpu_system_sec=None,
            child_cpu_total_sec=None,
            cpu_wall_ratio=None,
            return_code=None,
            error="Could not determine theory name",
            stdout_tail=None,
            stderr_tail=None,
        )

    safe_session_theory = re.sub(r"[^A-Za-z0-9_]", "_", theory_name)
    session_name = f"Bench_{safe_session_theory}_{abs(hash(str(thy_file))) % 10**8}"

    with tempfile.TemporaryDirectory(prefix="isabelle-build-bench-") as td:
        temp_dir = Path(td)

        # Write theory text into the temporary session directory.
        (temp_dir / thy_file.name).write_text(theory_text, encoding="utf-8")

        root_text = f'''session "{session_name}" = "{parent_session}" +
  options [document = false, browser_info = false]
  theories
    "{theory_name}"
'''
        (temp_dir / "ROOT").write_text(root_text, encoding="utf-8")

        cmd = [
            isabelle_bin,
            "build",
            "-D",
            str(temp_dir),
            "-j",
            str(jobs),
            session_name,
        ]

        child_before = get_children_rusage_snapshot()
        wall_t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        wall_elapsed = time.perf_counter() - wall_t0
        child_after = get_children_rusage_snapshot()

        child_user, child_system, child_total = diff_rusage(child_before, child_after)
        cpu_wall_ratio = (
            (child_total / wall_elapsed)
            if (child_total is not None and wall_elapsed > 0.0)
            else None
        )

        return FileResult(
            file=str(thy_file),
            theory_name=theory_name,
            ok=(proc.returncode == 0),
            wall_time_sec=wall_elapsed,
            child_cpu_user_sec=child_user,
            child_cpu_system_sec=child_system,
            child_cpu_total_sec=child_total,
            cpu_wall_ratio=cpu_wall_ratio,
            return_code=proc.returncode,
            error=None if proc.returncode == 0 else f"isabelle build failed with exit code {proc.returncode}",
            stdout_tail=proc.stdout[-4000:] if proc.stdout else None,
            stderr_tail=proc.stderr[-4000:] if proc.stderr else None,
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Benchmark whole-file Isabelle verification via `isabelle build`."
    )
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--isabelle-bin", default="isabelle")
    ap.add_argument("--parent-session", default="HOL-Analysis")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--output", type=Path, default=Path("build_bigstep_results.json"))
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    batch_child_before = get_children_rusage_snapshot()
    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    results = [asdict(build_one(f, args.isabelle_bin, args.parent_session, args.jobs)) for f in files]

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0
    batch_child_after = get_children_rusage_snapshot()
    total_child_cpu_user_sec, total_child_cpu_system_sec, total_child_cpu_time_sec = diff_rusage(
        batch_child_before, batch_child_after
    )

    wall_times = [r["wall_time_sec"] for r in results]
    child_cpu_totals = [r["child_cpu_total_sec"] for r in results if r["child_cpu_total_sec"] is not None]
    child_cpu_users = [r["child_cpu_user_sec"] for r in results if r["child_cpu_user_sec"] is not None]
    child_cpu_systems = [r["child_cpu_system_sec"] for r in results if r["child_cpu_system_sec"] is not None]
    cpu_wall_ratios = [r["cpu_wall_ratio"] for r in results if r["cpu_wall_ratio"] is not None]

    summary = {
        "tool": "isabelle-build",
        "benchmark_kind": "local_whole_file_verification_baseline",
        "corpus": str(args.corpus),
        "parent_session": args.parent_session,
        "jobs": args.jobs,
        "total_files": len(results),
        "successes": sum(1 for r in results if r["ok"]),
        "failures": sum(1 for r in results if not r["ok"]),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_child_cpu_user_sec": total_child_cpu_user_sec,
        "total_child_cpu_system_sec": total_child_cpu_system_sec,
        "total_child_cpu_time_sec": total_child_cpu_time_sec,
        "files_per_minute": (len(results) / total_wall_time_sec * 60.0) if total_wall_time_sec > 0 else None,
        "mean_wall_time_sec_per_file": safe_mean(wall_times),
        "median_wall_time_sec_per_file": safe_median(wall_times),
        "mean_child_cpu_time_sec_per_file": safe_mean(child_cpu_totals),
        "median_child_cpu_time_sec_per_file": safe_median(child_cpu_totals),
        "mean_child_cpu_wall_ratio_per_file": safe_mean(cpu_wall_ratios),
        "median_child_cpu_wall_ratio_per_file": safe_median(cpu_wall_ratios),
        "wall_time_stats_sec": summarize_metric(wall_times),
        "child_cpu_total_stats_sec": summarize_metric(child_cpu_totals),
        "child_cpu_user_stats_sec": summarize_metric(child_cpu_users),
        "child_cpu_system_stats_sec": summarize_metric(child_cpu_systems),
        "cpu_wall_ratio_stats": summarize_metric(cpu_wall_ratios),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "resource_module_available": resource is not None,
        },
        "results": results,
    }

    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Print a compact summary to stdout.
    compact = {k: v for k, v in summary.items() if k != "results"}
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
