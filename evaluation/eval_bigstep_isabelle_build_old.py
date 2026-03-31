#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class FileResult:
    file: str
    theory_name: Optional[str]
    ok: bool
    wall_time_sec: float
    return_code: Optional[int]
    error: Optional[str]
    stdout_tail: Optional[str]
    stderr_tail: Optional[str]


# Only match an actual theory command at the start of a line, e.g.
#   theory Infinite_Sum
#   theory "Cross3"
THEORY_RE = re.compile(
    r'(?m)^[ \t]*theory[ \t]+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))\b'
)

def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)

def determine_theory_name(thy_file: Path, text: str) -> str:
    """
    Prefer the declared theory name if we can find a real theory command.
    Otherwise fall back to the file stem, which is what Isabelle expects
    for loading <Name>.thy from the theories section of ROOT.
    """
    declared = extract_theory_name(text)
    return declared if declared is not None else thy_file.stem


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
            return_code=None,
            error="Could not determine theory name",
            stdout_tail=None,
            stderr_tail=None,
        )

    safe_session_theory = re.sub(r"[^A-Za-z0-9_]", "_", theory_name)
    session_name = f"Bench_{safe_session_theory}_{abs(hash(str(thy_file))) % 10**8}"

    with tempfile.TemporaryDirectory(prefix="isabelle-build-bench-") as td:
        temp_dir = Path(td)

        # write sanitized theory text instead of copying original file verbatim
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

        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.perf_counter() - t0

        return FileResult(
            file=str(thy_file),
            theory_name=theory_name,
            ok=(proc.returncode == 0),
            wall_time_sec=elapsed,
            return_code=proc.returncode,
            error=None if proc.returncode == 0 else f"isabelle build failed with exit code {proc.returncode}",
            stdout_tail=proc.stdout[-4000:] if proc.stdout else None,
            stderr_tail=proc.stderr[-4000:] if proc.stderr else None,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--isabelle-bin", default="isabelle")
    ap.add_argument("--parent-session", default="HOL-Analysis")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--output", type=Path, default=Path("build_bigstep_results.json"))
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    t0 = time.perf_counter()
    results = [asdict(build_one(f, args.isabelle_bin, args.parent_session, args.jobs)) for f in files]
    total_elapsed = time.perf_counter() - t0

    summary = {
        "tool": "isabelle-build",
        "corpus": str(args.corpus),
        "parent_session": args.parent_session,
        "jobs": args.jobs,
        "total_files": len(results),
        "successes": sum(1 for r in results if r["ok"]),
        "failures": sum(1 for r in results if not r["ok"]),
        "total_wall_time_sec": total_elapsed,
        "results": results,
    }

    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()