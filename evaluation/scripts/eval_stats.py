from __future__ import annotations

import math
import statistics
from typing import Optional


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


def normalize_execution_time(value) -> Optional[float]:
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    return None


def is_warning_message(msg: Optional[str]) -> bool:
    if not msg:
        return False
    stripped = msg.strip()
    first = stripped.splitlines()[0].strip().lower()
    if first.startswith("warning") or first.startswith("ml warning"):
        return True
    lowered = stripped.lower()
    if "warning" in lowered and "error" not in lowered and "failed" not in lowered:
        return True
    if not "error" in lowered:
        return True
    return False


def preview(text: str, n: int = 100) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 3] + "..."
