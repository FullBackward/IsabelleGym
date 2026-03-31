#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

import httpx


class IsabelleGymAsyncClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        )

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
        timeout: float | None = None,
    ) -> Any:
        response = await self.client.request(
            method=method,
            url=path,
            json=json_body,
            timeout=timeout if timeout is not None else self.timeout,
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(
                f"{method} {self.base_url}{path} failed: "
                f"{response.status_code} {detail}"
            ) from exc

        if not response.content:
            return None

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    # ---------- Health ----------
    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/")

    # ---------- Session management ----------
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
        return await self._request("POST", "/api/v1/sessions", json_body=payload)

    async def list_sessions(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/sessions")

    async def get_session_info(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/sessions/{session_id}")

    async def close_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/sessions/{session_id}")

    # ---------- Session operations ----------
    async def enter_theory(self, session_id: str, theory_name: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/enter_theory/{theory_name}",
        )

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/commands",
            json_body={
                "command": command,
                "timeout": timeout if timeout is not None else self.timeout,
            },
            timeout=timeout,
        )

    async def get_state(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/sessions/{session_id}/state")

    async def get_subgoals(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/sessions/{session_id}/subgoals")

    async def get_source(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/sessions/{session_id}/source")

    async def get_history(self, session_id: str, limit: int = 50) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/sessions/{session_id}/history?limit={limit}")

    async def save_checkpoint(self, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/sessions/{session_id}/checkpoints")

    async def restore_checkpoint(self, session_id: str, checkpoint_id: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore",
        )

    async def rollback(self, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/sessions/{session_id}/rollback")

    # ---------- Big-step ----------
    async def verify_bigstep(
        self,
        theory_name: str,
        theory_text: str,
        dependencies: Iterable[str] | None = None,
        field: str | None = "HOL",
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        payload = {
            "theory_name": theory_name,
            "dependencies": list(dependencies or []),
            "field": field,
            "theory": theory_text,
            "timeout": timeout,
        }
        return await self._request(
            "POST",
            "/api/v1/sessions/bigstep",
            json_body=payload,
            timeout=timeout,
        )


async def demo_smallstep(client: IsabelleGymAsyncClient) -> None:
    created = await client.create_session(theories=["Main"], field="HOL")
    session_id = created["session_id"]
    print(f"Created session: {session_id}")

    try:
        print(await client.enter_theory(session_id, "Scratch"))

        commands = [
            "theory Scratch imports Main begin\n",
            'lemma \"A ⟹ A\"\n',
            "by assumption\n",
            "end\n",
        ]

        for cmd in commands:
            result = await client.execute_command(session_id, cmd, timeout=60.0)
            print("\nCOMMAND:")
            print(cmd.rstrip())
            print("RESULT:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

            if not result.get("success", False):
                print("Stopping because the command failed.")
                break

        print("\nSOURCE:")
        print(json.dumps(await client.get_source(session_id), indent=2, ensure_ascii=False))

    finally:
        print("\nCLOSING SESSION")
        print(json.dumps(await client.close_session(session_id), indent=2, ensure_ascii=False))


async def demo_bigstep(client: IsabelleGymAsyncClient, thy_path: Path) -> None:
    theory_text = thy_path.read_text(encoding="utf-8")
    theory_name = thy_path.stem

    result = await client.verify_bigstep(
        theory_name=theory_name,
        theory_text=theory_text,
        dependencies=["Main"],
        field="HOL",
        timeout=300.0,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Async client for IsabelleGym FastAPI server")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=30.0)

    sub = parser.add_subparsers(dest="mode", required=True)

    sub.add_parser("health")
    sub.add_parser("smallstep-demo")

    big = sub.add_parser("bigstep")
    big.add_argument("theory_file", type=Path)

    args = parser.parse_args()

    async with IsabelleGymAsyncClient(
        base_url=args.base_url,
        timeout=args.timeout,
    ) as client:
        if args.mode == "health":
            print(json.dumps(await client.health(), indent=2, ensure_ascii=False))
        elif args.mode == "smallstep-demo":
            await demo_smallstep(client)
        elif args.mode == "bigstep":
            await demo_bigstep(client, args.theory_file)


if __name__ == "__main__":
    asyncio.run(amain())