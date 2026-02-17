import time
import uuid
import concurrent.futures
from typing import List, Dict, Any, Optional

from server_gym.success_checker import is_syntax_successful, get_error_message
from server.app.errors import SessionError
from .internal_models import SessionStatus, ExecuteResult, ProofState, CheckPointInfo
from server.app.services.threaded_backend import ThreadedBackend



class _Isabelle_Session:
    """Manages a single Isabelle proving session (threaded backend)."""

    def __init__(
        self,
        session_id: uuid.UUID,
        session_theories: List[str],
        session_field: str,
        backend,
    ):
        self.session_id = session_id
        self.theories = session_theories
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
        """
        Run a blocking Py4J backend call on the session's dedicated worker thread.
        This method is SYNC and will block the calling thread.
        (Call it from FastAPI via asyncio.to_thread(...) to avoid blocking the event loop.)
        """
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
        # property kept sync; keep timeout=None
        return self._call_backend(lambda: self.backend.raw.current_thy_name_string())

    def save_state(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.save_state(), timeout=timeout)

    def restore_state(self, state_id: int, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.restore_state(state_id), timeout=timeout)

    def rollback(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.rollback(), timeout=timeout)

    def enter_thy(self, thy_name: str, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.enter_thy(thy_name), timeout=timeout)

    def execute_command(self, command: str, timeout: float = 30.0) -> ExecuteResult:
        """Execute an Isar command (SYNC; call via asyncio.to_thread in FastAPI)."""
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
                    "command": command,
                    "timestamp": start_time,
                    "success": success,
                    "subgoals_count": len(subgoals),
                }
            )

            return ExecuteResult(
                success=success,
                output=result.total_output() if hasattr(result, "total_output") else None,
                error=get_error_message(result) if not success else None,
                subgoals=subgoals,
                execution_time=execution_time,
            )

        except Exception as e:
            execution_time = time.time() - start_time
            raise SessionError(error=str(e), execution_time=execution_time)

    def get_proof_state(self, timeout: float = 30.0) -> ProofState | SessionError:
        """Get current proof state (SYNC; call via asyncio.to_thread in FastAPI)."""
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
            return SessionError(error=str(e), execution_time=time.time() - start_time)

    def save_checkpoint(self, timeout: float = 30.0) -> CheckPointInfo | SessionError:
        self.update_activity()
        start_time = time.time()

        try:
            checkpoint_id = self.save_state(timeout=timeout)
            timestamp = time.time()
            self.checkpoints[checkpoint_id] = timestamp
            return CheckPointInfo(checkpoint_id=checkpoint_id, timestamp=timestamp)
        except Exception as e:
            return SessionError(error=str(e), execution_time=time.time() - start_time)

    def restore_checkpoint(self, checkpoint_id: int, timeout: float = 30.0) -> bool | SessionError:
        self.update_activity()
        start_time = time.time()

        try:
            if checkpoint_id not in self.checkpoints:
                return SessionError(
                    error=f"Checkpoint {checkpoint_id} not found",
                    execution_time=time.time() - start_time,
                )
            return self.restore_state(checkpoint_id, timeout=timeout)
        except Exception as e:
            return SessionError(error=str(e), execution_time=time.time() - start_time)

    def close(self):
        if not self._closed:
            try:
                # Stop worker thread; underlying backend stays owned by gateway/JVM
                self.backend.close()
            finally:
                self._closed = True
                self.status = SessionStatus.CLOSED

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
