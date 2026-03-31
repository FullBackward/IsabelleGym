from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from server.app.core.logging import get_logger
from server.app.services.internal_models import BigStepExecuteResult
from server.app.services.theory_parsing import extract_theory_name

logger = get_logger(__name__)


def normalize_theories(theories: Optional[List[str]]) -> List[str]:
    if not theories:
        return []
    out: List[str] = []
    for t in theories:
        if t is None:
            continue
        s = str(t).strip()
        if s:
            out.append(s)
    return sorted(set(out))


def build_dependency_key(field: str, theories: Optional[List[str]]) -> str:
    deps = normalize_theories(theories)
    raw = f"{field}::{'|'.join(deps)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_theory_hash(field: str, dependency_key: str, theory_text: str) -> str:
    raw = f"{field}::{dependency_key}::{theory_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_root(
    root_path: Path,
    session_name: str,
    parent_session: str,
    theory_name: str,
) -> None:
    root_text = f'''session "{session_name}" = "{parent_session}" +
  options [document = false, browser_info = false]
  theories
    "{theory_name}"
'''
    root_path.write_text(root_text, encoding="utf-8")


class BuildVerifier:
    def __init__(
        self,
        *,
        isabelle_bin: str = "isabelle",
        max_concurrent_builds: int = 4,
        build_jobs_per_request: int = 1,
        temp_parent: Optional[Path] = None,
    ) -> None:
        self.isabelle_bin = isabelle_bin
        self.build_jobs_per_request = build_jobs_per_request
        self.temp_parent = temp_parent
        self._sem = asyncio.Semaphore(max_concurrent_builds)
        self._cache: Dict[str, BigStepExecuteResult] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def verify(
        self,
        *,
        theory_name: str,
        theory_text: str,
        dependencies: Optional[List[str]],
        field: Optional[str],
        timeout: float,
    ) -> BigStepExecuteResult:
        parent_session = (field or "HOL").strip() or "HOL"
        deps = normalize_theories(dependencies)
        dependency_key = build_dependency_key(parent_session, deps)
        theory_hash = stable_theory_hash(parent_session, dependency_key, theory_text)

        cached = self._cache.get(theory_hash)
        if cached is not None:
            return cached.model_copy(update={"execution_time": 0.0})

        lock = self._locks.setdefault(theory_hash, asyncio.Lock())
        async with lock:
            cached = self._cache.get(theory_hash)
            if cached is not None:
                return cached.model_copy(update={"execution_time": 0.0})

            async with self._sem:
                result = await self._run_build(
                    theory_name=theory_name,
                    theory_text=theory_text,
                    dependencies=deps,
                    parent_session=parent_session,
                    dependency_key=dependency_key,
                    timeout=timeout,
                )

            if result.success:
                self._cache[theory_hash] = result
            return result

    async def _run_build(
        self,
        *,
        theory_name: str,
        theory_text: str,
        dependencies: List[str],
        parent_session: str,
        dependency_key: str,
        timeout: float,
    ) -> BigStepExecuteResult:
        start = time.perf_counter()

        declared = extract_theory_name(theory_text)
        if declared is None:
            return BigStepExecuteResult(
                success=False,
                error="Could not extract theory name from submitted theory text.",
                execution_time=0.0,
                mode="build_strict",
                theory_verified=False,
            )
        if declared != theory_name:
            return BigStepExecuteResult(
                success=False,
                error=f"Declared theory name '{declared}' does not match request theory_name '{theory_name}'.",
                execution_time=0.0,
                mode="build_strict",
                theory_verified=False,
            )

        temp_ctx = tempfile.TemporaryDirectory(
            prefix="isabelle-build-server-",
            dir=str(self.temp_parent) if self.temp_parent else None,
        )

        try:
            temp_dir = Path(temp_ctx.name)
            (temp_dir / f"{theory_name}.thy").write_text(theory_text, encoding="utf-8")

            session_name = f"Build_{dependency_key[:10]}_{hashlib.sha256(theory_text.encode()).hexdigest()[:10]}"
            write_root(
                temp_dir / "ROOT",
                session_name=session_name,
                parent_session=parent_session,
                theory_name=theory_name,
            )

            cmd = [
                self.isabelle_bin,
                "build",
                "-D",
                str(temp_dir),
                "-j",
                str(self.build_jobs_per_request),
                "-o",
                "document=false",
                "-o",
                "browser_info=false",
                session_name,
            ]

            logger.info("running isabelle build session=%s parent_session=%s", session_name, parent_session)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return BigStepExecuteResult(
                    success=False,
                    error=f"isabelle build timed out after {timeout:.1f}s",
                    execution_time=time.perf_counter() - start,
                    mode="build_strict",
                    theory_verified=False,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            ok = proc.returncode == 0

            return BigStepExecuteResult(
                success=ok,
                output=(stdout[-4000:] or None),
                error=None if ok else (
                    stderr[-4000:] or stdout[-4000:] or f"isabelle build failed with exit code {proc.returncode}"
                ),
                execution_time=time.perf_counter() - start,
                mode="build_strict",
                theory_verified=ok,
            )
        finally:
            temp_ctx.cleanup()