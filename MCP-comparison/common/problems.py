"""Parse comparison problem files (.thy with one theorem ... sorry)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Problem:
    name: str          # theory name
    theorem_name: str  # target theorem name
    imports: list[str]
    statement: str     # theorem statement without trailing sorry
    full_text: str     # original file content
    path: Path


def _normalize_import(raw: str) -> str:
    """Strip quotes and whitespace from a theory import name."""
    return raw.strip().strip('"').strip("'")


_HEADER_RE = re.compile(
    r"^theory\s+(\S+)\s+imports\s+(.+?)\s+begin",
    re.MULTILINE | re.DOTALL,
)
_THEOREM_RE = re.compile(
    r"(theorem\s+(\S+)\s*:.*?)\bsorry\b",
    re.MULTILINE | re.DOTALL,
)


def parse_thy(path: Path) -> Problem:
    text = path.read_text(encoding="utf-8")
    header = _HEADER_RE.search(text)
    if not header:
        raise ValueError(f"{path}: cannot parse theory header")
    theory_name = header.group(1)
    imports = [_normalize_import(i) for i in header.group(2).split() if i.strip()]

    theorem = _THEOREM_RE.search(text)
    if not theorem:
        raise ValueError(f"{path}: cannot find 'theorem ... sorry'")
    theorem_name = theorem.group(2)
    statement = theorem.group(1).rstrip()

    return Problem(
        name=theory_name,
        theorem_name=theorem_name,
        imports=imports,
        statement=statement,
        full_text=text,
        path=path,
    )


_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")


def sanitize_for_isabelle(text: str) -> str:
    """Remove lone surrogates and other code points that UTF-8-Isabelle rejects."""
    return _SURROGATE_RE.sub("", text)


def load_problems(directory: Path) -> list[Problem]:
    files = sorted(directory.glob("*.thy"))
    if not files:
        raise ValueError(f"No .thy files found in {directory}")
    return [parse_thy(f) for f in files]
