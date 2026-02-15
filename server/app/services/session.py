import time
from server_gym.success_checker import is_syntax_successful, get_error_message
from typing import List, Dict, Any
from .internal_models import *
import uuid
from server.app.errors import SessionError

class _Isabelle_Session:
    """Manages a single Isabelle proving session"""
    
    def __init__(
        self,
        session_id: uuid.UUID,
        session_theories: List[str],
        session_field: str,
        backend
    ):
        self.session_id = session_id
        self.theories = session_theories
        self.field = session_field
        self.created_at = time.time()
        self.last_activity = time.time()
        self.status = SessionStatus.ACTIVE
        self.backend = backend
        
        self.command_history: List[Dict[str, Any]] = []
        self.checkpoints: Dict[int, float] = {}
    

    def step(self, command: str):
        return self.backend.step(command)
    
    def open_subgoals(self):
        subgoals = self.backend.open_subgoals()
        return [s.strip() for s in subgoals]
    
    def proof_finished(self):
        return len(self.open_subgoals()) == 0
    
    def get_source(self):
        return self.backend.get_source()
    
    @property
    def current_thy(self):
        return self.backend.current_thy_name_string()
    
    def save_state(self):
        return self.backend.save_state()
    
    def restore_state(self, state_id):
        return self.backend.restore_state(state_id)
    
    def rollback(self):
        return self.backend.rollback()
    
    def enter_thy(self, thy_name: str):
        return self.backend.enter_thy(thy_name)
    
    def close(self):
        if not self._closed:
            self.backend.close()
            self._closed = True
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
    
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = time.time()
    
    def is_idle(self, timeout: float = 300) -> bool:
        """Check if session is idle (no activity for timeout seconds)"""
        return (time.time() - self.last_activity) > timeout
    
    def execute_command(self, command: str, timeout: float = 30.0) -> ExecuteResult | SessionError:
        """Execute an Isar command"""
        self.update_activity()
        start_time = time.time()
        
        try:
            result = self.step(command)
            execution_time = time.time() - start_time
            
            success = is_syntax_successful(result)
            subgoals = self.open_subgoals()
            
            self.command_history.append({
                'command': command,
                'timestamp': start_time,
                'success': success,
                'subgoals_count': len(subgoals)
            })
            
            return ExecuteResult(
                success=success,
                output=result.total_output() if hasattr(result, 'total_output') else None,
                error=get_error_message(result) if not success else None,
                subgoals=subgoals,
                execution_time=execution_time
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return SessionError(
                error=str(e),
                execution_time=execution_time
            )
    
    def get_proof_state(self) -> ProofState | SessionError:
        self.update_activity()
        start_time = time.time()
        
        try:
            subgoals = self.open_subgoals()
            current_thy = self.current_thy
            
            return ProofState(
                subgoals=subgoals,
                proof_finished=len(subgoals) == 0,
                current_theory=current_thy
            )
        except Exception as e:
            execution_time = time.time() - start_time
            return SessionError(
                error=str(e),
                execution_time=execution_time
            )
    
    def save_checkpoint(self) -> CheckPointInfo | SessionError:
        """Save current state"""
        self.update_activity()
        start_time = time.time()
        try:
            checkpoint_id = self.save_state()
            timestamp = time.time()
            self.checkpoints[checkpoint_id] = timestamp
        
            return CheckPointInfo(
                checkpoint_id=checkpoint_id,
                timestamp=timestamp
            )
        except Exception as e:
            return SessionError(
                error=str(e),
                execution_time=time.time() - start_time
            )
    
    def restore_checkpoint(self, checkpoint_id: int) -> bool | SessionError:
        self.update_activity()
        start_time = time.time()
        try:
            if checkpoint_id not in self.checkpoints:
                return SessionError(
                    error=f"Checkpoint {checkpoint_id} not found",
                    execution_time=time.time() - start_time
                )
        
            success = self.restore_state(checkpoint_id)
            return success
        except Exception as e:
            return SessionError(
                error=str(e),
                execution_time=time.time() - start_time
            )