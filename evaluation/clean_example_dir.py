#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

# Only these document-style commands are removed.
DOC_COMMANDS = {
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "subsubsubsection",
    "paragraph",
    "subparagraph",
    "text",
    "text_raw",
}

HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")


def split_theory(text: str) -> tuple[str, str, str]:
    header_match = HEADER_RE.search(text)
    end_match = END_RE.search(text.strip())
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    stripped = text.strip()
    header = stripped[:header_match.end()].strip() + "\n"
    body = stripped[header_match.end():end_match.start()].strip()
    end_kw = stripped[end_match.start():end_match.end()].strip()
    return header, body, end_kw


def first_keyword(command: str) -> str | None:
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_']*)\b", command)
    return m.group(1) if m else None


def split_top_level_commands(body_text: str) -> list[str]:
    """
    Split a theory body into top-level Isabelle commands.

    This scanner is careful not to split on keywords that occur inside:
    - nested comments: (* ... *)
    - cartouches: \<open> ... \<close>
    - quoted strings: "..."
    - backtick strings: `...`

    It starts a new command whenever it encounters a word at column-insensitive
    top level after a newline. This is deliberately broader than the old
    TOP_LEVEL_KEYWORDS-based splitter, because we want to preserve commands like
    adhoc_overloading/value/thm/etc. while only removing the doc commands later.
    """
    s = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return []

    starts = [0]
    i = 0
    n = len(s)

    comment_depth = 0
    cartouche_depth = 0
    in_string = False
    in_backtick = False

    line_start = True

    def starts_word(pos: int) -> bool:
        if pos >= n:
            return False
        ch = s[pos]
        if not (ch.isalpha() or ch == "_"):
            return False
        prev = s[pos - 1] if pos > 0 else "\n"
        return not (prev.isalnum() or prev == "_" or prev == "'")

    while i < n:
        # Handle comment open/close first when not inside string/backtick.
        if not in_string and not in_backtick:
            if s.startswith("(*", i):
                comment_depth += 1
                line_start = False
                i += 2
                continue
            if comment_depth > 0 and s.startswith("*)", i):
                comment_depth -= 1
                line_start = False
                i += 2
                continue

        if comment_depth > 0:
            if s[i] == "\n":
                line_start = True
            elif not s[i].isspace():
                line_start = False
            i += 1
            continue

        # Cartouches are only tracked outside strings/backticks/comments.
        if not in_string and not in_backtick:
            if s.startswith(r"\<open>", i):
                cartouche_depth += 1
                line_start = False
                i += len(r"\<open>")
                continue
            if cartouche_depth > 0 and s.startswith(r"\<close>", i):
                cartouche_depth -= 1
                line_start = False
                i += len(r"\<close>")
                continue

        if cartouche_depth == 0:
            # Quoted strings
            if not in_backtick and s[i] == '"':
                escaped = i > 0 and s[i - 1] == "\\"
                if not escaped:
                    in_string = not in_string
                line_start = False
                i += 1
                continue

            # Backtick strings
            if not in_string and s[i] == "`":
                in_backtick = not in_backtick
                line_start = False
                i += 1
                continue

        # At top level, a word starting a fresh line starts a new command.
        if (
            comment_depth == 0
            and cartouche_depth == 0
            and not in_string
            and not in_backtick
            and line_start
            and starts_word(i)
            and i not in starts
        ):
            starts.append(i)

        ch = s[i]
        if ch == "\n":
            line_start = True
        elif not ch.isspace():
            line_start = False
        i += 1

    starts = sorted(set(starts))
    blocks: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else n
        block = s[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_theory(text: str) -> tuple[str, int, int]:
    header, body, end_kw = split_theory(text)
    commands = split_top_level_commands(body)

    kept: list[str] = []
    removed = 0
    for cmd in commands:
        kw = first_keyword(cmd)
        if kw in DOC_COMMANDS:
            removed += 1
            continue
        kept.append(cmd)

    out = header.rstrip() + "\n\n"
    if kept:
        out += "\n\n".join(kept).rstrip() + "\n\n"
    out += end_kw + "\n"
    return normalize_whitespace(out), len(commands), removed


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Remove only Isabelle document commands (text/section/subsection/etc.) from .thy files."
    )
    ap.add_argument("--corpus", type=Path, required=True, help="Directory containing original .thy files")
    ap.add_argument("--out-corpus", type=Path, required=True, help="Directory to write cleaned .thy files")
    ap.add_argument("--copy-non-thy", action="store_true", help="Copy non-.thy files and subdirectories to the output directory")
    args = ap.parse_args()

    corpus = args.corpus.resolve()
    out = args.out_corpus.resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.copy_non_thy:
        for item in corpus.iterdir():
            if item.suffix == ".thy":
                continue
            target = out / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

    reports = []
    for thy in sorted(corpus.glob("*.thy")):
        text = thy.read_text(encoding="utf-8")
        cleaned, total_cmds, removed_cmds = clean_theory(text)
        (out / thy.name).write_text(cleaned, encoding="utf-8")
        reports.append(
            {
                "file": str(thy),
                "total_top_level_commands": total_cmds,
                "removed_doc_commands": removed_cmds,
                "kept_commands": total_cmds - removed_cmds,
            }
        )

    report_path = out / "cleaning_report.txt"
    lines = [
        f"files={len(reports)}",
        f"removed_doc_commands_total={sum(r['removed_doc_commands'] for r in reports)}",
        "",
    ]
    for r in reports:
        lines.append(
            f"{Path(r['file']).name}: kept={r['kept_commands']} removed_doc={r['removed_doc_commands']} total={r['total_top_level_commands']}"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote cleaned theories to: {out}")
    print(f"Wrote report to: {report_path}")


if __name__ == "__main__":
    main()
