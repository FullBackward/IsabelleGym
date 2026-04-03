from __future__ import annotations

import re
from typing import List

TOP_LEVEL_KEYWORDS = (
    "lemma",
    "theorem",
    "corollary",
    "proposition",
    "schematic_goal",
    "definition",
    "fun",
    "function",
    "primrec",
    "inductive",
    "inductive_set",
    "coinductive",
    "abbreviation",
    "notation",
    "no_notation",
    "declare",
    "context",
    "locale",
    "interpretation",
    "instantiation",
    "lift_definition",
    "datatype",
    "codatatype",
    "record",
    "typedef",
    "class",
    "instance",
    "text",
    "text_raw",
    "ML",
    "ML_file",
    "SML_export",
    "setup",
    "method_setup",
    "termination",
    "end",
)

PROOF_OPENERS = (
    "proof",
    "proof -",
    "proof (",
)

PROOF_CLOSERS = (
    "qed",
    "by",
    "done",
    "oops",
    "sorry",
)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _starts_with_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    stripped = line.strip()
    return any(re.match(rf"^{re.escape(keyword)}(\b|\s|\(|$)", stripped) for keyword in keywords)


def split_theory_into_blocks(theory_text: str) -> List[str]:
    text = _normalize(theory_text).strip()
    if not text:
        return []

    blocks: List[str] = []
    current: List[str] = []
    proof_depth = 0

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        starts_new_block = (
            bool(current)
            and proof_depth == 0
            and stripped != ""
            and _starts_with_keyword(stripped, TOP_LEVEL_KEYWORDS)
        )

        if starts_new_block:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []

        current.append(line)

        if _starts_with_keyword(stripped, PROOF_OPENERS):
            proof_depth += 1
        elif _starts_with_keyword(stripped, PROOF_CLOSERS):
            proof_depth = max(0, proof_depth - 1)

    block = "\n".join(current).strip()
    if block:
        blocks.append(block)

    return blocks


def split_block_into_chunks(block_text: str, max_lines: int = 40) -> List[str]:
    text = _normalize(block_text).strip()
    if not text:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: List[str] = []

    for paragraph in paragraphs:
        lines = paragraph.split("\n")
        if len(lines) <= max_lines:
            chunks.append(paragraph)
            continue

        for start in range(0, len(lines), max_lines):
            chunk = "\n".join(lines[start : start + max_lines]).strip()
            if chunk:
                chunks.append(chunk)

    return chunks or [text]


def preview_text(text: str, max_len: int = 160) -> str:
    one_line = " ".join(_normalize(text).split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."
