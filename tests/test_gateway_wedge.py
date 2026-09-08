"""Bug 9 hardening tests: a wedged-but-alive gateway (Event_Timer dead) must
trigger crash recovery, and functional liveness must be cached for health
probes. No Isabelle backend needed — gateways are fakes.
"""
from __future__ import annotations

import threading

import pytest

from server.app.services import session_manager_helpers as helpers
from server.app.services.session_manager import SessionManager


class _FakeGateway:
    def __init__(self, alive):
        self._alive = alive
        self.probe_calls = 0
        self.terminated = False

    def is_alive(self):
        self.probe_calls += 1
        return self._alive

    def terminate(self):
        self.terminated = True


def _bare_manager(gateway):
    """A SessionManager shell with only what _ensure_gateway touches."""
    mgr = SessionManager.__new__(SessionManager)
    mgr.gateway = gateway
    mgr._lock = threading.Lock()
    mgr._lru = {}
    mgr.thy_init = object()
    return mgr


def test_wedged_gateway_triggers_recovery(monkeypatch):
    wedged = _FakeGateway(alive=False)
    mgr = _bare_manager(wedged)
    started = []
    monkeypatch.setattr(
        helpers, "ReplBackendGatewayProcess", lambda: started.append(1) or _FakeGateway(True)
    )
    mgr._ensure_gateway()
    assert started == [1], "a wedged gateway must be replaced"
    assert wedged.terminated is True
    assert mgr.thy_init is None or mgr.gateway is not wedged
    assert mgr.gateway is not wedged


def test_alive_gateway_not_recovered(monkeypatch):
    healthy = _FakeGateway(alive=True)
    mgr = _bare_manager(healthy)
    monkeypatch.setattr(
        helpers,
        "ReplBackendGatewayProcess",
        lambda: pytest.fail("gateway must not be rebuilt"),
    )
    mgr._ensure_gateway()
    assert mgr.gateway is healthy
    assert healthy.terminated is False


def test_gateway_alive_probe_cached():
    healthy = _FakeGateway(alive=True)
    mgr = _bare_manager(healthy)
    first = mgr.gateway_alive()
    second = mgr.gateway_alive()
    assert first is True and second is True
    assert healthy.probe_calls == 1, "second call within the cache window must not re-probe"
