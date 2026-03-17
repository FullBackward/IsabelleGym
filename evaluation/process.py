#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
from typing import Iterable, Set


THEORY_HEADER_RE = re.compile(
    r"(?P<prefix>\btheory\b.*?\bimports\b)(?P<imports>.*?)(?P<suffix>\bbegin\b)",
    re.DOTALL,
)

# Matches either:
#   Foo
#   "Foo"
#   "HOL-Analysis.Foo"
# but not Isabelle keywords
IMPORT_TOKEN_RE = re.compile(
    r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*'
)


def build_analysis_theory_set(analysis_dir: pathlib.Path) -> Set[str]:
    """
    Return basenames of all .thy files in the HOL/Analysis directory.
    Example: Homotopy.thy -> 'Homotopy'
    """
    return {p.stem for p in analysis_dir.glob("*.thy")}


def normalize_import_token(token: str, analysis_theories: Set[str]) -> str:
    """
    Convert an import token to HOL-Analysis.<Name> iff:
    - it refers to a theory in analysis_theories
    - it is not already qualified
    """
    raw = token.strip()
    quoted = raw.startswith('"') and raw.endswith('"')
    name = raw[1:-1] if quoted else raw

    # Already session-qualified or path-qualified: leave it alone
    if "." in name or "/" in name:
        return raw

    # Only rewrite local HOL/Analysis theories
    if name in analysis_theories:
        new_name = f'HOL-Analysis.{name}'
        return f'"{new_name}"'

    return raw


def rewrite_imports_block(imports_block: str, analysis_theories: Set[str]) -> str:
    """
    Rewrite only import tokens inside the imports block, preserving
    whitespace/comments as much as possible.
    """
    pieces = []
    last = 0

    for m in IMPORT_TOKEN_RE.finditer(imports_block):
        start, end = m.span()
        token = m.group(0)

        pieces.append(imports_block[last:start])

        # Avoid rewriting obvious keywords if they somehow appear here
        if token in {"keywords", "begin"}:
            pieces.append(token)
        else:
            pieces.append(normalize_import_token(token, analysis_theories))

        last = end

    pieces.append(imports_block[last:])
    return "".join(pieces)


def rewrite_theory_text(text: str, analysis_theories: Set[str]) -> str:
    """
    Rewrite the imports ... begin section of a theory file.
    """
    m = THEORY_HEADER_RE.search(text)
    if not m:
        return text

    prefix = m.group("prefix")
    imports = m.group("imports")
    suffix = m.group("suffix")

    new_imports = rewrite_imports_block(imports, analysis_theories)
    return text[:m.start()] + prefix + new_imports + suffix + text[m.end():]


def process_file(path: pathlib.Path, analysis_theories: Set[str], inplace: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    updated = rewrite_theory_text(original, analysis_theories)

    changed = (updated != original)
    if changed and inplace:
        path.write_text(updated, encoding="utf-8")
    return changed


def iter_thy_files(paths: Iterable[pathlib.Path]) -> Iterable[pathlib.Path]:
    for p in paths:
        if p.is_file() and p.suffix == ".thy":
            yield p
        elif p.is_dir():
            yield from p.rglob("*.thy")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite local HOL/Analysis imports to HOL-Analysis.<Theory>"
    )
    parser.add_argument(
        "--analysis-dir",
        required=True,
        type=pathlib.Path,
        help="Path to Isabelle src/HOL/Analysis directory",
    )
    parser.add_argument(
        "--target",
        required=True,
        nargs="+",
        type=pathlib.Path,
        help="Target .thy files or directories to process",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        help="Directory to write rewritten files to (if not inplace)",
    )
    parser.add_argument(
        "--inplace",
        action="store_true",
        help="Overwrite files in place. Without this flag, only print changed files.",
    )
    parser.add_argument(
        "--show-diff",
        action="store_true",
        help="Print rewritten content for changed files (dry-run style).",
    )
    args = parser.parse_args()

    analysis_dir: pathlib.Path = args.analysis_dir.resolve()
    analysis_theories = build_analysis_theory_set(analysis_dir)

    total = 0
    changed = 0

    for thy_file in iter_thy_files(args.target):
        total += 1
        original = thy_file.read_text(encoding="utf-8")
        updated = rewrite_theory_text(original, analysis_theories)

        if updated != original:
            changed += 1
            print(f"[CHANGED] {thy_file}")
            if args.inplace:
                thy_file.write_text(updated, encoding="utf-8")
            if args.output_dir and not args.inplace:
                output_path = args.output_dir / thy_file.name
                output_path.write_text(updated, encoding="utf-8")
                print(f"  -> Written to {output_path}")
            if args.show_diff and not args.inplace:
                print("----- rewritten content -----")
                print(updated)
                print("----- end -----")
            

    print(f"\nProcessed {total} theory file(s), changed {changed}.")


if __name__ == "__main__":
    main()