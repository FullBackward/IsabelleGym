"""Container-aware memory monitor for the session pool.

Memory management used to live in the Scala backend, where it measured the
gateway JVM heap (`Runtime.getRuntime`). That number is unrelated to where
Isabelle's memory actually goes: each session is a separate `poly` (ML) OS
process the JVM cannot see. This monitor instead reads the **container cgroup**
(`/sys/fs/cgroup/memory.*`), which accounts for every process in the container
— gateway JVM, Python server, and all `poly` sessions — i.e. the exact figure
the OOM killer watches.

Pure stdlib, no psutil. Every file read is defensive: on any failure the
monitor degrades to a safe fallback rather than raising into the request path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from server.app.core.config import Memory
from server.app.core.logging import get_logger

logger = get_logger(__name__)

# cgroup v2 (preferred) and v1 file locations.
_CG_V2_CURRENT = "/sys/fs/cgroup/memory.current"
_CG_V2_MAX = "/sys/fs/cgroup/memory.max"
_CG_V2_STAT = "/sys/fs/cgroup/memory.stat"
_CG_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CG_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CG_V1_STAT = "/sys/fs/cgroup/memory/memory.stat"
_PROC_MEMINFO = "/proc/meminfo"

# cgroup v1 reports "unlimited" as a near-int64 sentinel; anything this large
# means "no limit", so we fall back to host memory.
_V1_UNLIMITED_FLOOR = 1 << 62


@dataclass(frozen=True)
class MemorySnapshot:
    used_bytes: int
    limit_bytes: int

    @property
    def available_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def pressure_pct(self) -> float:
        if self.limit_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.limit_bytes) * 100.0

    @property
    def used_mb(self) -> float:
        return self.used_bytes / (1024.0 * 1024.0)

    @property
    def limit_mb(self) -> float:
        return self.limit_bytes / (1024.0 * 1024.0)

    @property
    def available_mb(self) -> float:
        return self.available_bytes / (1024.0 * 1024.0)


def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_cgroup_max(path: str) -> Optional[int]:
    """cgroup v2 memory.max is either an integer or the literal "max"."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if raw == "max":
        return None  # no limit -> caller falls back to host memory
    try:
        return int(raw)
    except ValueError:
        return None


def _host_mem_total_bytes() -> Optional[int]:
    """MemTotal from /proc/meminfo (kB), as host/VM total memory."""
    try:
        with open(_PROC_MEMINFO, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


class MemoryMonitor:
    """Reads container memory pressure and decides whether new sessions fit."""

    def __init__(self) -> None:
        self.pressure_threshold = Memory.PRESSURE_THRESHOLD
        self.min_available_bytes = Memory.MIN_AVAILABLE_MB * 1024 * 1024
        self.fallback_limit_bytes = Memory.FALLBACK_SYSTEM_MB * 1024 * 1024

    @staticmethod
    def _read_inactive_file() -> int:
        """Reclaimable page cache (inactive_file) from memory.stat, 0 on failure.

        memory.current/usage_in_bytes INCLUDE page cache — Isabelle heap images
        read at session start stay cached and would otherwise count as 'used'
        forever, tripping the admission gate even when the kernel could reclaim
        them. Subtracting inactive_file is the same correction `docker stats`
        applies (v2 key: inactive_file; v1 hierarchical key: total_inactive_file).
        """
        for path, key in ((_CG_V2_STAT, "inactive_file"), (_CG_V1_STAT, "total_inactive_file")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith(key + " "):
                            return int(line.split()[1])
            except (OSError, ValueError, IndexError):
                continue
        return 0

    def _read_used(self) -> int:
        used = _read_int_file(_CG_V2_CURRENT)
        if used is None:
            used = _read_int_file(_CG_V1_USAGE)
        if used is None:
            return 0
        return max(0, used - self._read_inactive_file())

    def _read_limit(self) -> int:
        # cgroup v2 numeric limit, else v1 limit (unless sentinel "unlimited"),
        # else host MemTotal, else the configured fallback.
        limit = _read_cgroup_max(_CG_V2_MAX)
        if limit is None:
            v1 = _read_int_file(_CG_V1_LIMIT)
            if v1 is not None and v1 < _V1_UNLIMITED_FLOOR:
                limit = v1
        if limit is None:
            limit = _host_mem_total_bytes()
        if limit is None or limit <= 0:
            limit = self.fallback_limit_bytes
        return limit

    def read(self) -> MemorySnapshot:
        return MemorySnapshot(used_bytes=self._read_used(), limit_bytes=self._read_limit())

    def can_admit(self, snapshot: Optional[MemorySnapshot] = None) -> bool:
        """True if there is room for another session under current pressure."""
        snap = snapshot if snapshot is not None else self.read()
        return (
            snap.pressure_pct < self.pressure_threshold
            and snap.available_bytes > self.min_available_bytes
        )

    def status_dict(self, snapshot: Optional[MemorySnapshot] = None) -> Dict[str, Any]:
        snap = snapshot if snapshot is not None else self.read()
        return {
            "memory_used_mb": round(snap.used_mb, 1),
            "memory_limit_mb": round(snap.limit_mb, 1),
            "memory_available_mb": round(snap.available_mb, 1),
            "memory_pressure_pct": round(snap.pressure_pct, 1),
        }
