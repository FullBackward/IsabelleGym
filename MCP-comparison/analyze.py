#!/usr/bin/env python3
"""Read MCP-comparison/runs/*/results.jsonl and print summary tables."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

sys = __import__("sys")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.metrics import load_results


def summarize(runs_dir: Path) -> None:
    systems = ["isabellegym", "isabelle_mcp", "autocorrode"]
    rows = []
    for system in systems:
        path = runs_dir / system / "results.jsonl"
        if path.exists():
            rows.extend(load_results(path))

    if not rows:
        print(f"No results found under {runs_dir}")
        return

    by_system: dict[str, list] = defaultdict(list)
    for r in rows:
        by_system[r.system].append(r)

    print("| System | attempts | pass@1 | solved | mean rounds (solved) | mean wall_s (solved) | mean total tok (solved) |")
    print("|---|---|---|---|---|---|---|")
    for system in systems:
        rs = by_system.get(system, [])
        attempts = len(rs)
        solved = [r for r in rs if r.arbiter_solved]
        pass_at_1 = len(solved) / attempts if attempts else 0.0
        mean_rounds = sum(r.rounds for r in solved) / len(solved) if solved else None
        mean_wall = sum(r.wall_s for r in solved) / len(solved) if solved else None
        mean_tok = sum(r.total_tokens for r in solved) / len(solved) if solved else None
        print(f"| {system} | {attempts} | {pass_at_1:.2f} | {len(solved)} | "
              f"{mean_rounds if mean_rounds is not None else '-':.1f} | "
              f"{mean_wall if mean_wall is not None else '-':.1f} | "
              f"{mean_tok if mean_tok is not None else '-':.0f} |")

    print("\nPer-problem pass@1:")
    problems = sorted({r.problem for r in rows})
    for problem in problems:
        parts = []
        for system in systems:
            rs = [r for r in by_system.get(system, []) if r.problem == problem]
            solved = sum(1 for r in rs if r.arbiter_solved)
            parts.append(f"{system}={solved}/{len(rs)}")
        print(f"  {problem}: {', '.join(parts)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runs")
    args = parser.parse_args()
    summarize(args.runs_dir)


if __name__ == "__main__":
    main()
