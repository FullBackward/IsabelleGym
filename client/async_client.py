from __future__ import annotations

import json
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
        """Build the X-Lease-Id header dict, or None if no lease_id."""
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
        """Find an existing session matching theories/field or create a new one.

        Returns a dict with ``session_id``, ``theories``, ``status``,
        ``reused``, and ``lease_id``.
        """
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
        """Release the exclusive lease on a session, returning it to the
        pool for reuse.  Unlike ``close_session``, the backend stays alive."""
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
