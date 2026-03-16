#!/usr/bin/env python3
r"""
Small-step verification benchmark runner for three Isabelle backends:

1. QIsabelle
2. IsabelleGym
3. Isabelle Server (via the Python package `isabelle-client`)

The runner reads a JSON manifest describing proof cases and emits JSON with
per-step timings and success/failure information.

Manifest format
===============
{
  "cases": [
    {
      "name": "imp_refl",
      "theory_name": "Bench_Imp_Refl",
      "imports": ["Main"],
      "lemma": "lemma imp_refl: \"A \\\<longrightarrow> A\"",
      "steps": ["by assumption"]
    },
    {
      "name": "conj_comm",
      "theory_name": "Bench_Conj_Comm",
      "imports": ["Main"],
      "lemma": "lemma conj_comm: \"A \\\<and> B \\\<Longrightarrow> B \\\<and> A\"",
      "steps": [
        "apply (rule conjI)",
        "apply assumption",
        "apply assumption",
        "done"
      ]
    }
  ]
}

Typical usage
=============
QIsabelle:
    python small_step_bench_isabelle.py \
      --backend qisabelle \
      --manifest cases.json \
      --out qisabelle_results.json \
      --qisabelle-root /path/to/qisabelle \
      --qisabelle-port 17000 \
      --session HOL

IsabelleGym:
    python small_step_bench_isabelle.py \
      --backend isabelle-gym \
      --manifest cases.json \
      --out gym_results.json \
      --gym-root /path/to/FullBackward-IsabelleGym

Isabelle Server:
    python small_step_bench_isabelle.py \
      --backend isabelle-server \
      --manifest cases.json \
      --out server_results.json \
      --server-host localhost \
      --server-port 8000 \
      --server-password "$ISABELLE_SERVER_PASSWORD" \
      --server-session HOL
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


# -----------------------------
# Data model
# -----------------------------


@dataclass
class BenchmarkCase:
    name: str
    theory_name: str
    imports: list[str]
    lemma: str
    steps: list[str]


@dataclass
class StepResult:
    index: int
    kind: str  # lemma | proof_step
    code: str
    accepted: bool
    elapsed_seconds: float
    proof_done: Optional[bool] = None
    proof_state: Optional[str] = None
    goals: list[str] = field(default_factory=list)
    console_lines: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CaseResult:
    case_name: str
    theory_name: str
    backend: str
    ok: bool
    startup_seconds: float
    case_seconds: float
    exact_final_check_supported: bool
    exact_final_check_ok: Optional[bool] = None
    exact_final_check_seconds: Optional[float] = None
    exact_final_check_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


def load_manifest(path: Path) -> list[BenchmarkCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data or not isinstance(data["cases"], list):
        raise ValueError("Manifest must be a JSON object with a 'cases' list.")

    cases: list[BenchmarkCase] = []
    for raw in data["cases"]:
        if not isinstance(raw, dict):
            raise ValueError("Each case must be a JSON object.")
        cases.append(
            BenchmarkCase(
                name=str(raw["name"]),
                theory_name=str(raw.get("theory_name", raw["name"])),
                imports=[str(x) for x in raw.get("imports", ["Main"])],
                lemma=str(raw["lemma"]),
                steps=[str(x) for x in raw.get("steps", [])],
            )
        )
    return cases


# -----------------------------
# Helpers
# -----------------------------


class BenchmarkError(RuntimeError):
    pass


class ImportErrorWithHint(BenchmarkError):
    pass


class Backend:
    backend_name: str

    def start(self) -> float:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def run_case(self, case: BenchmarkCase, startup_seconds: float) -> CaseResult:
        raise NotImplementedError


def sanitize_theory_name(name: str) -> str:
    out = []
    for ch in name:
        out.append(ch if ch.isalnum() or ch == "_" else "_")
    cleaned = "".join(out).strip("_")
    return cleaned or f"Bench_{uuid.uuid4().hex[:8]}"


def import_module_from_file(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportErrorWithHint(f"Could not load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_theory_text(
    theory_name: str,
    imports: list[str],
    body_lines: Iterable[str],
    *,
    append_sorry: bool = False,
    append_print_state: bool = False,
    append_sexpr_ml: bool = False,
) -> str:
    lines = [
        f"theory {theory_name}",
        "imports " + " ".join(imports),
        "begin",
        "",
    ]
    lines.extend(body_lines)
    if append_print_state:
        lines.append("print_state")
    if append_sexpr_ml:
        lines.append('ML_val "List.map to_sexpr_untyped (Thm.prems_of (#goal @{Isar.goal}))"')
    if append_sorry:
        lines.append("sorry")
    lines.extend(["", "end", ""])
    return "\n".join(lines)


def try_model_dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return repr(obj)
    return obj


def format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# -----------------------------
# QIsabelle backend
# -----------------------------


class QIsabelleBackend(Backend):
    backend_name = "qisabelle"

    def __init__(self, root: Path, port: int, session_name: str, session_roots: list[Path]):
        self.root = root
        self.port = port
        self.session_name = session_name
        self.session_roots = session_roots
        self.session: Any = None

    def start(self) -> float:
        sys.path.insert(0, str(self.root))
        t0 = time.perf_counter()
        try:
            mod = importlib.import_module("client.session")
        except Exception as exc:
            raise ImportErrorWithHint(
                "Could not import QIsabelle. Point --qisabelle-root at the cloned qisabelle repo root."
            ) from exc
        QIsabelleSession = getattr(mod, "QIsabelleSession")
        self.session = QIsabelleSession(
            session_name=self.session_name,
            session_roots=self.session_roots,
            port=self.port,
            debug=False,
        )
        return time.perf_counter() - t0

    def stop(self) -> None:
        if self.session is not None:
            try:
                self.session.__exit__(None, None, None)
            finally:
                self.session = None

    def run_case(self, case: BenchmarkCase, startup_seconds: float) -> CaseResult:
        if self.session is None:
            raise BenchmarkError("QIsabelle session is not initialized.")

        result = CaseResult(
            case_name=case.name,
            theory_name=case.theory_name,
            backend=self.backend_name,
            ok=False,
            startup_seconds=startup_seconds,
            case_seconds=0.0,
            exact_final_check_supported=False,
        )
        theory_name = sanitize_theory_name(case.theory_name)
        t_case = time.perf_counter()

        try:
            self.session.forget_all_states()
        except Exception:
            pass

        state = f"{theory_name}_s0"
        try:
            self.session.new_theory(
                theory_name=theory_name,
                new_state_name=state,
                imports=case.imports,
                only_import_from_session_heap=False,
            )
        except Exception as exc:
            result.case_seconds = time.perf_counter() - t_case
            result.steps.append(
                StepResult(
                    index=0,
                    kind="lemma",
                    code=case.lemma,
                    accepted=False,
                    elapsed_seconds=0.0,
                    error=format_exc(exc),
                )
            )
            return result

        lines = [case.lemma] + case.steps
        for i, line in enumerate(lines):
            kind = "lemma" if i == 0 else "proof_step"
            new_state = f"{theory_name}_s{i + 1}"
            t0 = time.perf_counter()
            try:
                proof_done, proof_state = self.session.execute(state, line, new_state)
                elapsed = time.perf_counter() - t0
                result.steps.append(
                    StepResult(
                        index=i,
                        kind=kind,
                        code=line,
                        accepted=True,
                        elapsed_seconds=elapsed,
                        proof_done=bool(proof_done),
                        proof_state=proof_state,
                    )
                )
                state = new_state
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                result.steps.append(
                    StepResult(
                        index=i,
                        kind=kind,
                        code=line,
                        accepted=False,
                        elapsed_seconds=elapsed,
                        error=format_exc(exc),
                    )
                )
                result.case_seconds = time.perf_counter() - t_case
                result.ok = False
                return result

        result.case_seconds = time.perf_counter() - t_case
        result.ok = bool(result.steps) and all(s.accepted for s in result.steps) and bool(result.steps[-1].proof_done)
        return result


# -----------------------------
# IsabelleGym backend
# -----------------------------


class IsabelleGymBackend(Backend):
    backend_name = "isabelle-gym"

    def __init__(self, gym_root: Path):
        self.gym_root = gym_root
        self.gym: Any = None
        self._sys_path_inserted = False

    def start(self) -> float:
        if not self.gym_root.exists():
            raise BenchmarkError(f"--gym-root does not exist: {self.gym_root}")

        t0 = time.perf_counter()
        sys.path.insert(0, str(self.gym_root))
        self._sys_path_inserted = True
        try:
            mod = importlib.import_module("gym.isabelle_gym")
        except Exception as exc:
            raise ImportErrorWithHint(
                "Could not import FullBackward IsabelleGym. Point --gym-root at the cloned repo root, "
                "or install it with: pip install -e /path/to/IsabelleGym"
            ) from exc

        IsabelleGym = getattr(mod, "IsabelleGym")
        self.gym = IsabelleGym(show_states=False)
        return time.perf_counter() - t0

    def stop(self) -> None:
        try:
            if self.gym is not None:
                self.gym.close()
        finally:
            self.gym = None
            if self._sys_path_inserted:
                try:
                    sys.path.remove(str(self.gym_root))
                except ValueError:
                    pass
                self._sys_path_inserted = False

    def _header_line(self, theory_name: str, imports: list[str]) -> str:
        return f"theory {theory_name} imports {' '.join(imports)} begin"

    def _extract_result_text(self, repl_result: Any) -> tuple[list[str], list[str], str]:
        output_text = ""
        error_text = ""
        total_output = ""

        try:
            separated = repl_result.separated_output()
            output_text = separated.output() or ""
            error_text = separated.error() or ""
        except Exception:
            pass

        try:
            total_output = repl_result.total_output() or ""
        except Exception:
            pass

        console_lines: list[str] = []
        if output_text.strip():
            console_lines.extend([line for line in output_text.splitlines() if line.strip()])
        elif total_output.strip():
            console_lines.extend([line for line in total_output.splitlines() if line.strip()])

        error_lines = [line for line in error_text.splitlines() if line.strip()]
        if not error_lines and total_output.strip():
            for line in total_output.splitlines():
                if line.strip().startswith("***"):
                    error_lines.append(line.strip())

        return console_lines, error_lines, total_output

    def _reset_and_open_theory(self, theory_name: str, imports: list[str]) -> tuple[bool, Optional[str], list[str]]:
        if self.gym is None:
            raise BenchmarkError("IsabelleGym is not initialized.")
        self.gym.reset()
        self.gym.enter_thy(theory_name)
        header_result = self.gym.step(self._header_line(theory_name, imports))
        console_lines, error_lines, total_output = self._extract_result_text(header_result)
        if error_lines:
            return False, "\n".join(error_lines), console_lines
        if total_output.strip().startswith("***"):
            return False, total_output, console_lines
        return True, None, console_lines

    def _run_exact_replay(self, theory_name: str, imports: list[str], lines: list[str]) -> tuple[bool, float, Optional[str]]:
        t0 = time.perf_counter()
        try:
            ok, err, _ = self._reset_and_open_theory(theory_name, imports)
            if not ok:
                return False, time.perf_counter() - t0, err
            for line in lines:
                repl_result = self.gym.step(line)
                _console_lines, error_lines, total_output = self._extract_result_text(repl_result)
                if error_lines:
                    return False, time.perf_counter() - t0, "\n".join(error_lines)
                if total_output.strip().startswith("***"):
                    return False, time.perf_counter() - t0, total_output
            proof_done = False
            try:
                proof_done = bool(self.gym.proof_finished())
            except Exception:
                pass
            return proof_done, time.perf_counter() - t0, None if proof_done else "Proof not finished after replay"
        except Exception as exc:
            return False, time.perf_counter() - t0, format_exc(exc)

    def run_case(self, case: BenchmarkCase, startup_seconds: float) -> CaseResult:
        if self.gym is None:
            raise BenchmarkError("IsabelleGym is not initialized.")

        theory_name = sanitize_theory_name(case.theory_name) + "_" + uuid.uuid4().hex[:6]
        result = CaseResult(
            case_name=case.name,
            theory_name=theory_name,
            backend=self.backend_name,
            ok=False,
            startup_seconds=startup_seconds,
            case_seconds=0.0,
            exact_final_check_supported=True,
        )

        t_case = time.perf_counter()
        try:
            header_ok, header_err, header_lines = self._reset_and_open_theory(theory_name, case.imports)
            if not header_ok:
                result.steps.append(
                    StepResult(
                        index=-1,
                        kind="theory_header",
                        code=self._header_line(theory_name, case.imports),
                        accepted=False,
                        elapsed_seconds=0.0,
                        console_lines=header_lines,
                        error=header_err,
                    )
                )
                result.case_seconds = time.perf_counter() - t_case
                return result

            all_lines = [case.lemma] + case.steps
            for i, line in enumerate(all_lines):
                kind = "lemma" if i == 0 else "proof_step"
                t0 = time.perf_counter()
                try:
                    repl_result = self.gym.step(line)
                    elapsed = time.perf_counter() - t0
                    console_lines, error_lines, total_output = self._extract_result_text(repl_result)
                    goals: list[str] = []
                    proof_done = None
                    try:
                        goals = list(self.gym.open_subgoals())
                        proof_done = bool(self.gym.proof_finished())
                    except Exception:
                        pass
                    accepted = not error_lines and not total_output.strip().startswith("***")
                    result.steps.append(
                        StepResult(
                            index=i,
                            kind=kind,
                            code=line,
                            accepted=accepted,
                            elapsed_seconds=elapsed,
                            proof_done=proof_done,
                            proof_state="\n".join(goals) if goals else None,
                            goals=goals,
                            console_lines=console_lines,
                            error=None if accepted else "\n".join(error_lines) or total_output,
                        )
                    )
                    if not accepted:
                        result.case_seconds = time.perf_counter() - t_case
                        return result
                except Exception as exc:
                    elapsed = time.perf_counter() - t0
                    result.steps.append(
                        StepResult(
                            index=i,
                            kind=kind,
                            code=line,
                            accepted=False,
                            elapsed_seconds=elapsed,
                            error=format_exc(exc),
                        )
                    )
                    result.case_seconds = time.perf_counter() - t_case
                    return result

            exact_ok, exact_secs, exact_err = self._run_exact_replay(
                theory_name + "_exact", case.imports, all_lines
            )
            result.exact_final_check_ok = exact_ok
            result.exact_final_check_seconds = exact_secs
            result.exact_final_check_error = exact_err
            result.case_seconds = time.perf_counter() - t_case
            result.ok = all(s.accepted for s in result.steps) and bool(result.exact_final_check_ok)
            return result
        finally:
            try:
                self.gym.reset()
            except Exception:
                pass


# -----------------------------
# Isabelle Server backend
# -----------------------------


class IsabelleServerBackend(Backend):
    backend_name = "isabelle-server"

    def __init__(self, host: str, port: int, password: str, session: str):
        self.host = host
        self.port = port
        self.password = password
        self.session_name = session
        self.client: Any = None
        self.session_id: Optional[str] = None
        self.workdir = Path(tempfile.mkdtemp(prefix="isabelle_server_small_step_"))

    def start(self) -> float:
        if not self.password:
            raise BenchmarkError(
                "Isabelle Server requires a password. Pass --server-password or set ISABELLE_SERVER_PASSWORD."
            )

        t0 = time.perf_counter()
        try:
            client_mod = importlib.import_module("isabelle_client")
        except Exception as exc:
            raise ImportErrorWithHint(
                "Could not import isabelle-client. Install it with: pip install isabelle-client"
            ) from exc

        IsabelleClient = getattr(client_mod, "IsabelleClient")
        self.client = IsabelleClient(self.host, self.port, self.password)
        responses = self.client.session_start(session=self.session_name)
        final = responses[-1]
        session_id = getattr(getattr(final, "response_body", None), "session_id", None)
        if not session_id:
            raise BenchmarkError(f"Could not start Isabelle server session: {responses!r}")
        self.session_id = str(session_id)
        return time.perf_counter() - t0

    def stop(self) -> None:
        try:
            if self.client is not None and self.session_id is not None:
                try:
                    self.client.session_stop(self.session_id)
                except Exception:
                    pass
        finally:
            self.client = None
            self.session_id = None
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _extract_messages(self, responses: list[Any]) -> list[str]:
        msgs: list[str] = []
        for resp in responses:
            body = getattr(resp, "response_body", None)
            body = try_model_dump(body)
            if isinstance(body, dict):
                if "message" in body and isinstance(body["message"], str):
                    msgs.append(body["message"])
                if "nodes" in body and isinstance(body["nodes"], list):
                    for node in body["nodes"]:
                        if isinstance(node, dict):
                            for msg in node.get("messages", []):
                                if isinstance(msg, dict) and isinstance(msg.get("message"), str):
                                    msgs.append(msg["message"])
            elif isinstance(body, str):
                msgs.append(body)
        return msgs

    def _response_ok(self, responses: list[Any]) -> tuple[bool, Optional[str], list[str]]:
        msgs = self._extract_messages(responses)
        if not responses:
            return False, "Empty response from Isabelle server.", msgs
        final = responses[-1]
        rtype = getattr(getattr(final, "response_type", None), "value", None) or str(getattr(final, "response_type", ""))
        body = try_model_dump(getattr(final, "response_body", None))
        if rtype == "FINISHED":
            if isinstance(body, dict):
                ok = body.get("ok", True)
                if ok is False:
                    return False, json.dumps(body, ensure_ascii=False), msgs
            return True, None, msgs
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return False, body["message"], msgs
        return False, repr(body), msgs

    def _write_theory(self, theory_name: str, imports: list[str], body_lines: list[str], *, append_sorry: bool) -> Path:
        path = self.workdir / f"{theory_name}.thy"
        text = make_theory_text(theory_name, imports, body_lines, append_sorry=append_sorry)
        path.write_text(text, encoding="utf-8")
        return path

    def _purge_all(self) -> None:
        if self.client is None or self.session_id is None:
            return
        try:
            self.client.purge_theories(self.session_id, theories=[], master_dir=str(self.workdir), purge_all=True)
        except Exception:
            pass

    def _run_theory(self, theory_name: str) -> tuple[bool, float, Optional[str], list[str]]:
        if self.client is None or self.session_id is None:
            raise BenchmarkError("Isabelle server session is not initialized.")
        t0 = time.perf_counter()
        responses = self.client.use_theories(
            session_id=self.session_id,
            theories=[theory_name],
            master_dir=str(self.workdir),
        )
        elapsed = time.perf_counter() - t0
        ok, err, msgs = self._response_ok(responses)
        return ok, elapsed, err, msgs

    def run_case(self, case: BenchmarkCase, startup_seconds: float) -> CaseResult:
        base_theory_name = sanitize_theory_name(case.theory_name)
        result = CaseResult(
            case_name=case.name,
            theory_name=base_theory_name,
            backend=self.backend_name,
            ok=False,
            startup_seconds=startup_seconds,
            case_seconds=0.0,
            exact_final_check_supported=True,
        )
        t_case = time.perf_counter()

        all_lines = [case.lemma] + case.steps
        for i, line in enumerate(all_lines):
            prefix = all_lines[: i + 1]
            theory_name = f"{base_theory_name}_{i:03d}_{uuid.uuid4().hex[:6]}"
            self._write_theory(theory_name, case.imports, prefix, append_sorry=True)
            try:
                ok, elapsed, err, msgs = self._run_theory(theory_name)
            except Exception as exc:
                result.steps.append(
                    StepResult(
                        index=i,
                        kind="lemma" if i == 0 else "proof_step",
                        code=line,
                        accepted=False,
                        elapsed_seconds=0.0,
                        error=format_exc(exc),
                    )
                )
                result.case_seconds = time.perf_counter() - t_case
                self._purge_all()
                return result

            result.steps.append(
                StepResult(
                    index=i,
                    kind="lemma" if i == 0 else "proof_step",
                    code=line,
                    accepted=ok,
                    elapsed_seconds=elapsed,
                    messages=msgs,
                    error=err,
                )
            )
            self._purge_all()
            if not ok:
                result.case_seconds = time.perf_counter() - t_case
                return result

        exact_theory_name = f"{base_theory_name}_exact_{uuid.uuid4().hex[:6]}"
        self._write_theory(exact_theory_name, case.imports, all_lines, append_sorry=False)
        try:
            ok, elapsed, err, _msgs = self._run_theory(exact_theory_name)
            result.exact_final_check_ok = ok
            result.exact_final_check_seconds = elapsed
            result.exact_final_check_error = err
        except Exception as exc:
            result.exact_final_check_ok = False
            result.exact_final_check_seconds = 0.0
            result.exact_final_check_error = format_exc(exc)
        finally:
            self._purge_all()

        result.case_seconds = time.perf_counter() - t_case
        result.ok = all(s.accepted for s in result.steps) and bool(result.exact_final_check_ok)
        return result


# -----------------------------
# CLI
# -----------------------------


def build_backend(args: argparse.Namespace) -> Backend:
    if args.backend == "qisabelle":
        if not args.qisabelle_root:
            raise BenchmarkError("--qisabelle-root is required for --backend qisabelle")
        return QIsabelleBackend(
            root=Path(args.qisabelle_root).resolve(),
            port=args.qisabelle_port,
            session_name=args.session,
            session_roots=[Path(p).resolve() for p in args.session_root],
        )
    if args.backend == "isabelle-gym":
        gym_root = args.gym_root or args.gym_src
        if not gym_root:
            raise BenchmarkError("--gym-root is required for --backend isabelle-gym")
        gym_root_path = Path(gym_root).resolve()
        if gym_root_path.name == "gym" and (gym_root_path / "isabelle_gym.py").exists():
            gym_root_path = gym_root_path.parent
        return IsabelleGymBackend(gym_root_path)
    if args.backend == "isabelle-server":
        password = args.server_password or os.environ.get("ISABELLE_SERVER_PASSWORD", "")
        return IsabelleServerBackend(
            host=args.server_host,
            port=args.server_port,
            password=password,
            session=args.server_session,
        )
    raise BenchmarkError(f"Unknown backend: {args.backend}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True, choices=["qisabelle", "isabelle-gym", "isabelle-server"])
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)

    # QIsabelle
    p.add_argument("--qisabelle-root", default=None, help="Path to the cloned qisabelle repo root")
    p.add_argument("--qisabelle-port", type=int, default=17000)
    p.add_argument("--session", default="HOL", help="QIsabelle session name")
    p.add_argument("--session-root", action="append", default=[], help="Extra session root for QIsabelle; repeat as needed")

    # IsabelleGym
    p.add_argument("--gym-root", default=None, help="Path to the FullBackward IsabelleGym repo root")
    p.add_argument("--gym-src", default=None, help="Deprecated alias; may point to repo root or the gym/ package dir")

    # Isabelle Server
    p.add_argument("--server-host", default="localhost")
    p.add_argument("--server-port", type=int, default=8000)
    p.add_argument("--server-password", default=None)
    p.add_argument("--server-session", default="HOL")

    return p.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_manifest(args.manifest)
    backend = build_backend(args)

    startup_seconds = 0.0
    try:
        startup_seconds = backend.start()
        payload = {
            "backend": backend.backend_name,
            "startup_seconds": startup_seconds,
            "manifest": str(args.manifest),
            "results": [asdict(backend.run_case(case, startup_seconds)) for case in cases],
        }
        write_json(args.out, payload)
        print(f"Wrote results to {args.out}")
        return 0
    except Exception as exc:
        err = {
            "backend": getattr(backend, "backend_name", args.backend),
            "startup_seconds": startup_seconds,
            "error": format_exc(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(args.out, err)
        print(f"Benchmark failed. Details written to {args.out}", file=sys.stderr)
        return 1
    finally:
        try:
            backend.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
