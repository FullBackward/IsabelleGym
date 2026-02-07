from enum import Enum
import time

from fastapi import HTTPException
from server_gym.success_checker import is_syntax_successful, get_error_message
from typing import List, Dict, Any
from server.app.api.v1.schemas.API_models import CommandResponse, ProofStateResponse, StateCheckpoint
from server.local_gym.session_pool import return_backend # type: ignore

class SessionStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    ERROR = "error"

# Manages a single Isabelle proving session
class _Isabelle_Session:
    """Manages a single Isabelle proving session"""
    
    def __init__(
        self,
        session_id: str,
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
        """Execute Isar command"""
        return self.backend.step(command)
    
    def open_subgoals(self):
        """Get current subgoals"""
        subgoals = self.backend.open_subgoals()
        return [s.strip() for s in subgoals]
    
    def proof_finished(self):
        """Check if proof is complete"""
        return len(self.open_subgoals()) == 0
    
    def get_source(self):
        """Get theory source"""
        return self.backend.get_source()
    
    @property
    def current_thy(self):
        """Get current theory name"""
        return self.backend.current_thy_name_string()
    
    def save_state(self):
        """Save current state"""
        return self.backend.save_state()
    
    def restore_state(self, state_id):
        """Restore saved state"""
        return self.backend.restore_state(state_id)
    
    def rollback(self):
        """Undo last step"""
        return self.backend.rollback()
    
    def enter_thy(self, thy_name: str):
        """Enter theory"""
        return self.backend.enter_thy(thy_name)
    
    def close(self):
        """Return backend to pool"""
        if not self._closed:
            return_backend(self.backend)
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
    
    def execute_command(self, command: str, timeout: float = 30.0) -> CommandResponse:
        """Execute an Isar command"""
        self.update_activity()
        start_time = time.time()
        
        try:
            result = self.gym.step(command)
            execution_time = time.time() - start_time
            
            success = is_syntax_successful(result)
            subgoals = self.gym.open_subgoals()
            
            # Record in history
            self.command_history.append({
                'command': command,
                'timestamp': start_time,
                'success': success,
                'subgoals_count': len(subgoals)
            })
            
            return CommandResponse(
                success=success,
                output=result.total_output() if hasattr(result, 'total_output') else None,
                error=get_error_message(result) if not success else None,
                subgoals=subgoals,
                execution_time=execution_time
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            return CommandResponse(
                success=False,
                output=None,
                error=str(e),
                subgoals=[],
                execution_time=execution_time
            )
    
    def get_proof_state(self) -> ProofStateResponse:
        """Get current proof state"""
        self.update_activity()
        
        try:
            subgoals = self.gym.open_subgoals()
            current_thy = self.gym.current_thy
            
            return ProofStateResponse(
                subgoals=subgoals,
                proof_finished=len(subgoals) == 0,
                current_theory=current_thy
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    def save_checkpoint(self) -> StateCheckpoint:
        """Save current state"""
        self.update_activity()
        checkpoint_id = self.gym.save_state()
        timestamp = time.time()
        self.checkpoints[checkpoint_id] = timestamp
        
        return StateCheckpoint(
            checkpoint_id=checkpoint_id,
            timestamp=timestamp
        )
    
    def restore_checkpoint(self, checkpoint_id: int) -> bool:
        """Restore from checkpoint"""
        self.update_activity()
        
        if checkpoint_id not in self.checkpoints:
            raise HTTPException(
                status_code=404,
                detail=f"Checkpoint {checkpoint_id} not found"
            )
        
        success = self.gym.restore_state(checkpoint_id)
        return success
    
    def close(self):
        """Close the session and cleanup resources"""
        try:
            self.gym.close()
            self.status = SessionStatus.CLOSED
        except Exception as e:
            print(f"Error closing session {self.session_id}: {e}")