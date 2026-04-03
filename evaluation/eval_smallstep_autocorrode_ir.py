from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

SENTINEL = "<>"
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
THEORY_RE = re.compile(r"\btheory\s+([A-Za-z0-9_'.-]+)\b")
HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")
IMPORTS_RE = re.compile(r"(?s)\bimports\b(.*?)\bbegin\b")
IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
SUBGOAL_RE = re.compile(r"goal \((\d+) subgoals?\):")


@dataclass
class StepResult:
    step_kind: str
    preview: str
    accepted: bool
    elapsed_sec: float
    open_subgoals: Optional[int]
    proof_done: Optional[bool]
    state_preview: Optional[str] = None
    error: Optional[str] = None


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    startup_sec: float
    ok: bool
    total_steps: int
    accepted_steps: int
    steps: list[StepResult] = field(default_factory=list)


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


def split_theory(text: str) -> tuple[list[str], str]:
    stripped = text.strip()
    header_match = HEADER_RE.search(stripped)
    end_match = END_RE.search(stripped)
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    body = stripped[header_match.end():end_match.start()].strip()
    return split_top_level_blocks(body), stripped[end_match.start():end_match.end()].strip()


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


def preview(text: str, n: int = 100) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def ml_str(s: str) -> str:
    return '"' + (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    ) + '"'


def ml_list_str(items: list[str]) -> str:
    return "[" + ", ".join(ml_str(x) for x in items) + "]"


class IRServer:
    def __init__(
        self,
        *,
        ir_root: Path,
        isabelle_bin: str,
        session_name: str,
        session_dir: Optional[Path],
        port: int,
        token: str,
        connect_timeout_sec: float,
    ) -> None:
        self.ir_root = ir_root
        self.isabelle_bin = isabelle_bin
        self.session_name = session_name
        self.session_dir = session_dir
        self.port = port
        self.token = token
        self.connect_timeout_sec = connect_timeout_sec
        self.proc: Optional[subprocess.Popen[str]] = None
        self.log_lines: deque[str] = deque(maxlen=200)
        self._reader_thread: Optional[threading.Thread] = None

    def start(self) -> float:
        cmd = [
            sys.executable,
            str(self.ir_root / "ir" / "repl.py"),
            "--port",
            str(self.port),
            "--isabelle",
            self.isabelle_bin,
            "--session",
            self.session_name,
        ]
        if self.session_dir is not None:
            cmd.extend(["--dir", str(self.session_dir)])

        env = os.environ.copy()
        env["IR_AUTH_TOKEN"] = self.token
        env["PYTHONUNBUFFERED"] = "1"

        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.ir_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert self.proc.stdout is not None
        self._reader_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._reader_thread.start()
        self._wait_until_ready()
        return time.perf_counter() - t0

    def _drain_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log_lines.append(line.rstrip("\n"))

    def _wait_until_ready(self) -> None:
        deadline = time.time() + self.connect_timeout_sec
        last_error = ""
        while time.time() < deadline:
            if self.proc is None:
                break
            rc = self.proc.poll()
            if rc is not None:
                raise RuntimeError(
                    f"I/R exited early with code {rc}. Recent log tail:\n" + "\n".join(self.log_lines)
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.25)
        raise TimeoutError(
            f"Timed out waiting for I/R on 127.0.0.1:{self.port}. Last socket error: {last_error}\n"
            + "Recent log tail:\n"
            + "\n".join(self.log_lines)
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            self.proc = None

    def send(self, command: str, timeout_sec: float = 30.0) -> str:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout_sec) as sock:
            sock.settimeout(timeout_sec)
            sock.sendall((self.token + "\n").encode("utf-8"))
            auth = self._recv_until_newline(sock)
            if not auth.startswith("OK"):
                raise RuntimeError(f"I/R auth failed: {auth.strip()}")

            cmd = command.strip()
            if not cmd.endswith(";") and not cmd.startswith("/"):
                cmd += ";"
            sock.sendall((cmd + "\n").encode("utf-8"))

            buf = ""
            while SENTINEL not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise EOFError("Connection closed by I/R before sentinel")
                buf += chunk.decode("utf-8", errors="replace")

            raw = buf[: buf.index(SENTINEL)].strip()
            if raw.startswith("ERR\n"):
                raise RuntimeError(raw[4:].strip())
            return raw

    @staticmethod
    def _recv_until_newline(sock: socket.socket) -> str:
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(1024)
            if not chunk:
                raise EOFError("Connection closed during authentication")
            buf += chunk
        return buf.decode("utf-8", errors="replace")


def parse_state_metrics(text: str) -> tuple[Optional[int], Optional[bool]]:
    stripped = text.strip()
    m = SUBGOAL_RE.search(stripped)
    if m:
        n = int(m.group(1))
        return n, n == 0
    if "No subgoals" in stripped or stripped.startswith("theorem ") or stripped.startswith("lemma "):
        return 0, True
    if stripped.startswith("proof") or stripped.startswith("goal"):
        return None, False
    return None, None


def ir_init_command(repl_id: str, imports: list[str]) -> str:
    return f'Ir.init {ml_str(repl_id)} {ml_list_str(imports)}'


def ir_step_command(repl_id: str, isar_text: str) -> str:
    return f'Ir.step {ml_str(repl_id)} {ml_str(isar_text)}'


def ir_state_command(repl_id: str, idx: int = -1) -> str:
    idx_text = f"~{-idx}" if idx < 0 else str(idx)
    return f'Ir.state {ml_str(repl_id)} {idx_text}'


def ir_remove_command(repl_id: str) -> str:
    return f'Ir.remove {ml_str(repl_id)}'


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Small-step evaluator for AutoCorrode I/R (Isabelle/REPL)."
    )
    ap.add_argument("--ir-root", required=True, type=Path, help="Path to AutoCorrode repo root")
    ap.add_argument("--corpus", required=True, type=Path, help="Directory containing .thy files")
    ap.add_argument("--isabelle-bin", default="isabelle", help="Path to Isabelle binary")
    ap.add_argument("--session-name", default="HOL", help="Isabelle session to load into I/R")
    ap.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help="Optional directory to pass as --dir to I/R (e.g. AutoCorrode repo root for session AutoCorrode)",
    )
    ap.add_argument("--port", type=int, default=9147)
    ap.add_argument("--token", default="isabellegym-eval-token")
    ap.add_argument("--connect-timeout", type=float, default=120.0)
    ap.add_argument("--step-timeout", type=float, default=60.0)
    ap.add_argument("--output", type=Path, default=Path("smallstep_autocorrode_ir_results.json"))
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    server = IRServer(
        ir_root=args.ir_root,
        isabelle_bin=args.isabelle_bin,
        session_name=args.session_name,
        session_dir=args.session_dir,
        port=args.port,
        token=args.token,
        connect_timeout_sec=args.connect_timeout,
    )

    results: list[TheoryResult] = []
    startup = server.start()

    try:
        for index, thy_file in enumerate(files, start=1):
            text = thy_file.read_text(encoding="utf-8")
            m = THEORY_RE.search(text)
            if not m:
                continue
            theory_name = m.group(1)
            imports = extract_imports(text)
            blocks, _end_kw = split_theory(text)
            repl_id = f"eval_{index}_{theory_name}"

            step_results: list[StepResult] = []
            ok = True
            try:
                t_init = time.perf_counter()
                server.send(ir_init_command(repl_id, imports), timeout_sec=args.step_timeout)
                server.send(f'Ir.timeout {ml_str(repl_id)} {int(args.step_timeout)}', timeout_sec=args.step_timeout)
                elapsed_init = time.perf_counter() - t_init
                step_results.append(
                    StepResult(
                        "init",
                        preview(f"imports {imports}"),
                        True,
                        elapsed_init,
                        None,
                        None,
                    )
                )

                for block in blocks:
                    t1 = time.perf_counter()
                    state_text = None
                    open_subgoals, proof_done = None, None
                    state_probe_error = None
                    try:
                        server.send(ir_step_command(repl_id, block), timeout_sec=args.step_timeout)
                        accepted = True
                        err = None
                        try:
                            state_text = server.send(ir_state_command(repl_id, -1), timeout_sec=args.step_timeout)
                            open_subgoals, proof_done = parse_state_metrics(state_text)
                        except Exception as exc:
                            state_probe_error = str(exc)
                    except Exception as exc:
                        accepted = False
                        err = str(exc)
                    elapsed = time.perf_counter() - t1
                    if err is None and state_probe_error is not None:
                        err = f"state probe failed: {state_probe_error}"
                    step_results.append(
                        StepResult(
                            "body",
                            preview(block),
                            accepted,
                            elapsed,
                            open_subgoals,
                            proof_done,
                            preview(state_text or "") if state_text else None,
                            err,
                        )
                    )
                    if not accepted:
                        ok = False
                        break
            finally:
                try:
                    server.send(ir_remove_command(repl_id), timeout_sec=min(args.step_timeout, 10.0))
                except Exception:
                    pass

            results.append(
                TheoryResult(
                    file=str(thy_file),
                    theory_name=theory_name,
                    startup_sec=startup,
                    ok=ok,
                    total_steps=len(step_results),
                    accepted_steps=sum(1 for s in step_results if s.accepted),
                    steps=step_results,
                )
            )
    finally:
        server.stop()

    payload = {
        "tool": "autocorrode-ir-smallstep",
        "ir_root": str(args.ir_root),
        "session_name": args.session_name,
        "session_dir": str(args.session_dir) if args.session_dir else None,
        "corpus": str(args.corpus),
        "note": (
            "I/R does not accept theory headers as small-step commands. Each theory is evaluated as "
            "Ir.init(imports) followed by replay of body blocks only."
        ),
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
