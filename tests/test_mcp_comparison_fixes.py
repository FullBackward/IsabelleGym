"""Tests for the MCP-comparison harness fixes
(claude-work/research-mcp-comparison-audit/FINDINGS.md §2, H1–H6).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "MCP-comparison"))

from common.model import NUDGE_LIMIT, RoundResult, no_tool_call_action
from common.problems import derive_session


def _round(text: str | None, finish_reason: str | None = "stop") -> RoundResult:
    return RoundResult(
        assistant_text=text, tool_calls=[], usage={}, latency_s=0.0,
        finish_reason=finish_reason,
    )


# --------------------------------------------------- §2/H2: no-tool-call policy


def test_done_reply_finishes_attempt():
    action, payload = no_tool_call_action(_round("All verified. DONE"), 0)
    assert action == "done" and payload is None


def test_truncated_round_is_nudged_not_killed():
    action, payload = no_tool_call_action(_round("", finish_reason="length"), 0)
    assert action == "nudge"
    assert "token limit" in payload


def test_truncation_stop_message_names_max_tokens():
    action, payload = no_tool_call_action(_round("", finish_reason="length"), NUDGE_LIMIT)
    assert action == "stop"
    assert "max_tokens" in payload  # not "content filter"


def test_text_only_round_is_nudged_then_stopped():
    action, _ = no_tool_call_action(_round("Let me think about the approach."), 0)
    assert action == "nudge"
    action, payload = no_tool_call_action(_round("Still thinking."), NUDGE_LIMIT)
    assert action == "stop"
    assert "without replying DONE" in payload


def test_empty_round_without_truncation_labelled_empty():
    action, payload = no_tool_call_action(_round(None), NUDGE_LIMIT)
    assert action == "stop"
    assert "empty" in payload


# ------------------------------------------------------- H6: session derivation


def test_derive_session_plain_theories_use_default():
    assert derive_session(["Main"]) == "HOL"
    assert derive_session(["Complex_Main"]) == "HOL"


def test_derive_session_qualified_import_names_its_session():
    assert derive_session(
        ["Complex_Main", "HOL-Computational_Algebra.Computational_Algebra"]
    ) == "HOL-Computational_Algebra"


# ------------------------------------ I/Q token resolution + setup fail-fast


def test_iq_token_env_wins(monkeypatch):
    import run_autocorrode_iq as iq

    monkeypatch.setenv("IQ_AUTH_TOKEN", "  tok-from-env  ")
    token, source = iq.resolve_iq_token()
    assert token == "tok-from-env" and "env" in source


def test_iq_token_file_fallback(monkeypatch, tmp_path):
    import run_autocorrode_iq as iq

    monkeypatch.delenv("IQ_AUTH_TOKEN", raising=False)
    tok_file = tmp_path / "iq_token.txt"
    tok_file.write_text("tok-from-file\n")
    monkeypatch.setenv("IQ_AUTH_TOKEN_FILE", str(tok_file))
    token, source = iq.resolve_iq_token()
    assert token == "tok-from-file" and source == str(tok_file)


def test_iq_token_none_when_unconfigured(monkeypatch):
    import run_autocorrode_iq as iq

    monkeypatch.delenv("IQ_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("IQ_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setattr(iq, "_TOKEN_FILE_DEFAULT", Path("/nonexistent/iq_token.txt"))
    token, source = iq.resolve_iq_token()
    assert token is None and source == "not configured"


def test_iq_tool_output_failure_detection():
    import run_autocorrode_iq as iq

    assert iq.tool_output_failed("MCP tool error (authenticate): McpError: Invalid authentication token")
    assert iq.tool_output_failed("McpError: Connection closed")
    assert not iq.tool_output_failed("Authenticated successfully")
    assert not iq.tool_output_failed('{"count":1,"positions":[...]}')


# --------------------------------------- schema: old results rows still load


def test_load_results_accepts_rows_without_new_fields(tmp_path):
    from common.metrics import AttemptResult, append_result, load_results

    path = tmp_path / "results.jsonl"
    append_result(path, AttemptResult(system="isabellegym", problem="p", repeat=0))
    # simulate an OLD row (pre new fields) by dropping them
    import json
    row = json.loads(path.read_text().splitlines()[0])
    del row["n_truncated_rounds"]
    del row["n_nudge_rounds"]
    path.write_text(json.dumps(row) + "\n")
    rows = load_results(path)
    assert rows[0].n_truncated_rounds == 0
    assert rows[0].n_nudge_rounds == 0
