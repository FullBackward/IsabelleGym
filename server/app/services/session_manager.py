from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

from repl.src.python.repl_backend_gateway import ReplBackendGatewayProcess
from repl.src.python.thy_init import ThyInit
from server.app.core.config import Server
from server.app.core.logging import get_logger, logging_context
from server.app.errors import GatewayUnavailable, SessionError, SessionStartError
from server.app.services.session import SessionStatus, _Isabelle_Session
from server.app.services.threaded_backend import ThreadedBackend

from pathlib import Path
from server.app.services.build_verify import BuildVerifier

_gateway_lock = threading.Lock()
logger = get_logger(__name__)


class SessionManager:
    """Manages all active Isabelle sessions."""

    def __init__(
        self,
        idle_timeout: float = Server.IDLE_TIMEOUT_SECONDS,
        pool_size: int = Server.DEFAULT_POOL_SIZE,
        initial_sessions: int = Server.INITIAL_SESSIONS,
    ):
        self.idle_timeout = idle_timeout
        self.pool_size = pool_size
        self.initial_sessions = initial_sessions

        self.gateway: Optional[ReplBackendGatewayProcess] = None
        self.thy_init: Optional[ThyInit] = None

        self._lru: "OrderedDict[uuid.UUID, _Isabelle_Session]" = OrderedDict()
        self._lock = threading.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

        # add this here
        self.build_verifier = BuildVerifier(
            isabelle_bin="isabelle",
            max_concurrent_builds=max(1, pool_size // 2),
            build_jobs_per_request=1,
            temp_parent=Path("/tmp"),
        )

    def _where(self, func_name: str) -> str:
        return f"{__name__}.{self.__class__.__name__}.{func_name}"

    def _ensure_gateway(self):
        where = self._where("_ensure_gateway")
        try:
            with _gateway_lock:
                if self.gateway is None:
                    logger.info("starting REPL gateway")
                    self.gateway = ReplBackendGatewayProcess()
                    logger.info("REPL gateway started")
        except Exception as e:
            logger.exception("failed to start REPL gateway")
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

    def _normalize_theories(self, theories: Optional[List[str]]) -> List[str]:
        if not theories:
            return []
        cleaned = []
        for theory in theories:
            if theory is None:
                continue
            value = str(theory).strip()
            if value:
                cleaned.append(value)
        return sorted(set(cleaned))

    def build_dependency_key(self, theories: Optional[List[str]], field: Optional[str]) -> str:
        normalized_field = self._normalize_field(field)
        normalized_theories = self._normalize_theories(theories)
        base = f"{normalized_field}::{'|'.join(normalized_theories)}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def _build_wrapper_theory_name(self, theories: Optional[List[str]], field: Optional[str]) -> str:
        dep_key = self.build_dependency_key(theories, field)
        return f"wrap_{dep_key[:16]}"

    async def startup(self) -> None:
        where = self._where("startup")
        initial_sessions = self.initial_sessions
        if initial_sessions < 0:
            logger.warning("invalid initial_sessions=%s; defaulting to 2", initial_sessions)
            initial_sessions = Server.INITIAL_SESSIONS

        if initial_sessions == 0:
            logger.info("skipping pool pre-warming because initial_sessions=0")

        try:
            self._ensure_gateway()
        except Exception as e:
            if isinstance(e, GatewayUnavailable):
                raise
            raise GatewayUnavailable(f"{where}: gateway startup failed: {e}") from e

        try:
            if self.thy_init is None:
                logger.info("initializing ThyInit")
                self.thy_init = ThyInit()
                logger.info("ThyInit initialized")
        except Exception as e:
            logger.exception("failed to initialize ThyInit")
            raise SessionStartError(f"{where}: failed to initialize ThyInit: {e}") from e

        for i in range(initial_sessions):
            try:
                logger.info("pre-warming session %s/%s", i + 1, initial_sessions)
                await self._create_session()
            except Exception as e:
                logger.exception("failed to pre-start session %s", i)
                raise SessionStartError(f"{where}: failed to pre-start session {i}: {e}") from e

    async def shutdown(self) -> None:
        logger.info("session manager shutdown started")
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except Exception:
                pass

        with self._lock:
            sessions = list(self._lru.values())
            self._lru.clear()

        for session in sessions:
            try:
                with logging_context(session_id=session.session_id, field=session.field):
                    logger.info("closing session during shutdown")
                    session.close()
            except Exception:
                logger.exception("failed to close session during shutdown")

        if self.gateway is not None:
            try:
                logger.info("terminating REPL gateway")
                self.gateway.terminate()
            except Exception:
                logger.exception("failed to terminate REPL gateway")
            self.gateway = None

        if self.thy_init is not None:
            try:
                logger.info("cleaning up ThyInit")
                self.thy_init.cleanup()
            except Exception:
                logger.exception("failed to cleanup ThyInit")
            self.thy_init = None
        logger.info("session manager shutdown finished")

    async def _create_session(
        self,
        initial_thys: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
    ) -> _Isabelle_Session:
        where = self._where("_create_session")
        self._ensure_gateway()
        if self.thy_init is None:
            self.thy_init = ThyInit()

        normalized_field = self._normalize_field(field)
        dependency_theories = self._normalize_theories(initial_thys)
        dependency_key = self.build_dependency_key(dependency_theories, normalized_field)
        wrapper_theory: Optional[str] = None

        with logging_context(field=normalized_field):
            try:
                if not dependency_theories:
                    loaded_theories = ["$ISABELLE_REPL_HOME/thys/IsabelleREPL"]
                    logger.info("creating base session with default IsabelleREPL theory")
                else:
                    wrapper_name = self._build_wrapper_theory_name(dependency_theories, normalized_field)
                    logger.info(
                        "generating wrapper theory wrapper=%s dependency_key=%s theories=%s",
                        wrapper_name,
                        dependency_key[:12],
                        dependency_theories,
                    )
                    gen_result = self.thy_init.gen_file(wrapper_name, dependency_theories)
                    if not getattr(gen_result, "data", None):
                        raise RuntimeError(getattr(gen_result, "err", "unknown ThyInit error"))
                    wrapper_theory = f"$ISABELLE_REPL_HOME/thys/{gen_result.data}"
                    loaded_theories = [wrapper_theory]

                java_list = self.gateway.gateway.jvm.java.util.ArrayList()
                for thy in loaded_theories:
                    java_list.add(thy)
            except Exception as e:
                logger.exception("failed to prepare initial theory file")
                raise RuntimeError(f"{where}: failed to generate initial theory file: {e}") from e

            session_id = uuid.uuid4()

            try:
                logger.info("creating raw backend for session_id=%s", session_id)
                raw_backend = await asyncio.to_thread(
                    self.gateway.get_repl_backend_with_initial_theories,
                    show_states=Server.SHOW_STATES,
                    enable_cache=Server.ENABLE_CACHE,
                    max_cache_size=Server.MAX_CACHE_SIZE,
                    enable_memory_management=Server.ENABLE_MEMORY_MANAGEMENT,
                    initial_thys=java_list,
                    field=normalized_field,
                )
            except Exception as e:
                logger.exception("failed to create raw backend for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create raw backend for session {session_id}: {e}") from e

            try:
                backend = ThreadedBackend(raw_backend, name=f"isabelle-{session_id.hex[:8]}")
            except Exception as e:
                logger.exception("failed to create threaded backend for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create threaded backend for session {session_id}: {e}") from e

            try:
                session = _Isabelle_Session(
                    session_id=session_id,
                    session_theories=dependency_theories,
                    loaded_theories=loaded_theories,
                    dependency_key=dependency_key,
                    wrapper_theory=wrapper_theory,
                    session_field=normalized_field,
                    backend=backend,
                )
            except Exception as e:
                backend.close()
                logger.exception("failed to create session object for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create session object for session {session_id}: {e}") from e

            try:
                with self._lock:
                    self._lru[session_id] = session
                    self._lru.move_to_end(session_id, last=True)
                    self._evict_if_needed_locked()
            except Exception as e:
                session.close()
                logger.exception("failed to register session session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to add session {session_id} to session manager: {e}") from e

            logger.info(
                "session created session_id=%s dependency_key=%s wrapper_theory=%s",
                session_id,
                dependency_key[:12],
                wrapper_theory,
            )
            return session

    async def verify_big_step_build(
        self,
        *,
        theory_name: str,
        theory: str,
        field: Optional[str],
        timeout: float,
    ):
        normalized_field = self._normalize_field(field)
        return await self.build_verifier.verify(
            theory_name=theory_name,
            theory_text=theory,
            field=normalized_field,
            timeout=timeout,
        )

    def _evict_if_needed_locked(self) -> None:
        while len(self._lru) > self.pool_size:
            oldest_id, oldest = self._lru.popitem(last=False)
            try:
                with logging_context(session_id=oldest_id, field=oldest.field):
                    logger.info("evicting LRU session because pool is full")
                    oldest.close()
            except Exception:
                logger.exception("failed to close evicted session session_id=%s", oldest_id)

    async def create_session(
        self,
        theories: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
    ) -> _Isabelle_Session:
        try:
            return await self._create_session(theories, field)
        except Exception as e:
            logger.exception("create_session failed")
            raise SessionStartError(f"{self._where('create_session')}: Failed to create session: {e}") from e

    def find_session(
        self,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
    ) -> Optional[_Isabelle_Session]:
        """Find an existing ACTIVE session whose dependency_key matches the
        given theories + field combination.

        Parameters
        ----------
        theories : list of theory names (dependencies)
        field : Isabelle field / session image
        reuse_dirty : if *False*, only return sessions with an empty
                      ``command_history`` (i.e. "clean" sessions that have not
                      been used yet).  When *True* (default), any active
                      matching session can be returned.

        Returns
        -------
        The matching session (moved to the MRU end of the LRU cache), or
        ``None`` if no match was found.
        """
        target_key = self.build_dependency_key(theories, field)
        with self._lock:
            for sid, session in reversed(self._lru.items()):
                if session.status != SessionStatus.ACTIVE:
                    continue
                if session.dependency_key != target_key:
                    continue
                if not reuse_dirty and len(session.command_history) > 0:
                    continue
                # Match found — promote to MRU
                self._lru.move_to_end(sid, last=True)
                logger.info(
                    "found existing session session_id=%s dependency_key=%s",
                    sid,
                    target_key[:12],
                )
                return session
        return None

    async def acquire_session(
        self,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
    ) -> Tuple[_Isabelle_Session, bool]:
        """Find an existing matching session or create a new one.

        Returns
        -------
        (session, reused) – *reused* is ``True`` when an existing session was
        returned, ``False`` when a fresh one was created.
        """
        existing = self.find_session(theories=theories, field=field, reuse_dirty=reuse_dirty)
        if existing is not None:
            return existing, True
        new_session = await self.create_session(theories=theories, field=field)
        return new_session, False

    def get_session(self, session_id: Union[str, uuid.UUID]) -> _Isabelle_Session:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            if sid not in self._lru:
                raise KeyError(f"Session {sid} not found")

            session = self._lru[sid]
            if session.status == SessionStatus.CLOSED:
                raise RuntimeError(f"Session {sid} is closed")

            self._lru.move_to_end(sid, last=True)
            return session

    def close_session(self, session_id: Union[str, uuid.UUID]) -> bool:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.pop(sid, None)
        if session is None:
            raise KeyError(f"Session {sid} not found")
        with logging_context(session_id=sid, field=session.field):
            logger.info("closing session")
            session.close()
        return True

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": str(sid),
                    "created_at": session.created_at,
                    "last_activity": session.last_activity,
                    "status": session.status.value,
                    "theories": session.theories,
                    "loaded_theories": session.loaded_theories,
                    "wrapper_theory": session.wrapper_theory,
                    "dependency_key": session.dependency_key,
                    "field": session.field,
                    "commands_executed": len(session.command_history),
                    "verified_theories": session.verified_theories
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
                logger.info("closing idle session session_id=%s", sid)
                self.close_session(sid)

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            logger.info("starting idle session cleanup task")
            self._cleanup_task = asyncio.create_task(self.cleanup_idle_sessions())

    def get_lru_info(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_sessions": len(self._lru),
                "max_pool_size": self.pool_size,
            }