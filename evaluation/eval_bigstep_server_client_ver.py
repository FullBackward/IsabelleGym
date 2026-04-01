#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from client.async_client import IsabelleGymAsyncClient


@dataclass
class FileResult:
    file: str
    theory_name: Optional[str]
    ok: bool
    wall_time_sec: float
    api_execution_time_sec: Optional[float]
    client_overhead_sec: Optional[float]
    api_wall_ratio: Optional[float]
    http_status: Optional[int]
    error: Optional[str]
    response: Optional[dict]


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
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None, "p90": None, "p95": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": safe_mean(values),
        "median": safe_median(values),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
    }


async def post_bigstep(
    client: IsabelleGymAsyncClient,
    thy_file: Path,
    timeout: int,
    field: str,
) -> FileResult:
    t0 = time.perf_counter()
    try:
        resp = await client.verify_bigstep_file(thy_file, field=field, timeout=timeout)
        elapsed = time.perf_counter() - t0

        http_status = resp.status_code
        resp.raise_for_status()

        data = resp.json()
        api_execution_time = data.get("execution_time")
        if isinstance(api_execution_time, int):
            api_execution_time = float(api_execution_time)
        elif not isinstance(api_execution_time, float):
            api_execution_time = None

        client_overhead = max(0.0, elapsed - api_execution_time) if api_execution_time is not None else None
        api_wall_ratio = (api_execution_time / elapsed) if (api_execution_time is not None and elapsed > 0.0) else None

        return FileResult(
            file=str(thy_file),
            theory_name=data.get("theory_name", thy_file.stem),
            ok=bool(data.get("success", False)),
            wall_time_sec=elapsed,
            api_execution_time_sec=api_execution_time,
            client_overhead_sec=client_overhead,
            api_wall_ratio=api_wall_ratio,
            http_status=http_status,
            error=data.get("error"),
            response=data,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return FileResult(
            file=str(thy_file),
            theory_name=thy_file.stem,
            ok=False,
            wall_time_sec=elapsed,
            api_execution_time_sec=None,
            client_overhead_sec=None,
            api_wall_ratio=None,
            http_status=None,
            error=str(exc),
            response=None,
        )


async def amain() -> None:
    ap = argparse.ArgumentParser(description="Benchmark whole-file Isabelle verification via IsabelleGym server big-step endpoint.")
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--field", default="HOL-Analysis")
    ap.add_argument("--output", type=Path, default=Path("server_bigstep_results.json"))
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    async with IsabelleGymAsyncClient(base_url=args.server, timeout=float(args.timeout)) as client:
        results = [asdict(await post_bigstep(client, f, args.timeout, args.field)) for f in files]

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0

    wall_times = [r["wall_time_sec"] for r in results]
    api_exec_times = [r["api_execution_time_sec"] for r in results if r["api_execution_time_sec"] is not None]
    overhead_times = [r["client_overhead_sec"] for r in results if r["client_overhead_sec"] is not None]
    api_wall_ratios = [r["api_wall_ratio"] for r in results if r["api_wall_ratio"] is not None]

    summary = {
        "tool": "isabellegym-server-bigstep-client",
        "benchmark_kind": "server_whole_file_verification_benchmark",
        "server": args.server,
        "field": args.field,
        "timeout_sec": args.timeout,
        "corpus": str(args.corpus),
        "total_files": len(results),
        "successes": sum(1 for r in results if r["ok"]),
        "failures": sum(1 for r in results if not r["ok"]),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_api_execution_time_sec": sum(api_exec_times) if api_exec_times else None,
        "total_client_overhead_sec": sum(overhead_times) if overhead_times else None,
        "files_per_minute": (len(results) / total_wall_time_sec * 60.0) if total_wall_time_sec > 0 else None,
        "mean_wall_time_sec_per_file": safe_mean(wall_times),
        "median_wall_time_sec_per_file": safe_median(wall_times),
        "mean_api_execution_time_sec_per_file": safe_mean(api_exec_times),
        "median_api_execution_time_sec_per_file": safe_median(api_exec_times),
        "mean_client_overhead_sec_per_file": safe_mean(overhead_times),
        "median_client_overhead_sec_per_file": safe_median(overhead_times),
        "mean_api_wall_ratio_per_file": safe_mean(api_wall_ratios),
        "median_api_wall_ratio_per_file": safe_median(api_wall_ratios),
        "wall_time_stats_sec": summarize_metric(wall_times),
        "api_execution_time_stats_sec": summarize_metric(api_exec_times),
        "client_overhead_stats_sec": summarize_metric(overhead_times),
        "api_wall_ratio_stats": summarize_metric(api_wall_ratios),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "client": "async_client",
        },
        "results": results,
    }

    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    asyncio.run(amain())
