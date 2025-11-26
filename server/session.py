from enum import Enum
import time
import uuid
from typing import List, Optional, Dict, Any
import asyncio
from fastapi import HTTPException
try:
    from server_gym.isabelle_gym import IsabelleGym
    #from server_gym.isabelle_agent_interface import IsabelleAgent, ProofResult
    from server_gym.success_checker import is_syntax_successful, get_error_message
except ImportError:
    raise("Warning: IsabelleGym imports not available.")
from server.models import *

class SessionStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    ERROR = "error"

class IsabelleSession:
    """Manages a single Isabelle proving session"""
    
    def __init__(
        self,
        session_id: str,
        theories: List[str],
        enable_cache: bool = True
    ):
        self.session_id = session_id
        self.theories = theories
        self.created_at = time.time()
        self.last_activity = time.time()
        self.status = SessionStatus.ACTIVE
        
        # Initialize IsabelleGym
        self.gym = IsabelleGym(
            enable_cache=enable_cache,
            shared_cache=True,
            initial_thys=theories
        )
        
        self.command_history: List[Dict[str, Any]] = []
        self.checkpoints: Dict[int, float] = {}
    
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


class SessionManager:
    """Manages all active Isabelle sessions"""
    
    def __init__(self, idle_timeout: float = 300):
        self.sessions: Dict[str, IsabelleSession] = {}
        self.idle_timeout = idle_timeout
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def create_session(
        self,
        theories: Optional[List[str]] = None,
        enable_cache: bool = True
    ) -> IsabelleSession:
        """Create a new session"""
        session_id = str(uuid.uuid4())
        
        if theories is None:
            theories = ["$ISABELLE_REPL_HOME/IsabelleREPL"]
        
        session = IsabelleSession(
            session_id=session_id,
            theories=theories,
            enable_cache=enable_cache
        )
        
        self.sessions[session_id] = session
        print(f"Created session {session_id}")
        return session
    
    def get_session(self, session_id: str) -> IsabelleSession:
        """Get session by ID"""
        if session_id not in self.sessions:
            raise HTTPException(
                status_code=404,
                detail=f"Session {session_id} not found"
            )
        
        session = self.sessions[session_id]
        
        if session.status == SessionStatus.CLOSED:
            raise HTTPException(
                status_code=410,
                detail=f"Session {session_id} is closed"
            )
        
        return session
    
    def close_session(self, session_id: str) -> bool:
        """Close a session"""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.close()
            del self.sessions[session_id]
            print(f"Closed session {session_id}")
            return True
        return False
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all active sessions"""
        return [
            {
                'session_id': sid,
                'created_at': session.created_at,
                'last_activity': session.last_activity,
                'status': session.status.value,
                'theories': session.theories,
                'commands_executed': len(session.command_history)
            }
            for sid, session in self.sessions.items()
        ]
    
    async def cleanup_idle_sessions(self):
        """Periodic task to cleanup idle sessions"""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                idle_sessions = [
                    sid for sid, session in self.sessions.items()
                    if session.is_idle(self.idle_timeout)
                ]
                
                for sid in idle_sessions:
                    print(f"Closing idle session {sid}")
                    self.close_session(sid)
                    
            except Exception as e:
                print(f"Error in cleanup task: {e}")
    
    def start_cleanup_task(self):
        """Start the background cleanup task"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self.cleanup_idle_sessions())
    
    def shutdown(self):
        """Shutdown all sessions"""
        print("Shutting down all sessions...")
        for session_id in list(self.sessions.keys()):
            self.close_session(session_id)