from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import httpx

THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
BASE_URL = "/api/v1/sessions"


def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


class IsabelleGymAsyncClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def __aenter__(self) -> "IsabelleGymAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        response = await self.client.request(
            method=method,
            url=path,
            json=json_body,
            headers=headers,
            timeout=timeout if timeout is not None else self.timeout,
        )
        return response

    @staticmethod
    def _lease_headers(lease_id: str | None) -> dict[str, str] | None:
        if lease_id:
            return {"X-Lease-Id": lease_id}
        return None

    async def health(self) -> dict[str, Any]:
        response = await self._request("GET", "/")
        response.raise_for_status()
        return response.json()

    async def create_session(
        self,
        theories: list[str] | None = None,
        field: str | None = "HOL",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if theories is not None:
            payload["theories"] = theories
        if field is not None:
            payload["field"] = field
        response = await self._request("POST", BASE_URL, json_body=payload)
        response.raise_for_status()
        return response.json()

    async def acquire_session(
        self,
        theories: list[str] | None = None,
        field: str | None = "HOL",
        reuse_dirty: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reuse_dirty": reuse_dirty}
        if theories is not None:
            payload["theories"] = theories
        if field is not None:
            payload["field"] = field
        response = await self._request("POST", f"{BASE_URL}/acquire", json_body=payload)
        response.raise_for_status()
        return response.json()

    async def close_session(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "DELETE", f"{BASE_URL}/{session_id}",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def release_session(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/release",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def enter_theory(
        self, session_id: str, theory_name: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/enter_theory/{theory_name}",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/commands",
            json_body={
                "command": command,
                "timeout": timeout if timeout is not None else self.timeout,
            },
            headers=self._lease_headers(lease_id),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def verify_chunk(
        self,
        session_id: str,
        chunk: str,
        timeout: float | None = None,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a whole proof chunk in one call under a SINGLE wall budget.

        Returns a per-command status report in source order:
        ``{success, timed_out, stuck_line, commands: [{index, line, kind, status,
        messages}], execution_time}``. On timeout the report is partial and ``stuck_line``
        names the still-``running`` command (the likely loop) — no intermediate timeouts.
        """
        budget = timeout if timeout is not None else self.timeout
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/verify_chunk",
            json_body={"chunk": chunk, "timeout": budget},
            headers=self._lease_headers(lease_id),
            # client waits a bit beyond the server's wall budget (server bounds the work)
            timeout=(budget + 60.0) if budget is not None else None,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def format_chunk_report(report: dict[str, Any], *, max_msg: int = 300) -> str:
        """Render a ``verify_chunk`` report as a readable, source-ordered table.

        Saves callers from writing their own loop. ``report`` is the dict returned by
        :meth:`verify_chunk`. Per-command rows show a status marker, line, command kind,
        status, and any error/warning messages (whitespace-collapsed, truncated to
        ``max_msg`` chars). Returns a string; see :meth:`print_chunk_report` to print it.
        """
        marker = {"ok": "OK ", "failed": "ERR", "running": "RUN", "unprocessed": "..."}
        lines = [
            "verify_chunk: success={success} timed_out={timed_out} "
            "stuck_line={stuck_line} time={t:.2f}s".format(
                success=report.get("success"),
                timed_out=report.get("timed_out"),
                stuck_line=report.get("stuck_line"),
                t=float(report.get("execution_time", 0.0) or 0.0),
            )
        ]
        commands = report.get("commands") or []
        if not commands:
            lines.append("  (no commands reported)")
        for c in commands:
            status = str(c.get("status", "?"))
            lines.append(
                f"  [{marker.get(status, ' ? ')}] line {int(c.get('line', 0)):>3}  "
                f"{str(c.get('kind', '')):<7} {status}"
            )
            for m in c.get("messages") or []:
                text = " ".join(str(m.get("text", "")).split())
                if len(text) > max_msg:
                    text = text[: max_msg - 1] + "…"
                lines.append(f"        {m.get('sev', '')}: {text}")
        return "\n".join(lines)

    @staticmethod
    def print_chunk_report(report: dict[str, Any], *, max_msg: int = 300) -> None:
        """Pretty-print a :meth:`verify_chunk` report (see :meth:`format_chunk_report`)."""
        print(IsabelleGymAsyncClient.format_chunk_report(report, max_msg=max_msg))

    async def sledgehammer(
        self,
        session_id: str,
        timeout_s: int = 30,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Run Isabelle's sledgehammer on the current proof goal.

        Returns a dict with keys: success (bool), suggestions (list of strings),
        raw_output (str), execution_time (float).

        The session must already be in an active proof state.
        """
        http_timeout = timeout_s + 30.0
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/sledgehammer",
            json_body={"timeout_s": timeout_s},
            headers=self._lease_headers(lease_id),
            timeout=http_timeout,
        )
        response.raise_for_status()
        return response.json()

    async def verify_bigstep_text(
        self,
        theory_name: str,
        theory_text: str,
        *,
        field: str | None = "HOL",
        timeout: float = 300.0,
    ) -> httpx.Response:
        payload = {
            "theory_name": theory_name,
            "field": field,
            "theory": theory_text,
            "timeout": timeout,
        }
        return await self._request(
            "POST",
            f"{BASE_URL}/bigstep",
            json_body=payload,
            timeout=timeout,
        )

    async def verify_bigstep_file(
        self,
        file_path: str | Path,
        *,
        field: str | None = "HOL",
        timeout: float = 300.0,
    ) -> httpx.Response:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Theory file {path} not found.")
        theory_text = path.read_text(encoding="utf-8")
        theory_name = extract_theory_name(theory_text) or path.stem
        return await self.verify_bigstep_text(
            theory_name=theory_name,
            theory_text=theory_text,
            field=field,
            timeout=timeout,
        )
