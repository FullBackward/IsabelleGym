#!/usr/bin/env python3
"""Read MCP-comparison/runs/**/results.jsonl and print summary tables.

Results may live directly under runs/<system>/ or in per-experiment subfolders
(e.g. runs/isabellegym/restrictive-prompt/results.jsonl) — both are found.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

sys = __import__("sys")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.metrics import load_results

SYSTEMS = ["isabellegym", "isabelle_mcp", "autocorrode"]


def _fmt(value: float | None, spec: str) -> str:
    return format(value, spec) if value is not None else "-"


def summarize(runs_dir: Path) -> None:
    rows = []
    for system in SYSTEMS:
        system_dir = runs_dir / system
        if not system_dir.is_dir():
            continue
        for path in sorted(system_dir.rglob("results.jsonl")):
            rows.extend(load_results(path))

    if not rows:
        print(f"No results found under {runs_dir}")
        return

    by_system: dict[str, list] = defaultdict(list)
    for r in rows:
        by_system[r.system].append(r)

    print("| System | attempts | pass@1 | solved | mean rounds (solved) | mean wall_s (solved) | mean total tok (solved) | truncated rounds |")
    print("|---|---|---|---|---|---|---|---|")
    for system in SYSTEMS:
        rs = by_system.get(system, [])
        attempts = len(rs)
        solved = [r for r in rs if r.arbiter_solved]
        pass_at_1 = len(solved) / attempts if attempts else 0.0
        mean_rounds = sum(r.rounds for r in solved) / len(solved) if solved else None
        mean_wall = sum(r.wall_s for r in solved) / len(solved) if solved else None
        mean_tok = sum(r.total_tokens for r in solved) / len(solved) if solved else None
        truncated = sum(getattr(r, "n_truncated_rounds", 0) for r in rs)
        print(f"| {system} | {attempts} | {pass_at_1:.2f} | {len(solved)} | "
              f"{_fmt(mean_rounds, '.1f')} | {_fmt(mean_wall, '.1f')} | "
              f"{_fmt(mean_tok, '.0f')} | {truncated} |")

    print("\nPer-problem pass@1:")
    problems = sorted({r.problem for r in rows})
    for problem in problems:
        parts = []
        for system in SYSTEMS:
            rs = [r for r in by_system.get(system, []) if r.problem == problem]
            solved = sum(1 for r in rs if r.arbiter_solved)
            parts.append(f"{system}={solved}/{len(rs)}")
        print(f"  {problem}: {', '.join(parts)}")

    print("\nError classes (unsolved attempts):")
    classes: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.arbiter_solved:
            continue
        err = (r.error or "none recorded").splitlines()[0]
        if "truncated at max_tokens" in err:
            key = "truncated at max_tokens"
        elif "empty response" in err:
            key = "empty response"
        elif "wall cap" in err:
            key = "wall cap exceeded"
        elif "not found" in err and "theorem" in err:
            key = "target theorem missing"
        elif "sorry/oops" in err:
            key = "sorry/oops left in file"
        else:
            key = err[:60]
        classes[key] += 1
    for key, count in sorted(classes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {key}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", type=Path,
                        default=Path(__file__).resolve().parent / "runs")
    args = parser.parse_args()
    summarize(args.runs_dir)


if __name__ == "__main__":
    main()
