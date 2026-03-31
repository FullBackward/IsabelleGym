from __future__ import annotations

import concurrent.futures
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from server.app.core.config import Logging
from server.app.core.logging import get_logger, logging_context
from server.app.errors import SessionError
from server.app.services.threaded_backend import ThreadedBackend
from server.app.services.theory_parsing import extract_theory_name
from server_gym.success_checker import (
    get_error_message,
    get_raw_error_output,
    get_raw_output,
    is_syntax_successful,
)

from .internal_models import (
    BigStepDiagnostic,
    BigStepExecuteResult,
    CheckPointInfo,
    ProofState,
    SessionExecutionError,
    SessionStatus,
    SmallStepExecuteResult,
)
from .theory_chunks import preview_text

logger = get_logger(__name__)


class _Isabelle_Session:
    """Manages a single Isabelle proving session (threaded backend)."""

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

    def is_idle(self, timeout: float = 300, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.last_activity) > timeout

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

    def enter_thy(self, thy_name: str, timeout: Optional[float] = None):
        self.entered_thy = thy_name
        logger.info("entering theory theory_name=%s", thy_name)
        return self._call_backend(lambda: self.backend.raw.enter_thy(thy_name), timeout=timeout)

    def _result_output(self, result) -> Optional[str]:
        return result.total_output() if hasattr(result, "total_output") else None

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

    def _make_big_step_failure(
        self,
        *,
        stage: str,
        result=None,
        fallback_error: Optional[str] = None,
        execution_time: float,
        output_parts: Optional[List[str]] = None,
    ) -> BigStepExecuteResult:
        message = fallback_error
        if result is not None and fallback_error is None:
            try:
                message = get_error_message(result)
            except Exception:
                message = None
        if not message:
            message = f"Big-step verification failed during {stage}."
        else:
            message = f"Big-step verification failed during {stage}: {message}"

        output = "\n".join([part for part in (output_parts or []) if part]).strip() or None
        return BigStepExecuteResult(
            success=False,
            output=output,
            error=message,
            execution_time=execution_time,
        )

    def _extract_declared_theory_name(self, theory_text: str) -> Optional[str]:
        return extract_theory_name(theory_text)

    def _split_complete_theory(self, theory_text: str) -> Tuple[str, str, str]:
        """
        Split a complete theory file into:
        1. theory/imports/begin header
        2. theory body
        3. final end keyword

        This matches the backend behavior where incomplete proofs are surfaced when
        the final standalone `end` is stepped.
        """
        text = theory_text.strip()
        if not text:
            raise ValueError("Theory text is empty.")

        header_match = re.search(r"(?s)\btheory\b.*?\bbegin\b", text)
        if header_match is None:
            raise ValueError(
                "Big-step verification requires a complete theory file containing "
                "a theory header with imports and a 'begin' keyword."
            )

        end_match = re.search(r"\bend\s*$", text)
        if end_match is None:
            raise ValueError(
                "Big-step verification requires a complete theory file ending with a final 'end'."
            )

        if header_match.end() > end_match.start():
            raise ValueError("Theory body is malformed: final 'end' appears before the end of the header.")

        header = text[: header_match.end()].strip()
        body = text[header_match.end() : end_match.start()]
        end_keyword = text[end_match.start() : end_match.end()].strip()

        return header + "\n", body.strip(), end_keyword

    def _restore_big_step_state(
        self,
        checkpoint_id: Optional[int],
        previous_theory: str,
        timeout: float,
    ) -> Optional[str]:
        restore_errors: List[str] = []

        if checkpoint_id is not None:
            try:
                restored = self.restore_state(checkpoint_id, timeout=timeout)
                if restored is False:
                    restore_errors.append("restore_state returned false")
            except Exception as exc:
                restore_errors.append(f"restore_state error: {exc}")

        if previous_theory and previous_theory != self.entered_thy:
            try:
                self.enter_thy(previous_theory, timeout=timeout)
            except Exception as exc:
                restore_errors.append(f"failed to re-enter previous theory '{previous_theory}': {exc}")

        if restore_errors:
            logger.error("state restore encountered issues: %s", "; ".join(restore_errors))
            return "; ".join(restore_errors)
        logger.info("state restored successfully after big-step failure")
        return None

    def execute_command(self, command: str, timeout: float = 30.0) -> SmallStepExecuteResult:
        self.update_activity()
        start_time = time.time()

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
                if not(command == "end" or command == "end\n"):
                    subgoals = self.open_subgoals(timeout=timeout)
                else:
                    subgoals = []

                self.command_history.append(
                    {
                        "type": "small_step",
                        "command": command,
                        "timestamp": start_time,
                        "success": success,
                        "subgoals_count": len(subgoals),
                    }
                )

                logger.info(
                    "small-step command finished success=%s subgoals=%s execution_time=%s",
                    success,
                    len(subgoals),
                    round(execution_time, 3),
                )
                return SmallStepExecuteResult(
                    success=success,
                    output=self._result_output(result),
                    error=self._result_error(result) if not success else None,
                    subgoals=subgoals,
                    execution_time=execution_time,
                )

            except Exception as e:
                execution_time = time.time() - start_time
                logger.exception("small-step result processing failed")
                raise SessionError(error=str(e), execution_time=execution_time)

    def big_step(self, theory_name: str, proof: str, timeout: float = 300.0) -> BigStepExecuteResult:
        self.update_activity()
        start_time = time.time()
        checkpoint_id: Optional[int] = None
        output_parts: List[str] = []

        try:
            previous_theory = self.current_thy
        except Exception:
            previous_theory = ""

        try:
            declared_name = self._extract_declared_theory_name(proof)
            if declared_name is None:
                execution_time = time.time() - start_time
                return self._make_big_step_failure(
                    stage="theory-header validation",
                    fallback_error="Theory text must start with a valid 'theory <Name>' declaration.",
                    execution_time=execution_time,
                )

            if declared_name != theory_name:
                execution_time = time.time() - start_time
                return self._make_big_step_failure(
                    stage="theory-name validation",
                    fallback_error=(
                        f"Declared theory name '{declared_name}' does not match request theory_name '{theory_name}'."
                    ),
                    execution_time=execution_time,
                )

            header_text, body_text, end_text = self._split_complete_theory(proof)
            checkpoint_id = self.save_state(timeout=timeout)
            self.enter_thy(theory_name, timeout=timeout)

            header_result = self.step(header_text, timeout=timeout)
            output_parts.append(self._result_output(header_result))
            if not is_syntax_successful(header_result):
                restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                failure = self._make_big_step_failure(
                    stage="theory header/import processing",
                    result=header_result,
                    execution_time=time.time() - start_time,
                    output_parts=output_parts,
                )
                if restore_note:
                    failure.error = f"{failure.error} Restore note: {restore_note}"
                return failure

            if body_text.strip():
                body_result = self.step(body_text.strip() + "\n", timeout=timeout)
                output_parts.append(self._result_output(body_result))
                if not is_syntax_successful(body_result):
                    restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                    failure = self._make_big_step_failure(
                        stage="theory body processing",
                        result=body_result,
                        execution_time=time.time() - start_time,
                        output_parts=output_parts,
                    )
                    if restore_note:
                        failure.error = f"{failure.error} Restore note: {restore_note}"
                    return failure

            end_result = self.step(end_text, timeout=timeout)
            output_parts.append(self._result_output(end_result))
            if not is_syntax_successful(end_result):
                restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
                failure = self._make_big_step_failure(
                    stage="final end verification",
                    result=end_result,
                    execution_time=time.time() - start_time,
                    output_parts=output_parts,
                )
                if restore_note:
                    failure.error = f"{failure.error} Restore note: {restore_note}"
                return failure

            execution_time = time.time() - start_time
            self.command_history.append(
                {
                    "command": f"[BIGSTEP] {theory_name}",
                    "timestamp": start_time,
                    "success": True,
                    "subgoals_count": 0,
                }
            )
            self.verified_theories.append(theory_name)
            logger.info(
                "big-step command finished theory_name=%s execution_time=%s",
                theory_name,
                round(execution_time, 3),
            )
            return BigStepExecuteResult(
                success=True,
                output="\n".join([part for part in output_parts if part]).strip() or None,
                error=None,
                execution_time=execution_time,
            )

        except Exception as exc:
            restore_note = self._restore_big_step_state(checkpoint_id, previous_theory, timeout)
            execution_time = time.time() - start_time
            failure = self._make_big_step_failure(
                stage="big-step execution",
                fallback_error=str(exc),
                execution_time=execution_time,
                output_parts=output_parts,
            )
            if restore_note:
                failure.error = f"{failure.error} Restore note: {restore_note}"
            return failure

    def get_proof_state(self, timeout: float = 30.0) -> ProofState | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

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

    def save_checkpoint(self, timeout: float = 30.0) -> CheckPointInfo | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

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

    def restore_checkpoint(self, checkpoint_id: int, timeout: float = 30.0) -> bool | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

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
