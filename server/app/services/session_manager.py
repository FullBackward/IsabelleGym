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
from server.app.errors import GatewayUnavailable, PoolExhausted, SessionBusyError, SessionError, SessionLeaseError, SessionNotFound, SessionStartError
from server.app.services.session import SessionStatus, _Isabelle_Session
from server.app.services.threaded_backend import ThreadedBackend

from pathlib import Path
from server.app.services.build_verify import BuildVerifier

_gateway_lock = threading.Lock()
logger = get_logger(__name__)


class SessionManager:

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
                logger.info("cleaning up ThyInit generated files")
                for filename in list(self.thy_init.created_files):
                    try:
                        self.thy_init.cleanup(filename)
                    except Exception:
                        logger.exception("failed to cleanup ThyInit file=%s", filename)
            except Exception:
                logger.exception("failed to cleanup ThyInit")
            self.thy_init = None
        logger.info("session manager shutdown finished")

    async def _create_session(
        self,
        initial_thys: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
        lease_id: Optional[str] = None,
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

                def _create_backend():
                    with _gateway_lock:
                        return self.gateway.get_repl_backend_with_initial_theories(
                            show_states=Server.SHOW_STATES,
                            enable_cache=Server.ENABLE_CACHE,
                            max_cache_size=Server.MAX_CACHE_SIZE,
                            enable_memory_management=Server.ENABLE_MEMORY_MANAGEMENT,
                            initial_thys=java_list,
                            field=normalized_field,
                        )

                raw_backend = await asyncio.to_thread(_create_backend)
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
                evict_targets: List[Tuple[uuid.UUID, _Isabelle_Session]] = []
                with self._lock:
                    self._lru[session_id] = session
                    if lease_id is not None:
                        session.acquire_lease(lease_id)
                    self._lru.move_to_end(session_id, last=True)
                    # Evict excess sessions, but SKIP sessions that are
                    # actively processing a request or exclusively leased.
                    while len(self._lru) > self.pool_size:
                        # Walk LRU oldest-first looking for an idle candidate.
                        candidate_id: Optional[uuid.UUID] = None
                        for sid in self._lru:
                            if sid == session_id:
                                continue  # never evict the one we just created
                            s = self._lru[sid]
                            if not s.in_use and not s.leased:
                                candidate_id = sid
                                break
                        if candidate_id is None:
                            # Every session is in-use or leased — undo our
                            # insertion and let the caller know the pool is
                            # exhausted.
                            self._lru.pop(session_id, None)
                            session.close()
                            raise PoolExhausted(
                                f"Session pool is full ({self.pool_size} sessions) "
                                f"and all sessions are actively processing requests or leased. "
                                f"Try again later or increase ISABELLE_POOL_SIZE."
                            )
                        evict_targets.append(
                            (candidate_id, self._lru.pop(candidate_id))
                        )
                # Close evicted sessions OUTSIDE the lock.
                for oldest_id, oldest in evict_targets:
                    try:
                        with logging_context(session_id=oldest_id, field=oldest.field):
                            logger.info("evicting idle LRU session because pool is full")
                            oldest.close()
                    except Exception:
                        logger.exception("failed to close evicted session session_id=%s", oldest_id)
            except PoolExhausted:
                raise  # propagate without wrapping
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
        dependencies: Optional[List[str]] = None,
        field: Optional[str],
        timeout: float,
    ):
        normalized_field = self._normalize_field(field)
        return await self.build_verifier.verify(
            theory_name=theory_name,
            theory_text=theory,
            dependencies=dependencies,
            field=normalized_field,
            timeout=timeout,
        )

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

    async def create_leased_session(
        self,
        theories: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
    ) -> Tuple[_Isabelle_Session, str]:
        lease_id = uuid.uuid4().hex[:12]
        try:
            session = await self._create_session(theories, field, lease_id=lease_id)
            return session, lease_id
        except Exception as e:
            logger.exception("create_leased_session failed")
            raise SessionStartError(f"{self._where('create_leased_session')}: Failed to create leased session: {e}") from e

    def _find_and_lease_session_locked(
        self,
        *,
        lease_id: str,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
    ) -> Optional[_Isabelle_Session]:
        """Find a matching session and atomically attach the lease while holding the manager lock."""
        target_key = self.build_dependency_key(theories, field)
        for sid, session in reversed(self._lru.items()):
            if session.status != SessionStatus.ACTIVE:
                continue
            if session.leased:
                continue
            if session.dependency_key != target_key:
                continue
            if not reuse_dirty and len(session.command_history) > 0:
                continue
            if not session.try_acquire_lease(lease_id):
                continue
            self._lru.move_to_end(sid, last=True)
            logger.info(
                "found existing session and leased it atomically session_id=%s dependency_key=%s lease_id=%s",
                sid,
                target_key[:12],
                lease_id,
            )
            return session
        return None

    def find_session(
        self,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
    ) -> Optional[_Isabelle_Session]:
        target_key = self.build_dependency_key(theories, field)
        with self._lock:
            for sid, session in reversed(self._lru.items()):
                if session.status != SessionStatus.ACTIVE:
                    continue
                if session.leased:
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
    ) -> Tuple[_Isabelle_Session, bool, str]:
        lease_id = uuid.uuid4().hex[:12]

        with self._lock:
            existing = self._find_and_lease_session_locked(
                lease_id=lease_id,
                theories=theories,
                field=field,
                reuse_dirty=reuse_dirty,
            )
        if existing is not None:
            logger.info(
                "session leased (reused) session_id=%s lease_id=%s",
                existing.session_id, lease_id,
            )
            return existing, True, lease_id

        new_session = await self._create_session(initial_thys=theories, field=field, lease_id=lease_id)
        logger.info(
            "session leased (new) session_id=%s lease_id=%s",
            new_session.session_id, lease_id,
        )
        return new_session, False, lease_id

    def release_session(self, session_id: Union[str, uuid.UUID], lease_id: str) -> bool:
        """Release the exclusive lease so the session returns to the pool
        for reuse by a future ``acquire_session`` call.

        Unlike ``close_session``, the backend is kept alive.
        """
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.get(sid)
            if session is None:
                raise SessionNotFound(f"Session {sid} not found")
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            session.require_lease(lease_id)
            if session.in_use:
                raise SessionBusyError(f"Session {sid} is busy and cannot be released")
            session.release_lease()
            self._lru.move_to_end(sid, last=True)
        with logging_context(session_id=sid, field=session.field):
            logger.info("releasing session lease back to pool")
        return True

    def get_session(
        self,
        session_id: Union[str, uuid.UUID],
        *,
        lease_id: Optional[str] = None,
        require_lease: bool = False,
    ) -> _Isabelle_Session:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            if sid not in self._lru:
                raise SessionNotFound(f"Session {sid} not found")

            session = self._lru[sid]
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            if require_lease:
                session.require_lease(lease_id)

            self._lru.move_to_end(sid, last=True)
            return session

    def close_session(
        self,
        session_id: Union[str, uuid.UUID],
        *,
        lease_id: Optional[str] = None,
        require_lease: bool = True,
    ) -> bool:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.get(sid)
            if session is None:
                raise SessionNotFound(f"Session {sid} not found")
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            if require_lease:
                session.require_lease(lease_id)
            if session.in_use:
                raise SessionBusyError(f"Session {sid} is busy and cannot be closed")
            self._lru.pop(sid, None)
            if session.leased:
                session.release_lease()
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
                    "verified_theories": session.verified_theories,
                    "in_use": session.in_use,
                    "active_requests": session.active_request_count,
                    "leased": session.leased,
                    "lease_id": session.lease_id,
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
                    if session.leased:
                        continue  # never evict a leased session
                    if session.status != SessionStatus.CLOSED and session.is_idle(self.idle_timeout, now=now):
                        to_close.append(sid)

            for sid in to_close:
                logger.info("closing idle session session_id=%s", sid)
                self.close_session(sid, require_lease=False)

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            logger.info("starting idle session cleanup task")
            self._cleanup_task = asyncio.create_task(self.cleanup_idle_sessions())

    def get_lru_info(self) -> Dict[str, Any]:
        with self._lock:
            sessions = list(self._lru.values())
            return {
                "active_sessions": len(sessions),
                "max_pool_size": self.pool_size,
                "busy_sessions": sum(1 for s in sessions if s.in_use),
                "leased_sessions": sum(1 for s in sessions if s.leased),
            }