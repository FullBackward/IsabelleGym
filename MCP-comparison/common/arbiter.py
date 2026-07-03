"""Neutral solved check via isabelle build."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .problems import Problem


_SORRY_RE = re.compile(r"\b(sorry|oops)\b")


def _contains_sorry_parsed(text: str) -> bool:
    # Simple lexical check; ignores comments/strings for now.
    # TODO: use isabelle tokenization if false positives arise.
    return bool(_SORRY_RE.search(text))


def _find_isabelle(bin_name: str) -> str:
    """Resolve isabelle binary. If bin_name is an absolute path, use it.
    Otherwise try PATH, then common Windows install locations."""
    if Path(bin_name).exists():
        return str(Path(bin_name).resolve())

    # Try PATH
    path_exe = shutil.which(bin_name)
    if path_exe:
        return path_exe

    # Common Windows install paths
    home = Path.home()
    candidates = [
        home / r"isabelle\Isabelle2025-2\bin\isabelle.exe",
        home / r"isabelle\Isabelle2025-1\bin\isabelle.exe",
        home / r"isabelle\Isabelle2025\bin\isabelle.exe",
        Path(r"C:\Program Files\Isabelle2025-2\bin\isabelle.exe"),
        Path(r"C:\Program Files (x86)\Isabelle2025-2\bin\isabelle.exe"),
        Path(r"C:\Program Files\Isabelle2025-1\bin\isabelle.exe"),
        Path(r"C:\Program Files (x86)\Isabelle2025-1\bin\isabelle.exe"),
        Path(r"C:\Program Files\Isabelle2025\bin\isabelle.exe"),
        Path(r"C:\Program Files (x86)\Isabelle2025\bin\isabelle.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)

    return bin_name


def check(
    problem: Problem,
    final_thy_path: Path,
    isabelle_bin: str = "isabelle",
    cleanup: bool = True,
) -> dict[str, object]:
    """Return {"solved": bool, "error": str|None, "build_log": str}."""
    final_thy_path = Path(final_thy_path)
    if not final_thy_path.exists():
        return {"solved": False, "error": f"file not found: {final_thy_path}", "build_log": ""}

    final_text = final_thy_path.read_text(encoding="utf-8")

    # 1. No sorry/oops on parsed commands
    if _contains_sorry_parsed(final_text):
        return {"solved": False, "error": "file still contains sorry/oops", "build_log": ""}

    # 2. Target theorem present
    if f"theorem {problem.theorem_name}" not in final_text:
        return {
            "solved": False,
            "error": f"target theorem {problem.theorem_name} not found",
            "build_log": "",
        }

    # 3. Build clean in a throwaway ROOT session
    resolved_bin = _find_isabelle(isabelle_bin)
    tmpdir = Path(tempfile.mkdtemp(prefix="isabelle_arbiter_"))
    try:
        root_path = tmpdir / "ROOT"
        thy_path = tmpdir / f"{problem.name}.thy"
        thy_path.write_text(final_text, encoding="utf-8")
        # Map theory imports to session parents. Common cases:
        #   Main / Complex_Main       -> HOL
        #   HOL-Analysis.<X>          -> HOL-Analysis
        #   HOL-<Session>.<Theory>    -> HOL-<Session>
        # For mixed imports, list every distinct session parent.
        parents: list[str] = []
        for imp in problem.imports:
            if imp in ("Main", "Complex_Main"):
                parents.append("HOL")
            elif "." in imp:
                # Session-qualified import: "HOL-Analysis.Lebesgue" → session "HOL-Analysis"
                parents.append(imp.split(".")[0])
            else:
                parents.append(imp)  # best-effort
        parents = sorted(set(parents))
        parent_clause = " + ".join(parents) + " +"
        root_content = (
            f"session {problem.name}_Arbiter = {parent_clause}\n"
            f"  theories\n"
            f"    {problem.name}\n"
        )
        root_path.write_text(root_content, encoding="utf-8")

        cmd = [resolved_bin, "build", "-D", str(tmpdir), "-v", f"{problem.name}_Arbiter"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        build_log = proc.stdout + proc.stderr
        solved = proc.returncode == 0
        return {
            "solved": solved,
            "error": None if solved else "isabelle build failed",
            "build_log": build_log,
        }
    finally:
        if cleanup:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    from .config import load
    from .problems import parse_thy

    if len(sys.argv) != 3:
        print("Usage: python -m common.arbiter <original.thy> <final.thy>")
        sys.exit(1)
    cfg = load()
    problem = parse_thy(Path(sys.argv[1]))
    verdict = check(problem, Path(sys.argv[2]), isabelle_bin=cfg.arbiter_isabelle_bin)
    print(f"solved={verdict['solved']}")
    if verdict["error"]:
        print(f"error={verdict['error']}")
