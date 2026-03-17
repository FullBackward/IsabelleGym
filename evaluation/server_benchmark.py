#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


def iter_thy_files(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p.is_file() and p.suffix == ".thy":
            yield p
        elif p.is_dir():
            yield from p.rglob("*.thy")


def extract_declared_theory_name(theory_text: str) -> Optional[str]:
    match = re.search(r"\btheory\s+([A-Za-z0-9_'.-]+)\b", theory_text)
    if match:
        return match.group(1)
    return None


@dataclass
class FileBenchmarkResult:
    file: str
    theory_name: Optional[str]
    endpoint: str
    ok: bool
    status_code: Optional[int]
    duration_sec: float
    error: Optional[str]
    response: Any


class AsyncRequestsBenchmarkClient:
    """
    Async facade over requests.post, modeled after an async API client.
    Uses asyncio.to_thread so it can be awaited from asyncio code.
    """

    def __init__(
        self,
        api_url: str,
        headers: Optional[dict[str, str]] = None,
        http_timeout: int = 600,
        n_retries: int = 3,
        retry_backoff_sec: float = 1.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = headers or {"Content-Type": "application/json"}
        self.http_timeout = http_timeout
        self.n_retries = n_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.api_url + path

    def _post_sync(self, url: str, payload: dict[str, Any]) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(1, self.n_retries + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.http_timeout)
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    raise ValueError(f"Server returned non-JSON: {response.text}") from exc
            except Exception as exc:  # requests.exceptions.RequestException or JSON error
                last_err = exc
                if attempt == self.n_retries:
                    break
                time.sleep(self.retry_backoff_sec * (2 ** (attempt - 1)))
        assert last_err is not None
        raise RuntimeError(f"Request failed after {self.n_retries} retries: {last_err}")

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        url = self.build_url(path)
        return await asyncio.to_thread(self._post_sync, url, payload)

    async def health(self, path: str = "/health") -> Any:
        # Kept as POST because you asked to send requests through requests.post.
        return await self.post(path, {})

    async def close(self) -> None:
        await asyncio.to_thread(self.session.close)


async def bigstep(
    thy: str,
    url: str,
    client: AsyncRequestsBenchmarkClient,
    file_path: str,
    timeout: int,
    theory_name: str,
) -> FileBenchmarkResult:
    # Adjust this payload if your Isabelle server expects different key names.
    payload = {
        "theory_name": theory_name,
        "dependencies": [],
        "field": "HOL",
        "theory": thy,
    }

    start = time.perf_counter()
    try:
        response = await client.post(url, payload)
        duration = time.perf_counter() - start
        return FileBenchmarkResult(
            file=file_path,
            theory_name=theory_name,
            endpoint=url,
            ok=True,
            status_code=200,
            duration_sec=duration,
            error=None,
            response=response,
        )
    except Exception as exc:
        duration = time.perf_counter() - start
        return FileBenchmarkResult(
            file=file_path,
            theory_name=theory_name,
            endpoint=url,
            ok=False,
            status_code=None,
            duration_sec=duration,
            error=str(exc),
            response=None,
        )


async def small_step(
    thy: str,
    url: str,
    client: AsyncRequestsBenchmarkClient,
    file_path: str,
    timeout: int,
    theory_name: Optional[str] = None,
) -> FileBenchmarkResult:
    # Adjust this payload if your Isabelle server expects different key names.
    payload = {
        "theory": thy,
        "theory_name": theory_name,
        "timeout": timeout,
    }

    start = time.perf_counter()
    try:
        response = await client.post(url, payload)
        duration = time.perf_counter() - start
        return FileBenchmarkResult(
            file=file_path,
            theory_name=theory_name,
            endpoint=url,
            ok=True,
            status_code=200,
            duration_sec=duration,
            error=None,
            response=response,
        )
    except Exception as exc:
        duration = time.perf_counter() - start
        return FileBenchmarkResult(
            file=file_path,
            theory_name=theory_name,
            endpoint=url,
            ok=False,
            status_code=None,
            duration_sec=duration,
            error=str(exc),
            response=None,
        )


async def _run_batch(
    batch: list[Path],
    step: str,
    client: AsyncRequestsBenchmarkClient,
    url: str,
    timeout: int,
) -> list[FileBenchmarkResult]:
    results: list[FileBenchmarkResult] = []
    for thy_file in batch:
        theory_text = thy_file.read_text(encoding="utf-8")
        theory_name = extract_declared_theory_name(theory_text)
        if step == "bigstep":
            result = await bigstep(
                thy=theory_text,
                url=url,
                client=client,
                file_path=str(thy_file),
                timeout=timeout,
                theory_name=theory_name,
            )
        elif step == "smallstep":
            result = await small_step(
                thy=theory_text,
                url=url,
                client=client,
                file_path=str(thy_file),
                timeout=timeout,
                theory_name=theory_name,
            )
        else:
            raise ValueError(f"Unknown step type: {step}")
        results.append(result)
    return results


async def _bounded_gather(
    batches: list[list[Path]],
    step: str,
    client: AsyncRequestsBenchmarkClient,
    url: str,
    timeout: int,
    max_workers: int,
) -> list[FileBenchmarkResult]:
    sem = asyncio.Semaphore(max_workers)

    async def worker(batch: list[Path]) -> list[FileBenchmarkResult]:
        async with sem:
            return await _run_batch(batch, step=step, client=client, url=url, timeout=timeout)

    tasks = [asyncio.create_task(worker(batch)) for batch in batches]
    results: list[FileBenchmarkResult] = []

    total = len(tasks)
    done_count = 0
    for fut in asyncio.as_completed(tasks):
        batch_results = await fut
        results.extend(batch_results)
        done_count += 1
        print(f"Completed {done_count}/{total} batch(es)")

    return results


async def benchmark(
    step: str,
    corpus: Path,
    server_host: str,
    server_port: int,
    batch_size: int,
    max_workers: int,
    timeout: int,
    output: Optional[Path],
    bigstep_path: str,
    smallstep_path: str,
    health_path: Optional[str],
) -> None:
    base_url = f"http://{server_host}:{server_port}"
    client = AsyncRequestsBenchmarkClient(api_url=base_url, http_timeout=timeout)

    try:
        if health_path:
            try:
                health = await client.health(health_path)
                print("Health check response:")
                print(json.dumps(health, indent=2, ensure_ascii=False))
            except Exception as exc:
                print(f"Health check failed: {exc}")

        files = sorted(iter_thy_files([corpus]))
        if not files:
            raise FileNotFoundError(f"No .thy files found under: {corpus}")

        if batch_size <= 0:
            batch_size = 1
        batches = [files[i : i + batch_size] for i in range(0, len(files), batch_size)]

        if step == "bigstep":
            print("Starting big-step benchmark...")
            url = bigstep_path
        elif step == "smallstep":
            print("Starting small-step benchmark...")
            url = smallstep_path
        else:
            raise ValueError(f"Unknown step type: {step}")

        start_time = time.perf_counter()
        results = await _bounded_gather(
            batches=batches,
            step=step,
            client=client,
            url=url,
            timeout=timeout,
            max_workers=max_workers,
        )
        total_duration = time.perf_counter() - start_time

        ok_count = sum(1 for r in results if r.ok)
        fail_count = len(results) - ok_count
        avg_duration = sum(r.duration_sec for r in results) / max(1, len(results))

        summary = {
            "step": step,
            "base_url": base_url,
            "endpoint": url,
            "total_files": len(results),
            "successful": ok_count,
            "failed": fail_count,
            "avg_duration_sec": avg_duration,
            "total_duration_sec": total_duration,
            "results": [asdict(r) for r in results],
        }

        print("\nBenchmark summary:")
        print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))

        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Wrote results to {output}")
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark Isabelle server big-step or small-step endpoints over a corpus of .thy files.")
    p.add_argument("--corpus", required=True, type=Path)
    p.add_argument("--server-host", default="localhost")
    p.add_argument("--server-port", type=int, default=8000)
    p.add_argument("--step", required=True, choices=["bigstep", "smallstep"], default="bigstep")
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--bigstep-path", default="/api/v1/sessions/bigstep")
    p.add_argument("--smallstep-path", default="/api/v1/sessions/execute_command")
    p.add_argument("--health-path", default="/")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        benchmark(
            step=args.step,
            corpus=args.corpus,
            server_host=args.server_host,
            server_port=args.server_port,
            batch_size=args.batch_size,
            max_workers=args.max_workers,
            timeout=args.timeout,
            output=args.output,
            bigstep_path=args.bigstep_path,
            smallstep_path=args.smallstep_path,
            health_path=args.health_path,
        )
    )
