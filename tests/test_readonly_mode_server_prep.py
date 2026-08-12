"""Tests for the read-only-mode server preparation
(claude-work/research-lsp-readonly-mode, plan: server prep for dual MCP support).

Covers the pieces testable without a running Isabelle backend:
- DocumentLoadRequest schema validation (empty text, imports without thy_name)
- Session.load_document: backend reset, theory-name derivation, bookkeeping reset
- Session.verify_chunk retains last_chunk_report; load_document clears it
"""
from __future__ import annotations

import concurrent.futures
import uuid

import pytest
from pydantic import ValidationError

from server.app.api.v1.schemas.API_models import DocumentLoadRequest
from server.app.errors import SessionError
from server.app.services.session import _Isabelle_Session


class _FakeRaw:
    """Minimal stand-in for the Py4J ReplBackend proxy."""

    def __init__(self):
        self.reset_calls = 0
        self.entered = []
        self.steps = []
        self.probe_states = []

    def reset(self):
        self.reset_calls += 1
        return None

    def enter_thy(self, name):
        self.entered.append(name)
        return None

    def step(self, text):
        self.steps.append(text)
        return None  # success checkers degrade None -> no error output

    def verify_chunk(self, chunk, budget_ms):
        return (
            '{"timed_out": false, "success": true, "proof_open": false, '
            '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
            '"commands": [{"i": 0, "line": 1, "kind": "theorem", "status": "ok", '
            '"messages": []}]}'
        )

    def step_chunk_report(self, text, budget_ms, probe_state):
        self.probe_states.append(probe_state)
        return (
            '{"timed_out": false, "success": false, "proof_open": false, '
            '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
            '"commands": ['
            '{"i": 0, "line": 1, "kind": "theory", "status": "ok", "messages": []}, '
            '{"i": 1, "line": 2, "kind": "lemma", "status": "failed", "messages": '
            '[{"sev": "error", "text": "Failed to finish proof"}]}'
            "]}"
        )


class _FakeBackend:
    """Executes submitted jobs inline, like a healthy ThreadedBackend."""

    def __init__(self):
        self.raw = _FakeRaw()

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # pragma: no cover - defensive
            fut.set_exception(e)
        return fut


def _make_session() -> _Isabelle_Session:
    return _Isabelle_Session(
        session_id=uuid.uuid4(),
        session_theories=[],
        session_field="HOL",
        backend=_FakeBackend(),
    )


# ------------------------------------------------------- schema validation


def test_document_load_rejects_empty_text():
    with pytest.raises(ValidationError):
        DocumentLoadRequest(text="")


def test_document_load_requires_thy_name_with_imports():
    with pytest.raises(ValidationError):
        DocumentLoadRequest(text="lemma True by simp", imports=["Main"])
    # ... and accepts it when thy_name is given
    req = DocumentLoadRequest(
        text="lemma True by simp", imports=["Main"], thy_name="Scratch"
    )
    assert req.thy_name == "Scratch"


def test_document_load_full_file_mode_ok():
    req = DocumentLoadRequest(text="theory Foo imports Main begin\nend")
    assert req.thy_name is None and req.imports is None


# ------------------------------------------------------- load_document


def test_load_document_resets_backend_and_derives_theory_name():
    session = _make_session()
    result = session.load_document(
        "theory Foo imports Main begin\nlemma True by simp\nend"
    )
    assert session.backend.raw.reset_calls == 1
    assert session.backend.raw.entered == ["Foo"]
    assert session.entered_thy == "Foo"
    assert result.success
    assert session.command_history[-1]["type"] == "document_load"


def test_load_document_clears_bookkeeping():
    session = _make_session()
    session.command_history.append({"type": "small_step"})
    session.checkpoints[1] = 0.0
    session.verified_theories.append("Old")
    session.last_chunk_report = {"report": {}}
    session.load_document("theory Foo imports Main begin\nend", timeout=5.0)
    assert session.checkpoints == {}
    assert session.verified_theories == []
    assert session.last_chunk_report is None
    # history contains only the document_load marker
    assert [h["type"] for h in session.command_history] == ["document_load"]


def test_load_document_without_header_or_name_errors():
    session = _make_session()
    with pytest.raises(SessionError):
        session.load_document("lemma True by simp")


def test_load_document_with_imports_builds_header_server_side():
    session = _make_session()
    result = session.load_document(
        "lemma True by simp", thy_name="Scratch", imports=["Main"]
    )
    assert result.success
    assert session.entered_thy == "Scratch"
    # header stepped first, then the body
    assert session.backend.raw.steps[0].startswith("theory Scratch imports Main begin")
    assert session.backend.raw.steps[1] == "lemma True by simp"


# ------------------------------------------------------- last_chunk_report


def test_verify_chunk_retains_last_report():
    session = _make_session()
    session.entered_thy = "Foo"
    out = session.verify_chunk("lemma True by simp", timeout=5.0)
    assert session.last_chunk_report is not None
    assert session.last_chunk_report["report"]["success"] is True
    assert session.last_chunk_report["execution_time"] == out["execution_time"]
    assert session.last_chunk_report["timestamp"] > 0


def test_load_document_clears_last_report():
    session = _make_session()
    session.verify_chunk("lemma True by simp", timeout=5.0)
    assert session.last_chunk_report is not None
    session.load_document("theory Foo imports Main begin\nend")
    assert session.last_chunk_report is None


# ------------------------------------------------------- load_document report=True


def test_load_document_report_stores_last_chunk_report():
    session = _make_session()
    result = session.load_document(
        "theory Foo imports Main begin\nlemma False by auto", report=True
    )
    assert not result.success
    assert result.error == "line 2: Failed to finish proof"
    assert session.last_chunk_report is not None
    assert session.last_chunk_report["report"]["success"] is False
    cmds = session.last_chunk_report["report"]["commands"]
    assert [c["status"] for c in cmds] == ["ok", "failed"]


def test_load_document_report_skips_state_probe_after_theory_end():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nend", report=True)
    # probe_state=False: probing past a trailing `end` would hang the ML channel
    assert session.backend.raw.probe_states == [False]


def test_load_document_report_probes_state_without_theory_end():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nlemma True by simp", report=True)
    assert session.backend.raw.probe_states == [True]


def test_load_document_without_report_leaves_last_report_none():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nlemma True by simp")
    assert session.last_chunk_report is None
    assert session.backend.raw.probe_states == []
