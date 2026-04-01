#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

TOP_LEVEL_KEYWORDS = (
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    "definition", "fun", "function", "primrec", "inductive", "inductive_set",
    "coinductive", "abbreviation", "notation", "no_notation", "declare",
    "context", "locale", "interpretation", "instantiation", "lift_definition",
    "datatype", "codatatype", "record", "typedef", "class", "instance",
    "text", "text_raw", "ML", "ML_file", "SML_export", "setup",
    "method_setup", "termination", "end",
)
PROOF_OPENERS = ("proof", "proof -", "proof (")
PROOF_CLOSERS = ("qed", "by", "done", "oops", "sorry")
THEORY_RE = re.compile(r'(?m)^[ \t]*theory[ \t]+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))\b')
HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")
IMPORTS_RE = re.compile(r"(?s)\bimports\b(.*?)\bbegin\b")
IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
PREBEGIN_KEYWORDS_RE = re.compile(r"(?ms)^\s*keywords\b")


@dataclass
class StepResult:
    step_kind: str
    preview: str
    accepted: bool
    elapsed_sec: float
    proof_done: Optional[bool]
    error: Optional[str] = None


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    startup_sec: float
    ok: bool
    total_steps: int
    accepted_steps: int
    error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _starts_with_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    stripped = line.strip()
    return any(re.match(rf"^{re.escape(keyword)}(\b|\s|\(|$)", stripped) for keyword in keywords)


def split_top_level_blocks(body_text: str) -> list[str]:
    text = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    blocks: list[str] = []
    current: list[str] = []
    proof_depth = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        starts_new_block = bool(current) and proof_depth == 0 and stripped != "" and _starts_with_keyword(stripped, TOP_LEVEL_KEYWORDS)
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


def split_theory(text: str) -> tuple[str, list[str], str]:
    stripped = text.strip()
    header_match = HEADER_RE.search(stripped)
    end_match = END_RE.search(stripped)
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    header = stripped[:header_match.end()].strip()
    body = stripped[header_match.end():end_match.start()].strip()
    return header, split_top_level_blocks(body), stripped[end_match.start():end_match.end()].strip()


def extract_imports(text: str) -> list[str]:
    m = IMPORTS_RE.search(text)
    if not m:
        return ["Main"]
    out: list[str] = []
    for token in IMPORT_TOKEN_RE.findall(m.group(1)):
        token = token.strip().strip('"')
        if token and token not in {"imports", "begin"}:
            out.append(token)
    return out or ["Main"]


def header_requires_features_not_supported_by_new_theory(header: str) -> Optional[str]:
    # qIsabelle newTheory only models: theory <name> imports ... begin
    # It cannot express extra header declarations like custom `keywords`.
    if PREBEGIN_KEYWORDS_RE.search(header):
        return "qIsabelle new_theory() cannot reproduce header `keywords` declarations before `begin`"
    return None


def preview(text: str, n: int = 100) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qisabelle-root", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--session-name", default="HOL")
    ap.add_argument("--session-root", action="append", default=[])
    ap.add_argument("--master-dir", type=Path, default=None)
    ap.add_argument("--port", type=int, default=17000)
    ap.add_argument("--only-import-from-session-heap", action="store_true")
    ap.add_argument("--output", type=Path, default=Path("smallstep_qisabelle_results.json"))
    args = ap.parse_args()

    sys.path.insert(0, str(args.qisabelle_root))
    from client.session import QIsabelleSession

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    results: list[TheoryResult] = []
    t0 = time.perf_counter()
    session_roots = [Path(p).resolve() for p in args.session_root]
    master_dir = (args.master_dir.resolve() if args.master_dir else args.corpus.resolve())

    with QIsabelleSession(session_name=args.session_name, session_roots=session_roots, port=args.port) as session:
        startup = time.perf_counter() - t0

        for thy_file in files:
            text = thy_file.read_text(encoding="utf-8")
            theory_name = extract_theory_name(text)
            if not theory_name:
                results.append(
                    TheoryResult(
                        file=str(thy_file),
                        theory_name=thy_file.stem,
                        startup_sec=startup,
                        ok=False,
                        total_steps=0,
                        accepted_steps=0,
                        error="Could not extract theory name",
                    )
                )
                continue

            header, blocks, end_kw = split_theory(text)
            unsupported = header_requires_features_not_supported_by_new_theory(header)
            if unsupported:
                results.append(
                    TheoryResult(
                        file=str(thy_file),
                        theory_name=theory_name,
                        startup_sec=startup,
                        ok=False,
                        total_steps=0,
                        accepted_steps=0,
                        error=unsupported,
                    )
                )
                continue

            imports = extract_imports(text)
            step_results: list[StepResult] = []
            ok = True
            theory_error: Optional[str] = None
            try:
                session.new_theory(
                    theory_name=theory_name,
                    new_state_name=f"{theory_name}_0",
                    imports=imports,
                    master_dir=master_dir,
                    only_import_from_session_heap=args.only_import_from_session_heap,
                )
                current_state = f"{theory_name}_0"
                next_idx = 1

                for kind, command in [("body", b + "\n") for b in blocks] + [("end", end_kw)]:
                    next_state = f"{theory_name}_{next_idx}"
                    next_idx += 1
                    t1 = time.perf_counter()
                    try:
                        proof_done, _goals = session.execute(current_state, command, next_state)
                        elapsed = time.perf_counter() - t1
                        accepted = True
                        err = None
                        current_state = next_state
                    except Exception as exc:
                        elapsed = time.perf_counter() - t1
                        proof_done = None
                        accepted = False
                        err = str(exc)
                        theory_error = err

                    step_results.append(
                        StepResult(kind, preview(command), accepted, elapsed, proof_done, err)
                    )
                    if not accepted:
                        ok = False
                        break
            except Exception as exc:
                ok = False
                theory_error = str(exc)

            results.append(
                TheoryResult(
                    file=str(thy_file),
                    theory_name=theory_name,
                    startup_sec=startup,
                    ok=ok,
                    total_steps=len(step_results),
                    accepted_steps=sum(1 for s in step_results if s.accepted),
                    error=theory_error,
                    steps=step_results,
                )
            )

    payload = {
        "tool": "qisabelle",
        "corpus": str(args.corpus),
        "session_name": args.session_name,
        "session_roots": [str(p) for p in session_roots],
        "master_dir": str(master_dir),
        "theories_attempted": len(results),
        "theories_completed": sum(1 for r in results if r.ok),
        "results": [
            {
                **asdict(r),
                "steps": [asdict(s) for s in r.steps],
            }
            for r in results
        ],
    }
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
