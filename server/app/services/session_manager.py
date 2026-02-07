
import uuid
from collections import OrderedDict
from typing import List, Optional, Any, Dict, Union
import asyncio

#from server_gym.isabelle_gym import IsabelleGym
#from server_gym.isabelle_agent_interface import IsabelleAgent, ProofResult

import threading
from repl.src.python.repl_backend_gateway import ReplBackendGatewayProcess
from repl.src.python.thy_init import ThyInit
from server.app.services.session import _Isabelle_Session, SessionStatus
from server.app.core import Server

# Global resources (shared across all sessions)
_gateway_lock = threading.Lock()

#Default parameters

# Session manager: manages all sessions with LRU cache
class SessionManager:
    """Manages all active Isabelle sessions"""

    def __init__(self, idle_timeout: float = 300, pool_size: int = Server.DEFAULT_POOL_SIZE, initial_sessions: int = 4):
        self.idle_timeout = idle_timeout
        self.pool_size = pool_size

        self.gateway: Optional[ReplBackendGatewayProcess] = None
        self.thy_init: Optional[ThyInit] = None

        self._lru: "OrderedDict[uuid.UUID, _Isabelle_Session]" = OrderedDict()

        self._lock = threading.Lock()

        self._cleanup_task: Optional[asyncio.Task] = None

    async def startup(self) -> None:
        """Call once from FastAPI lifespan/startup."""
        self._ensure_gateway()
        # ThyInit in your code appears awaitable; keep it async here
        if self.thy_init is None:
            self.thy_init = await ThyInit()
    
    def _ensure_gateway(self):
        """Ensure gateway process is running (only created once)"""
    
        with _gateway_lock:
            if self.gateway is None:
                print("Starting shared Isabelle gateway...")
                self.gateway = ReplBackendGatewayProcess()
                print("Gateway ready")
    
    def _normalize_session_id(self, session_id: Union[str, uuid.UUID]) -> uuid.UUID:
        if isinstance(session_id, uuid.UUID):
            return session_id
        return uuid.UUID(session_id)

    async def _create_session(
        self,
        initial_thys: List[str],
        field: str
    ) -> _Isabelle_Session:
        """Create a new session"""
        if initial_thys is None:
            initial_thys = ["$ISABELLE_REPL_HOME/thys/IsabelleREPL"]
        else:
            initial_thys = ["$ISABELLE_REPL_HOME/thys/" + self.thy_init.gen_file("main", initial_thys).data]
        session_id = uuid.uuid4()
        # Create backend (reuses existing gateway!)
        backend = self.gateway.get_repl_backend_with_initial_theories(
            show_states=True,
            enable_cache=Server.ENABLE_CACHE,
            enable_memory_management=Server.ENABLE_MEMORY_MANAGEMENT,
            initial_thys=initial_thys,
            field=field
        )
        session = _Isabelle_Session(
            session_id=session_id,
            session_theories=initial_thys,
            session_field=field,
            backend=backend
        )
        
        self.LRU_update(session)
        print(f"Created session {session_id}")
        return session
    
    async def init_sessions(self) -> None:
        self._ensure_gateway()

    def LRU_update(self, session: _Isabelle_Session) -> None:
        """Update LRU cache with session"""
        self.LRU[session.session_id] = session
        if len(self.LRU) > self.pool_size:
            # Evict least recently used session
            oldest_session_id = min(self.LRU, key=lambda k: self.LRU[k].last_activity)
            oldest_session = self.LRU[oldest_session_id]
            oldest_session.close()
            del self.LRU[oldest_session_id]
            print(f"Evicted session {oldest_session_id} due to LRU policy")

    def _get_session(self, session_id: str) -> _Isabelle_Session:
        """Get session by ID"""
        if session_id not in self.LRU:
            raise Exception(f"Session {session_id} not found")

        session = self.LRU[session_id]

        if session.status == SessionStatus.CLOSED:
            raise Exception(f"Session {session_id} is closed")
        
        return session
    
    def _close_session(self, session_id: str) -> bool:
        """Close a session"""
        if session_id in self.LRU:
            session = self.LRU[session_id]
            session.close()
            del self.LRU[session_id]
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
            for sid, session in self.LRU.items()
        ]
    
    async def cleanup_idle_sessions(self):
        """Periodic task to cleanup idle sessions"""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                idle_sessions = [
                    sid for sid, session in self.LRU.items()
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
        for session_id in list(self.LRU.keys()):
            self.close_session(session_id)