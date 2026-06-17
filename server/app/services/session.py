from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import py4j

from server.app.core.config import Logging, Timeouts, RegularExp
from server.app.core.logging import get_logger, logging_context
from server.app.errors import SessionError, SessionLeaseError
from server.app.services.threaded_backend import ThreadedBackend
from server_gym.success_checker import (
    get_error_message,
    is_syntax_successful,
)

from .internal_models import (
    CheckPointInfo,
    ProofState,
    SessionExecutionError,
    SessionStatus,
    SmallStepExecuteResult,
)
from .theory_chunks import preview_text
from .session_bigstep import BigStepMixin

logger = get_logger(__name__)


class _Isabelle_Session(BigStepMixin):

    def __init__(
        self,
        session_id: uuid.UUID,
        session_theories: List[str],
        session_field: str,
        backend,
        loaded_theories: Optional[List[str]] = None,
        dependency_key: Optional[str] = None,
        wrapper_theory: Optional[str] = None,
    ):
        self.session_id = session_id
        self.theories = list(session_theories)
        self.loaded_theories = list(loaded_theories or session_theories)
        self.dependency_key = dependency_key
        self.wrapper_theory = wrapper_theory
        self.field = session_field
        self.created_at = time.time()
        self.last_activity = time.time()
        self.status = SessionStatus.ACTIVE
        self.backend: ThreadedBackend = backend

        self.command_history: List[Dict[str, Any]] = []
        self.checkpoints: Dict[int, float] = {}
        self.verified_theories: List[str] = []

        self._closed = False
        self.entered_thy = ""
        self._active_requests = 0
        self._active_requests_lock = threading.Lock()

        # Exclusive-lease support: when a session is leased, only the
        # holder (identified by lease_id) may use it.  find_session()
        # skips leased sessions so no two workers can collide.
        self._leased = False
        self._lease_id: Optional[str] = None
        self._lease_lock = threading.Lock()
        with logging_context(session_id=self.session_id, field=self.field):
            logger.info(
                "session object initialized dependency_key=%s loaded_theories=%s",
                (self.dependency_key or "")[:12],
                self.loaded_theories,
            )

    def _call_backend(self, fn, timeout: Optional[float] = None):
        fut: concurrent.futures.Future = self.backend.submit(fn)
        return fut.result(timeout=timeout)

    def update_activity(self):
        self.last_activity = time.time()

    def is_idle(self, timeout: float = Timeouts.IDLE_DEFAULT, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.last_activity) > timeout

    @property
    def in_use(self) -> bool:
        with self._active_requests_lock:
            return self._active_requests > 0

    @property
    def active_request_count(self) -> int:
        with self._active_requests_lock:
            return self._active_requests

    def _acquire_request(self) -> None:
        with self._active_requests_lock:
            self._active_requests += 1

    def _release_request(self) -> None:
        with self._active_requests_lock:
            self._active_requests = max(0, self._active_requests - 1)


    @property
    def leased(self) -> bool:
        with self._lease_lock:
            return self._leased

    @property
    def lease_id(self) -> Optional[str]:
        with self._lease_lock:
            return self._lease_id

    def try_acquire_lease(self, lease_id: str) -> bool:
        with self._lease_lock:
            if self._leased:
                return False
            self._leased = True
            self._lease_id = lease_id
        self.update_activity()
        return True

    def acquire_lease(self, lease_id: str) -> None:
        if not self.try_acquire_lease(lease_id):
            raise SessionLeaseError(
                f"Session {self.session_id} is already leased by {self.lease_id}"
            )

    def require_lease(self, lease_id: Optional[str]) -> None:
        with self._lease_lock:
            if not self._leased or not self._lease_id:
                raise SessionLeaseError(f"Session {self.session_id} is not currently leased")
            if not lease_id:
                raise SessionLeaseError("Missing lease token for leased session")
            if self._lease_id != lease_id:
                raise SessionLeaseError(
                    f"Invalid lease token for session {self.session_id}"
                )

    def release_lease(self) -> None:
        with self._lease_lock:
            self._leased = False
            self._lease_id = None
        self.update_activity()

    def step(self, command: str, timeout: Optional[float] = None):
        if not isinstance(command, str) or command.strip() == "":
            logger.debug("ignoring empty command")
            return None
        logger.debug("backend step submitted preview=%s", preview_text(command, Logging.COMMAND_PREVIEW_CHARS))
        return self._call_backend(lambda: self.backend.raw.step(command), timeout=timeout)

    def open_subgoals(self, timeout: Optional[float] = None) -> List[str]:
        subgoals = self._call_backend(lambda: list(self.backend.raw.open_subgoals()), timeout=timeout)
        return [s.strip() for s in subgoals]

    def proof_finished(self, timeout: Optional[float] = None) -> bool:
        return len(self.open_subgoals(timeout=timeout)) == 0

    def get_source(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.get_source(), timeout=timeout)

    @property
    def current_thy(self) -> str:
        return self._call_backend(lambda: self.backend.raw.current_thy_name_string())

    def save_state(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.save_state(), timeout=timeout)

    def restore_state(self, state_id: int, timeout: Optional[float] = None):
        logger.debug("restoring backend state state_id=%s", state_id)
        return self._call_backend(lambda: self.backend.raw.restore_state(state_id), timeout=timeout)

    def rollback(self, timeout: Optional[float] = None):
        logger.info("rolling back backend state")
        return self._call_backend(lambda: self.backend.raw.rollback(), timeout=timeout)

    def sledgehammer(
        self,
        timeout_s: int = 30,
        http_timeout: Optional[float] = None,
    ) -> list:
        """Call Isabelle's sledgehammer via the dedicated ML channel.

        Returns a list of proof method strings (e.g. ['by (metis foo)',
        'by (simp add: bar)']).  Returns an empty list if no proof is found
        within timeout_s or if the session is not in a proof state.
        """
        logger.info("running sledgehammer timeout_s=%s", timeout_s)
        effective_http_timeout = http_timeout or (timeout_s + 30.0)
        raw: "py4j.java_collections.JavaList[str]" = self._call_backend(
            lambda: self.backend.raw.sledgehammer(timeout_s),
            timeout=effective_http_timeout,
        )
        return list(raw) if raw is not None else []

    def enter_thy(self, thy_name: str, timeout: Optional[float] = None):
        self.entered_thy = thy_name
        logger.info("entering theory theory_name=%s", thy_name)
        return self._call_backend(lambda: self.backend.raw.enter_thy(thy_name), timeout=timeout)

    def _result_error(self, result) -> Optional[str]:
        return get_error_message(result)

    def _result_output(self, result) -> str:
        if result is None:
            return ""
        if hasattr(result, "total_output"):
            try:
                value = result.total_output()
                return value if isinstance(value, str) else str(value)
            except Exception:
                return str(result)
        return str(result)

    def execute_command(self, command: str, timeout: float = Timeouts.COMMAND_DEFAULT) -> SmallStepExecuteResult:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "small-step command started timeout=%s preview=%s",
                    timeout,
                    preview_text(command, Logging.COMMAND_PREVIEW_CHARS),
                )
                try:
                    result = self.step(command, timeout=timeout)
                    execution_time = time.time() - start_time
                except Exception as e:
                    execution_time = time.time() - start_time
                    logger.exception("small-step backend call failed")
                    raise SessionError(error=str(e), execution_time=execution_time)

                try:
                    success = is_syntax_successful(result)
                    error_message = self._result_error(result)
                    subgoal_error: Optional[str] = None
                    if command == "end" or command == "end\n" or \
                            RegularExp.THEORY_HEADER_RE.search(command):
                        subgoals = []
                    else:
                        try:
                            subgoals = self.open_subgoals(timeout=timeout)
                        except Exception as exc:
                            subgoals = []
                            subgoal_error = f"{exc.__class__.__name__}: {exc}"
                            logger.warning(
                                "open_subgoals failed after command execution; command_success=%s error=%s",
                                success,
                                subgoal_error,
                            )

                    self.command_history.append(
                        {
                            "type": "small_step",
                            "command": command,
                            "timestamp": start_time,
                            "success": success,
                            "subgoal_error": subgoal_error,
                            "subgoals_count": len(subgoals),
                        }
                    )

                    logger.info(
                        "small-step command finished success=%s subgoals=%s subgoal_error=%s execution_time=%s",
                        success,
                        len(subgoals),
                        bool(subgoal_error),
                        round(execution_time, 3),
                    )
                    return SmallStepExecuteResult(
                        success=success,
                        output=self._result_output(result),
                        error=error_message if not success else None,
                        subgoal_error=subgoal_error,
                        subgoals=subgoals,
                        execution_time=execution_time,
                    )

                except Exception as e:
                    execution_time = time.time() - start_time
                    logger.exception("small-step result processing failed")
                    raise SessionError(error=str(e), execution_time=execution_time)
        finally:
            self._release_request()

    def verify_chunk(self, chunk: str, timeout: float = Timeouts.COMMAND_DEFAULT) -> Dict[str, Any]:
        """Verify a whole proof chunk in one shot under a SINGLE wall budget.

        The chunk is inserted as one PIDE edit and checked with per-session parallelism on;
        the backend returns a per-command status report (ok/failed/running/unprocessed) in
        source order. The wall budget is enforced inside the backend, so on expiry we get a
        PARTIAL report (naming the still-`running` line) rather than a timeout exception —
        only one timeout is ever surfaced. Returns {"report": <parsed>, "execution_time": s}.
        """
        self.update_activity()
        self._acquire_request()
        start_time = time.time()
        budget_ms = int(max(0.0, timeout) * 1000)
        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "verify_chunk started budget_ms=%s preview=%s",
                    budget_ms,
                    preview_text(chunk, Logging.COMMAND_PREVIEW_CHARS),
                )
                try:
                    # Backend bounds the work at budget_ms; give the Python call extra grace
                    # so the Python side never times out before the backend returns its report.
                    report_json = self._call_backend(
                        lambda: self.backend.raw.verify_chunk(chunk, budget_ms),
                        timeout=timeout + Timeouts.COMMAND_DEFAULT,
                    )
                    execution_time = time.time() - start_time
                except Exception as e:
                    execution_time = time.time() - start_time
                    logger.exception("verify_chunk backend call failed")
                    raise SessionError(error=str(e), execution_time=execution_time)

                try:
                    report = json.loads(report_json) if report_json else {}
                except (ValueError, TypeError):
                    report = {"timed_out": False, "commands": [],
                              "error": "unparseable backend report"}
                commands = report.get("commands", []) or []
                logger.info(
                    "verify_chunk finished commands=%s timed_out=%s execution_time=%s",
                    len(commands), report.get("timed_out"), round(execution_time, 3),
                )
                return {"report": report, "execution_time": execution_time}
        finally:
            self._release_request()

    def get_proof_state(self, timeout: float = Timeouts.PROOF_STATE) -> ProofState | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    subgoals = self.open_subgoals(timeout=timeout)
                    current_thy = self.current_thy
                    logger.debug("proof state fetched subgoals=%s current_theory=%s", len(subgoals), current_thy)
                    return ProofState(
                        subgoals=subgoals,
                        proof_finished=len(subgoals) == 0,
                        current_theory=current_thy,
                    )
                except Exception as e:
                    logger.exception("failed to fetch proof state")
                    return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def save_checkpoint(self, timeout: float = Timeouts.CHECKPOINT_SAVE) -> CheckPointInfo | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    checkpoint_id = self.save_state(timeout=timeout)
                    timestamp = time.time()
                    self.checkpoints[checkpoint_id] = timestamp
                    logger.info("checkpoint saved checkpoint_id=%s", checkpoint_id)
                    return CheckPointInfo(checkpoint_id=checkpoint_id, timestamp=timestamp)
                except Exception as e:
                    logger.exception("failed to save checkpoint")
                    return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def restore_checkpoint(self, checkpoint_id: int, timeout: float = Timeouts.CHECKPOINT_RESTORE) -> bool | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    if checkpoint_id not in self.checkpoints:
                        logger.warning("checkpoint not found checkpoint_id=%s", checkpoint_id)
                        return SessionExecutionError(
                            error=f"Checkpoint {checkpoint_id} not found",
                            execution_time=time.time() - start_time,
                        )
                    self.restore_state(checkpoint_id, timeout=timeout)
                    logger.info("checkpoint restored checkpoint_id=%s", checkpoint_id)
                    return True
                except Exception as e:
                    logger.exception("failed to restore checkpoint checkpoint_id=%s", checkpoint_id)
                    return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def close(self):
        if not self._closed:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    logger.info("closing threaded backend")
                    self.backend.close()
                finally:
                    self._closed = True
                    self.status = SessionStatus.CLOSED
                    logger.info("session closed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
