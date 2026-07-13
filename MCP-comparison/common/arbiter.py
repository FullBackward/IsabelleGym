"""Neutral solved check via IsabelleGym server bigstep endpoint."""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from client.async_client import IsabelleGymAsyncClient

from .problems import Problem

if "." not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_SORRY_RE = re.compile(r"\b(sorry|oops)\b")


def _contains_sorry_parsed(text: str) -> bool:
    return bool(_SORRY_RE.search(text))


async def check(
    problem: Problem,
    final_thy_path: Path,
    gym_url: str,
    field: str = "HOL",
) -> dict[str, object]:
    """Return {"solved": bool, "error": str|None, "build_log": str}.

    Submits the theory to the IsabelleGym server's bigstep endpoint
    (POST /api/v1/sessions/bigstep).  The server must be running with
    a warm session pool.
    """
    final_thy_path = Path(final_thy_path)
    if not final_thy_path.exists():
        return {"solved": False, "error": f"file not found: {final_thy_path}", "build_log": ""}

    final_text = final_thy_path.read_text(encoding="utf-8")

    # 1. No sorry/oops
    if _contains_sorry_parsed(final_text):
        return {"solved": False, "error": "file still contains sorry/oops", "build_log": ""}

    # 2. Target theorem present
    if f"theorem {problem.theorem_name}" not in final_text:
        return {
            "solved": False,
            "error": f"target theorem {problem.theorem_name} not found",
            "build_log": "",
        }

    # 3. Derive parent session (field) and dependencies from imports
    # that live in non-HOL sessions (only imports with a dot like
    # "HOL-Computational_Algebra.Computational_Algebra").
    deps: list[str] = []
    effective_field = field
    for imp in problem.imports:
        if "." in imp:
            parent = imp.split(".")[0]
            if parent and parent not in deps and parent != "Main":
                deps.append(parent)
    # If there are external session imports, use the first as parent (field).
    # Complex_Main and other HOL-internal imports stay with field="HOL".
    if deps:
        effective_field = deps[0]

    # 4. Send to the IsabelleGym server via async client
    try:
        async with IsabelleGymAsyncClient(base_url=gym_url, timeout=420.0) as client:
            resp = await client.verify_bigstep_text(
                theory_name=problem.name,
                theory_text=final_text,
                field=effective_field,
                timeout=300.0,
            )
            resp.raise_for_status()
            data = resp.json()
            solved = bool(data.get("theory_verified", False))
            return {
                "solved": solved,
                "error": None if solved else data.get("error", "bigstep verification failed"),
                "build_log": str(data),
            }
    except Exception as e:
        return {"solved": False, "error": f"gym arbiter: {e}", "build_log": ""}


if __name__ == "__main__":
    from .config import load
    from .problems import parse_thy

    if len(sys.argv) != 3:
        print("Usage: python -m common.arbiter <original.thy> <final.thy>")
        sys.exit(1)
    cfg = load()
    problem = parse_thy(Path(sys.argv[1]))

    async def _main():
        verdict = await check(
            problem,
            Path(sys.argv[2]),
            gym_url=cfg.arbiter_gym_url or "http://localhost:8000",
        )
        print(f"solved={verdict['solved']}")
        if verdict["error"]:
            print(f"error={verdict['error']}")

    asyncio.run(_main())