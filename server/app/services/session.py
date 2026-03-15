import concurrent.futures
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from server.app.errors import SessionError
from server.app.services.threaded_backend import ThreadedBackend
from server_gym.success_checker import get_error_message, is_syntax_successful

from .internal_models import (
    BigStepDiagnostic,
    BigStepExecuteResult,
    BigStepFailureLocation,
    CheckPointInfo,
    ProofState,
    SessionExecutionError,
    SessionStatus,
    SmallStepExecuteResult,
)
from .theory_chunks import preview_text, split_block_into_chunks, split_theory_into_blocks


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

        self._closed = False
        self.entered_thy = ""

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
            return None
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
        return self._call_backend(lambda: self.backend.raw.restore_state(state_id), timeout=timeout)

    def rollback(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.rollback(), timeout=timeout)

    def enter_thy(self, thy_name: str, timeout: Optional[float] = None):
        self.entered_thy = thy_name
        return self._call_backend(lambda: self.backend.raw.enter_thy(thy_name), timeout=timeout)

    def _result_output(self, result) -> Optional[str]:
        return result.total_output() if hasattr(result, "total_output") else None

    def _result_error(self, result) -> Optional[str]:
        return get_error_message(result)

    def _make_diagnostic(
        self,
        *,
        stage: str,
        index: int,
        chunk: str,
        success: bool,
        execution_time: float,
        result=None,
        error: Optional[str] = None,
    ) -> BigStepDiagnostic:
        return BigStepDiagnostic(
            stage=stage,
            index=index,
            success=success,
            preview=preview_text(chunk),
            output=self._result_output(result) if result is not None else None,
            error=(None if success else (error or (self._result_error(result) if result is not None else None))),
            execution_time=execution_time,
        )

    def execute_command(self, command: str, timeout: float = 30.0) -> SmallStepExecuteResult:
        self.update_activity()
        start_time = time.time()

        try:
            result = self.step(command, timeout=timeout)
            execution_time = time.time() - start_time
        except Exception as e:
            execution_time = time.time() - start_time
            raise SessionError(error=str(e), execution_time=execution_time)

        try:
            success = is_syntax_successful(result)
            subgoals = self.open_subgoals(timeout=timeout)

            self.command_history.append(
                {
                    "type": "small_step",
                    "command": command,
                    "timestamp": start_time,
                    "success": success,
                    "subgoals_count": len(subgoals),
                }
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
            raise SessionError(error=str(e), execution_time=execution_time)

    def _replay_block_by_chunks(
        self,
        block: str,
        block_index: int,
        block_checkpoint: int,
        timeout: float,
    ) -> Tuple[bool, List[BigStepDiagnostic], Optional[int], Optional[str]]:
        diagnostics: List[BigStepDiagnostic] = []
        chunks = split_block_into_chunks(block)
        self.restore_state(block_checkpoint, timeout=timeout)

        for chunk_index, chunk in enumerate(chunks):
            start = time.time()
            try:
                result = self.step(chunk, timeout=timeout)
                elapsed = time.time() - start
                success = is_syntax_successful(result)
                diagnostics.append(
                    self._make_diagnostic(
                        stage=f"block[{block_index}].chunk",
                        index=chunk_index,
                        chunk=chunk,
                        success=success,
                        execution_time=elapsed,
                        result=result,
                    )
                )
            except Exception as e:
                elapsed = time.time() - start
                self.restore_state(block_checkpoint, timeout=timeout)
                diagnostics.append(
                    self._make_diagnostic(
                        stage=f"block[{block_index}].chunk",
                        index=chunk_index,
                        chunk=chunk,
                        success=False,
                        execution_time=elapsed,
                        error=str(e),
                    )
                )
                return False, diagnostics, chunk_index, str(e)

            if not success:
                self.restore_state(block_checkpoint, timeout=timeout)
                return False, diagnostics, chunk_index, self._result_error(result)

        return True, diagnostics, None, None

    def _execute_big_step_localized(self, proof: str, timeout: float) -> tuple[bool, List[BigStepDiagnostic], Optional[BigStepFailureLocation], Optional[str]]:
        diagnostics: List[BigStepDiagnostic] = []
        blocks = split_theory_into_blocks(proof)

        if not blocks:
            return True, diagnostics, None, None

        for block_index, block in enumerate(blocks):
            block_checkpoint = self.save_state(timeout=timeout)
            start = time.time()
            try:
                result = self.step(block, timeout=timeout)
                elapsed = time.time() - start
                success = is_syntax_successful(result)
                diagnostics.append(
                    self._make_diagnostic(
                        stage="block",
                        index=block_index,
                        chunk=block,
                        success=success,
                        execution_time=elapsed,
                        result=result,
                    )
                )
            except Exception as e:
                elapsed = time.time() - start
                self.restore_state(block_checkpoint, timeout=timeout)
                diagnostics.append(
                    self._make_diagnostic(
                        stage="block",
                        index=block_index,
                        chunk=block,
                        success=False,
                        execution_time=elapsed,
                        error=str(e),
                    )
                )
                chunk_success, chunk_diags, failing_chunk, error = self._replay_block_by_chunks(
                    block=block,
                    block_index=block_index,
                    block_checkpoint=block_checkpoint,
                    timeout=timeout,
                )
                diagnostics.extend(chunk_diags)
                if not chunk_success:
                    return False, diagnostics, BigStepFailureLocation(
                        block_index=block_index,
                        chunk_index=failing_chunk,
                        preview=preview_text(block),
                    ), error
                continue

            if success:
                continue

            self.restore_state(block_checkpoint, timeout=timeout)
            chunk_success, chunk_diags, failing_chunk, error = self._replay_block_by_chunks(
                block=block,
                block_index=block_index,
                block_checkpoint=block_checkpoint,
                timeout=timeout,
            )
            diagnostics.extend(chunk_diags)
            if not chunk_success:
                return False, diagnostics, BigStepFailureLocation(
                    block_index=block_index,
                    chunk_index=failing_chunk,
                    preview=preview_text(block),
                ), error

        return True, diagnostics, None, None

    def big_step(self, theory_name: str, proof: str, timeout: float = 300.0) -> BigStepExecuteResult:
        """
        Execute large theory content with a fast path first, then localized replay on failure.
        """
        self.update_activity()
        start_time = time.time()

        try:
            self.enter_thy(theory_name, timeout=timeout)
            initial_checkpoint = self.save_state(timeout=timeout)
        except Exception as e:
            raise SessionError(error=str(e), execution_time=time.time() - start_time)

        try:
            result = self.step(proof, timeout=timeout)
            full_elapsed = time.time() - start_time
            success = is_syntax_successful(result)
            if success:
                self.command_history.append(
                    {
                        "type": "big_step",
                        "timestamp": start_time,
                        "success": True,
                        "mode": "full",
                    }
                )
                return BigStepExecuteResult(
                    success=True,
                    output=self._result_output(result),
                    error=None,
                    execution_time=full_elapsed,
                    mode="full",
                    diagnostics=[
                        self._make_diagnostic(
                            stage="full",
                            index=0,
                            chunk=proof,
                            success=True,
                            execution_time=full_elapsed,
                            result=result,
                        )
                    ],
                )

            full_error = self._result_error(result)
        except Exception as e:
            full_elapsed = time.time() - start_time
            full_error = str(e)
            result = None

        try:
            self.restore_state(initial_checkpoint, timeout=timeout)
            localized_success, localized_diags, failure_location, localized_error = self._execute_big_step_localized(
                proof,
                timeout,
            )
        except Exception as e:
            raise SessionError(error=str(e), execution_time=time.time() - start_time)

        diagnostics = [
            self._make_diagnostic(
                stage="full",
                index=0,
                chunk=proof,
                success=False,
                execution_time=full_elapsed,
                result=result,
                error=full_error,
            )
        ] + localized_diags

        total_elapsed = time.time() - start_time
        final_success = localized_success
        final_error = None if localized_success else (localized_error or full_error)
        final_mode = "localized" if localized_success else "localized_failed"

        self.command_history.append(
            {
                "type": "big_step",
                "timestamp": start_time,
                "success": final_success,
                "mode": final_mode,
                "diagnostics_count": len(diagnostics),
            }
        )

        return BigStepExecuteResult(
            success=final_success,
            output=None if result is None else self._result_output(result),
            error=final_error,
            execution_time=total_elapsed,
            mode=final_mode,
            diagnostics=diagnostics,
            failure_location=failure_location,
        )

    def get_proof_state(self, timeout: float = 30.0) -> ProofState | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

        try:
            subgoals = self.open_subgoals(timeout=timeout)
            current_thy = self.current_thy

            return ProofState(
                subgoals=subgoals,
                proof_finished=len(subgoals) == 0,
                current_theory=current_thy,
            )
        except Exception as e:
            return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)

    def save_checkpoint(self, timeout: float = 30.0) -> CheckPointInfo | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

        try:
            checkpoint_id = self.save_state(timeout=timeout)
            timestamp = time.time()
            self.checkpoints[checkpoint_id] = timestamp
            return CheckPointInfo(checkpoint_id=checkpoint_id, timestamp=timestamp)
        except Exception as e:
            return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)

    def restore_checkpoint(self, checkpoint_id: int, timeout: float = 30.0) -> bool | SessionExecutionError:
        self.update_activity()
        start_time = time.time()

        try:
            if checkpoint_id not in self.checkpoints:
                return SessionExecutionError(
                    error=f"Checkpoint {checkpoint_id} not found",
                    execution_time=time.time() - start_time,
                )
            self.restore_state(checkpoint_id, timeout=timeout)
            return True
        except Exception as e:
            return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)

    def close(self):
        if not self._closed:
            try:
                self.backend.close()
            finally:
                self._closed = True
                self.status = SessionStatus.CLOSED

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
