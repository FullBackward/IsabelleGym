
import uuid
from collections import OrderedDict
from typing import List, Optional, Any, Dict, Union
import asyncio
import time

import threading
from repl.src.python.repl_backend_gateway import ReplBackendGatewayProcess
from repl.src.python.thy_init import ThyInit
from server.app.services.session import _Isabelle_Session, SessionStatus
from server.app.core.config import Server
from server.app.services.threaded_backend import ThreadedBackend
from server.app.errors import *

# Global resources (shared across all sessions)
_gateway_lock = threading.Lock()

class SessionManager:
    """Manages all active Isabelle sessions"""

    def __init__(self, idle_timeout: float = 300, pool_size: int = Server.DEFAULT_POOL_SIZE, initial_sessions: int = Server.INITIAL_SESSIONS):
        self.idle_timeout = idle_timeout
        self.pool_size = pool_size

        self.gateway: Optional[ReplBackendGatewayProcess] = None
        self.thy_init: Optional[ThyInit] = None

        self._lru: "OrderedDict[uuid.UUID, _Isabelle_Session]" = OrderedDict()

        self._lock = threading.Lock()

        self._cleanup_task: Optional[asyncio.Task] = None

    # Helpers

    def _where(self, func_name: str) -> str:
        return f"{__name__}.{self.__class__.__name__}.{func_name}"
    
    def _ensure_gateway(self):
        where = self._where("_ensure_gateway")
        try:
            with _gateway_lock:
                if self.gateway is None:
                    self.gateway = ReplBackendGatewayProcess()
        except Exception as e:
            raise GatewayUnavailable(f"{where}: failed to start REPL gateway: {e}") from e
    
    def _normalize_field(self, field: Optional[str]) -> str:
        if field is None:
            return Server.DEFAULT_FIELD
        f = str(field).strip()
        if f == "" or f.lower() in {"null", "none", "default"}:
            return Server.DEFAULT_FIELD
        return f

    def _normalize_session_id(self, session_id: Union[str, uuid.UUID]) -> uuid.UUID:
        where = self._where("_normalize_session_id")
        if isinstance(session_id, uuid.UUID):
            return session_id
        try:
            return uuid.UUID(str(session_id))
        except Exception as e:
            raise SessionError(f"{where}: invalid session_id '{session_id}'") from e

    # Lifecycle

    async def startup(self) -> None:
        where = self._where("startup")
        if Server.INITIAL_SESSIONS <=0 :
            if Server.INITIAL_SESSIONS == 0:
                print(f"{where}: skipping pool pre-warming (INITIAL_SESSIONS=0)")
            else:
                Server.INITIAL_SESSIONS = 2
                print(f"{where}: invalid INITIAL_SESSIONS={Server.INITIAL_SESSIONS}, must be >= 0. Defaulting to 2.")
        try:
            self._ensure_gateway()
        except Exception as e:
            if isinstance(e, GatewayUnavailable):
                raise
            raise GatewayUnavailable(f"{where}: gateway startup failed: {e}") from e
        try:
            if self.thy_init is None:
                self.thy_init = ThyInit()
        except Exception as e:
            raise SessionStartError(f"{where}: failed to initialize ThyInit: {e}") from e
        for i in range(Server.INITIAL_SESSIONS):
            try:
                await self._create_session()
            except Exception as e:
                if isinstance(e, (GatewayUnavailable, SessionStartError, SessionError)):
                    raise SessionStartError(f"{where}: failed to pre-start session {i}: {e}") from e
                raise SessionStartError(f"{where}: failed to pre-start session {i}: {e}") from e

    async def shutdown(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except Exception:
                pass

        with self._lock:
            sessions = list(self._lru.values())
            self._lru.clear()

        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

        if self.gateway is not None:
            try:
                self.gateway.terminate()
            except Exception:
                pass
            self.gateway = None
        
        if self.thy_init is not None:
            try:
                self.thy_init.cleanup()
            except Exception:
                pass
            self.thy_init = None
    
    # Services

    async def _create_session(
        self,
        initial_thys: List[str] = None,
        field: str = Server.DEFAULT_FIELD
    ) -> _Isabelle_Session:
        try:
            if initial_thys is None:
                initial_thys = ["$ISABELLE_REPL_HOME/thys/IsabelleREPL"]
            else:
                initial_thys = ["$ISABELLE_REPL_HOME/thys/" + self.thy_init.gen_file("main", initial_thys).data]
            java_list = self.gateway.gateway.jvm.java.util.ArrayList()
            for thy in initial_thys:
                java_list.add(thy)
            if field is None or field == "" or str(field).lower() == "null":
                field = Server.DEFAULT_FIELD
        except Exception as e:
            raise RuntimeError(f"{__name__}._create_session: Failed to generate initial thy file: {e}")
        session_id = uuid.uuid4()

        try:
            raw_backend = await asyncio.to_thread(
                self.gateway.get_repl_backend_with_initial_theories,
                show_states=Server.SHOW_STATES,
                enable_cache=Server.ENABLE_CACHE,
                max_cache_size=Server.MAX_CACHE_SIZE,
                enable_memory_management=Server.ENABLE_MEMORY_MANAGEMENT,
                initial_thys=java_list,
                field=field
            )
        except Exception as e:
            raise RuntimeError(f"{__name__}._create_session: Failed to create raw backend for session {session_id}: {e}")

        try:
            backend = ThreadedBackend(raw_backend, name = f"isabelle-{session_id.hex[:8]}")
        except Exception as e:
            raise RuntimeError(f"{__name__}._create_session: Failed to create threaded backend for session {session_id}: {e}")

        try:
            session = _Isabelle_Session(
                session_id=session_id,
                session_theories=initial_thys,
                session_field=field,
                backend=backend
            )
        except Exception as e:
            backend.close()
            raise RuntimeError(f"{__name__}._create_session: Failed to create session object for session {session_id}: {e}")
        
        try:
            with self._lock:
                self._lru[session_id] = session
                self._lru.move_to_end(session_id, last=True)
                self._evict_if_needed_locked()
        except Exception as e:
            session.close()
            raise RuntimeError(f"{__name__}._create_session: Failed to add session {session_id} to session manager: {e}")
        
        return session
    
    def _evict_if_needed_locked(self) -> None:
        while len(self._lru) > self.pool_size:
            oldest_id, oldest = self._lru.popitem(last=False)
            try:
                oldest.close()
            except Exception:
                pass

    async def create_session(self, theories: Optional[List[str]] = None, field: str = Server.DEFAULT_FIELD) -> _Isabelle_Session:
        try:
            return await self._create_session(theories, field)
        except Exception as e:
            raise SessionStartError(f"{self._where('create_session')}: Failed to create session: {e}") from e

    def get_session(self, session_id: Union[str, uuid.UUID]) -> _Isabelle_Session:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            if sid not in self._lru:
                raise KeyError(f"Session {sid} not found")

            session = self._lru[sid]
            if session.status == SessionStatus.CLOSED:
                raise RuntimeError(f"Session {sid} is closed")

            # bump LRU
            self._lru.move_to_end(sid, last=True)
            return session

    def close_session(self, session_id: Union[str, uuid.UUID]) -> None:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.pop(sid, None)
        if session is None:
            raise KeyError(f"Session {sid} not found")
        session.close()
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": str(sid),
                    "created_at": session.created_at,
                    "last_activity": session.last_activity,
                    "status": session.status.value,
                    "theories": session.theories,
                    "commands_executed": len(session.command_history),
                }
                for sid, session in self._lru.items()
            ]

    
    async def cleanup_idle_sessions(self) -> None:
        while True:
            await asyncio.sleep(60)

            now = time.time()
            to_close: List[uuid.UUID] = []

            with self._lock:
                for sid, session in list(self._lru.items()):
                    if session.status != SessionStatus.CLOSED and session.is_idle(self.idle_timeout, now=now):
                        to_close.append(sid)

            for sid in to_close:
                self.close_session(sid)

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self.cleanup_idle_sessions())
    
    def get_lru_info(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_sessions": len(self._lru),
                "max_pool_size": self.pool_size,
            }
    
