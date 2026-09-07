from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pathlib import Path as _Path

from .schemas.API_models import (
    BigStepTheoryRequest,
    ChunkVerifyRequest,
    ChunkVerifyResponse,
    CommandAtLineResponse,
    CommandMessage,
    CommandRange,
    CommandRequest,
    CommandResponse,
    CommandStatus,
    DefinitionResponse,
    DefinitionTarget,
    DiagnosticRequest,
    DiagnosticResponse,
    DocumentLoadRequest,
    DocumentLoadResponse,
    EnterTheoryRequest,
    FactsResponse,
    GoalsResponse,
    HeapBuildRequest,
    HeapEntryResponse,
    HeapGroupInfo,
    HeapGroupsResponse,
    HeapListResponse,
    HeapManifestResponse,
    HeapTheoryFile,
    AvailableHeapsResponse,
    HoverResponse,
    LocatedCommand,
    Position,
    ProofStateResponse,
    SessionAcquireRequest,
    SessionAcquireResponse,
    SessionCreateRequest,
    SessionResponse,
    SledgehammerAtRequest,
    SledgehammerAtResponse,
    StateCheckpoint,
    SledgehammerRequest,
    SledgehammerResponse,
)
from server.app.core.config import API, Heap, Logging, Server
from server.app.core.logging import get_logger, logging_context
from server.app.core import metrics
from server.app.dependencies import get_heap_pool, get_session_manager
from server.app.errors import SessionLeaseError
from server.app.services.heap_pool import HeapNotFound
from server.app.services.internal_models import SessionExecutionError
from server.app.services.unicode_normaliser import normalise_for_isabelle

router = APIRouter()
logger = get_logger(__name__)


def _require_lease_id(x_lease_id: str | None) -> str:
    if not x_lease_id:
        raise SessionLeaseError("Missing X-Lease-Id header")
    return x_lease_id


def _preview(text: str | None, limit: int) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _ascii(text):
    """Normalise RENDERED output (goals, state, query/sledgehammer results, hover
    contents) back to Isabelle's \<name> ASCII notation. The PIDE layer decodes
    escapes to Unicode for display; agents are told to write \<...> — so read
    output should speak the same notation. Uses Isabelle's own symbol table via
    normalise_for_isabelle; gated by Server.ASCII_OUTPUT. Raw document source
    (command_at_line.source, GET .../source) must NOT pass through here."""
    if text is None or not Server.ASCII_OUTPUT:
        return text
    try:
        return normalise_for_isabelle(str(text))
    except FileNotFoundError:
        # Host-side dev without an Isabelle install: degrade to raw output.
        logger.warning("Isabelle symbol table not found — returning raw (Unicode) output")
        return str(text)


def _parse_command_range(raw) -> CommandRange | None:
    """Parse the optional per-command `range` from a backend chunk report.

    Defensive: any malformed shape (missing start/end, non-int line/col) yields
    None so one bad entry never fails the whole report.
    """
    try:
        if not isinstance(raw, dict):
            return None
        start, end = raw["start"], raw["end"]
        return CommandRange(
            start=Position(line=int(start["line"]), col=int(start["col"])),
            end=Position(line=int(end["line"]), col=int(end["col"])),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


@router.get("/")
async def root(session_manager=Depends(get_session_manager)):
    lru = session_manager.get_lru_info() if hasattr(session_manager, "get_lru_info") else {}
    logger.debug("root health endpoint requested")
    gateway_alive = lru.get("gateway_alive", True)
    return {
        "service": "IsabelleGym Server",
        "version": API.VERSION,
        "status": "healthy" if gateway_alive else "degraded",
        "gateway_alive": gateway_alive,
        "active_sessions": lru.get("active_sessions", 0),
        "busy_sessions": lru.get("busy_sessions", 0),
        "max_pool_size": lru.get("max_pool_size", 0),
        "max_concurrent_sledgehammer": lru.get("max_concurrent_sledgehammer", 0),
        "memory_management_enabled": lru.get("memory_management_enabled", False),
        "memory_used_mb": lru.get("memory_used_mb", 0),
        "memory_limit_mb": lru.get("memory_limit_mb", 0),
        "memory_pressure_pct": lru.get("memory_pressure_pct", 0),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/healthz")
async def healthz():
    """Liveness probe: 200 as long as the process serves requests.

    Deliberately does NOT depend on the session manager / gateway — a live but
    not-yet-ready process should restart on readiness, not liveness.
    """
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness probe: 200 only when the session manager is up and the REPL
    gateway is alive; 503 otherwise (so traffic isn't routed to a degraded
    instance)."""
    sm = getattr(request.app.state, "session_manager", None)
    alive = bool(sm is not None and sm.gateway_alive())
    if alive:
        return {"status": "ready", "gateway_alive": True}
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "gateway_alive": alive},
    )


@router.post("/api/v1/sessions", response_model=SessionResponse)
async def create_session(
    request: SessionCreateRequest | None = None,
    session_manager=Depends(get_session_manager),
    heap_pool=Depends(get_heap_pool),
):
    if request is None:
        request = SessionCreateRequest()

    theories = request.theories if request.theories else None
    field = request.field
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        field = None

    task_group = request.task_group or Heap.DEFAULT_TASK_GROUP
    session_dirs = None
    dependency_extra = None
    if request.heap_session or request.project:
        heap_theories, field, session_dirs, dependency_extra = _resolve_heap_for_session(
            heap_pool, task_group, request.heap_session, request.project
        )
        theories = sorted(set((theories or []) + heap_theories))

    with logging_context(field=field or "default"):
        logger.info("creating session theories=%s task_group=%s", theories or [], task_group)
        session, lease_id = await session_manager.create_leased_session(
            theories=theories, field=field, task_group=task_group,
            session_dirs=session_dirs, dependency_extra=dependency_extra,
        )
        session.label = request.label

        logger.info("session created session_id=%s lease_id=%s label=%s task_group=%s", session.session_id, lease_id, request.label, task_group)
        return SessionResponse(
            session_id=str(session.session_id),
            created_at=session.created_at,
            theories=session.theories or [],
            status=session.status.value if hasattr(session.status, "value") else str(session.status),
            lease_id=lease_id,
            label=session.label,
            task_group=task_group,
        )


@router.get("/api/v1/sessions")
async def list_sessions(session_manager=Depends(get_session_manager)):
    sessions = session_manager.list_sessions()
    logger.debug("listed %s sessions", len(sessions))
    return {"sessions": sessions} if sessions else {"sessions": []}


@router.post("/api/v1/sessions/acquire", response_model=SessionAcquireResponse)
async def acquire_session(
    request: SessionAcquireRequest,
    session_manager=Depends(get_session_manager),
    heap_pool=Depends(get_heap_pool),
):
    theories = request.theories if request.theories else None
    field = request.field
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        field = None

    task_group = request.task_group or Heap.DEFAULT_TASK_GROUP
    session_dirs = None
    dependency_extra = None
    if request.heap_session or request.project:
        heap_theories, field, session_dirs, dependency_extra = _resolve_heap_for_session(
            heap_pool, task_group, request.heap_session, request.project
        )
        theories = sorted(set((theories or []) + heap_theories))

    with logging_context(field=field or "default"):
        logger.info(
            "acquire_session requested theories=%s reuse_dirty=%s task_group=%s",
            theories or [],
            request.reuse_dirty,
            task_group,
        )

        session, reused, lease_id = await session_manager.acquire_session(
            theories=theories,
            field=field,
            reuse_dirty=request.reuse_dirty,
            task_group=task_group,
            session_dirs=session_dirs,
            dependency_extra=dependency_extra,
        )
        # Label follows the CURRENT holder: applied on every acquire, fresh or
        # reused, so the admin console never shows a stale creator's label.
        session.label = request.label

        logger.info(
            "acquire_session result session_id=%s reused=%s lease_id=%s",
            session.session_id,
            reused,
            lease_id,
        )
        return SessionAcquireResponse(
            session_id=str(session.session_id),
            created_at=session.created_at,
            theories=session.theories or [],
            status=session.status.value if hasattr(session.status, "value") else str(session.status),
            reused=reused,
            lease_id=lease_id,
            task_group=task_group,
        )


@router.post("/api/v1/sessions/{session_id}/release")
async def release_session(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Release the exclusive lease on a session, returning it to the pool
    for reuse.  Unlike DELETE, the backend stays alive."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        logger.info("releasing session lease")
        session_manager.release_session(session_id, lease_id)
        logger.info("session lease released")
        return {"success": True, "session_id": session_id}


@router.get("/api/v1/sessions/{session_id}")
async def get_session_info(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.debug("fetching session info")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        return {
            "session_id": str(session.session_id),
            "created_at": session.created_at,
            "last_activity": session.last_activity,
            "status": session.status.value if hasattr(session.status, "value") else str(session.status),
            "theories": session.theories,
            "loaded_theories": session.loaded_theories,
            "wrapper_theory": session.wrapper_theory,
            "dependency_key": session.dependency_key,
            "commands_executed": len(session.command_history),
            "checkpoints": len(session.checkpoints),
            "verified_theories": session.verified_theories if hasattr(session, "verified_theories") else [],
            "in_use": session.in_use,
            "active_requests": session.active_request_count,
            "label": session.label,
            "task_group": session.task_group,
        }


@router.delete("/api/v1/sessions/{session_id}")
async def close_session(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        logger.info("closing session")
        await asyncio.to_thread(session_manager.close_session, session_id, lease_id=lease_id)
        logger.info("session closed")
        return {"success": True}


@router.post("/api/v1/sessions/{session_id}/commands", response_model=CommandResponse)
async def execute_command(session_id: str, request: CommandRequest, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        logger.info(
            "executing command timeout=%s preview=%s",
            request.timeout,
            _preview(request.command, Logging.COMMAND_PREVIEW_CHARS),
        )
        result = await asyncio.to_thread(session.execute_command, request.command, request.timeout)
        logger.info(
            "command finished success=%s execution_time=%s",
            getattr(result, "success", False),
            float(getattr(result, "execution_time", 0.0) or 0.0),
        )
        return CommandResponse(
            success=getattr(result, "success", False),
            output=getattr(result, "output", None),
            error=getattr(result, "error", None),
            subgoal_error=getattr(result, "subgoal_error", None),
            subgoals=getattr(result, "subgoals", []) or [],
            execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
        )


@router.post("/api/v1/sessions/{session_id}/verify_chunk", response_model=ChunkVerifyResponse)
async def verify_chunk(session_id: str, request: ChunkVerifyRequest, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        logger.info(
            "verify_chunk timeout=%s preview=%s",
            request.timeout,
            _preview(request.chunk, Logging.COMMAND_PREVIEW_CHARS),
        )
        result = await asyncio.to_thread(session.verify_chunk, request.chunk, request.timeout)
        report = result.get("report", {}) or {}
        commands = [
            CommandStatus(
                index=int(c.get("i", 0)),
                line=int(c.get("line", 0)),
                node_line=c.get("node_line"),
                kind=str(c.get("kind", "")),
                status=str(c.get("status", "unprocessed")),
                range=_parse_command_range(c.get("range")),
                messages=[CommandMessage(sev=str(m.get("sev", "")), text=str(m.get("text", "")))
                          for m in (c.get("messages", []) or [])],
            )
            for c in (report.get("commands", []) or [])
        ]
        timed_out = bool(report.get("timed_out", False))
        proof_open = bool(report.get("proof_open", False))
        pending_qed = bool(report.get("pending_qed", False))
        used_sorry = bool(report.get("used_sorry", False))
        stuck_line = next((c.line for c in commands if c.status == "running"), None)
        success = (not timed_out) and len(commands) > 0 and all(c.status == "ok" for c in commands)
        logger.info(
            "verify_chunk done success=%s proof_open=%s pending_qed=%s used_sorry=%s timed_out=%s commands=%s stuck_line=%s",
            success, proof_open, pending_qed, used_sorry, timed_out, len(commands), stuck_line,
        )
        return ChunkVerifyResponse(
            success=success,
            proof_open=proof_open,
            pending_qed=pending_qed,
            used_sorry=used_sorry,
            timed_out=timed_out,
            stuck_line=stuck_line,
            commands=commands,
            execution_time=float(result.get("execution_time", 0.0) or 0.0),
            error=report.get("error"),
        )


@router.put("/api/v1/sessions/{session_id}/document", response_model=DocumentLoadResponse)
async def load_document(session_id: str, request: DocumentLoadRequest, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Replace the session's whole document with ``text`` (the file-sync primitive
    for read-only, file-mirroring clients). Resets the backend document and all
    session bookkeeping, then re-enters the theory and issues the text as one
    edit. See DocumentLoadRequest for the two header modes."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        logger.info(
            "load_document requested thy_name=%s imports=%s report=%s preview=%s",
            request.thy_name,
            request.imports,
            request.report,
            _preview(request.text, Logging.COMMAND_PREVIEW_CHARS),
        )
        result = await asyncio.to_thread(
            session.load_document, request.text, request.thy_name, request.imports, request.timeout, request.report
        )
        logger.info(
            "load_document finished success=%s execution_time=%s",
            getattr(result, "success", False),
            float(getattr(result, "execution_time", 0.0) or 0.0),
        )
        return DocumentLoadResponse(
            success=getattr(result, "success", False),
            theory=session.entered_thy,
            output=getattr(result, "output", None),
            error=getattr(result, "error", None),
            execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
            report=(session.last_chunk_report or {}).get("report") if request.report else None,
        )


@router.get("/api/v1/sessions/{session_id}/last_report")
async def get_last_report(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """The retained report of the session's MOST RECENT verify_chunk call
    (success or failure). 404 until the first verify_chunk. Cleared by
    load_document; NOT cleared by rollback/restore."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        if session.last_chunk_report is None:
            raise HTTPException(status_code=404, detail="no verify_chunk report yet for this session")
        logger.debug("returning last verify_chunk report")
        return session.last_chunk_report


@router.post("/api/v1/sessions/{session_id}/diagnostic", response_model=DiagnosticResponse)
async def run_diagnostic(session_id: str, request: DiagnosticRequest, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Run a single READ-ONLY diagnostic command (thm, term, find_theorems, print_*, ...)
    and return its output. The command runs transiently and does not alter the proof. Input
    is gatekept by the DiagnosticRequest validator (allowlist of diagnostic keywords + denylist
    of code-execution/IO commands); rejected input returns HTTP 422 before reaching here."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        logger.info(
            "diagnostic requested preview=%s",
            _preview(request.command, Logging.COMMAND_PREVIEW_CHARS),
        )
        result = await asyncio.to_thread(session.run_diagnostic, request.command, request.timeout)
        logger.info(
            "diagnostic finished success=%s execution_time=%s",
            getattr(result, "success", False),
            float(getattr(result, "execution_time", 0.0) or 0.0),
        )
        return DiagnosticResponse(
            success=getattr(result, "success", False),
            output=_ascii(getattr(result, "output", None)),
            error=getattr(result, "error", None),
            execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
        )


@router.get("/api/v1/sessions/{session_id}/state")
async def get_proof_state(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.debug("fetching proof state")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        state = await asyncio.to_thread(session.get_proof_state)
        if isinstance(state, SessionExecutionError):
            logger.warning("proof state fetch failed: %s", state.error)
            return JSONResponse(
                status_code=500,
                content={"error": state.error, "execution_time": state.execution_time},
            )
        return ProofStateResponse(
            subgoals=[_ascii(s) for s in (state.subgoals or [])],
            proof_finished=state.proof_finished,
            pending_qed=state.pending_qed,
            current_theory=state.current_theory,
        )


@router.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        state = await asyncio.to_thread(session.get_proof_state)
        if isinstance(state, SessionExecutionError):
            logger.warning("subgoals fetch failed: %s", state.error)
            return JSONResponse(
                status_code=500,
                content={"error": state.error, "execution_time": state.execution_time},
            )
        subgoals = [_ascii(s) for s in (state.subgoals or [])]
        logger.debug("returning %s subgoals", len(subgoals))
        return {
            "subgoals": subgoals,
            "count": len(subgoals),
            "proof_finished": state.proof_finished,
        }


@router.get("/api/v1/sessions/{session_id}/facts/local", response_model=FactsResponse)
async def get_local_facts(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Read-only probe: facts in the current local proof context. Transient —
    the proof script, rollback chain, and command history are untouched."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        facts = [_ascii(f) for f in await asyncio.to_thread(session.local_facts)]
        logger.debug("returning %s local facts", len(facts))
        return FactsResponse(facts=facts, count=len(facts))


@router.get("/api/v1/sessions/{session_id}/facts/global", response_model=FactsResponse)
async def get_global_facts(session_id: str, limit: int = Query(100, ge=1, le=1000), x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Read-only probe: theory-level facts, sorted by name, capped at ``limit``.
    Transient — the proof script, rollback chain, and command history are untouched."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        facts = [_ascii(f) for f in await asyncio.to_thread(session.global_facts, limit)]
        logger.debug("returning %s global facts (limit=%s)", len(facts), limit)
        return FactsResponse(facts=facts, count=len(facts))


def _parse_located_command(raw) -> LocatedCommand | None:
    """Parse the {kind, source, range} command object of a backend line-query
    reply; None on any malformed shape."""
    try:
        if not isinstance(raw, dict):
            return None
        return LocatedCommand(
            kind=str(raw.get("kind", "")),
            source=str(raw.get("source", "")),
            range=_parse_command_range(raw.get("range")),
        )
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Heap pool (Stage 3)
# ---------------------------------------------------------------------------


def _heap_entry_response(entry) -> HeapEntryResponse:
    return HeapEntryResponse(
        task_group=entry["task_group"],
        project=entry["project"],
        session_name=entry["session_name"],
        root_dir=entry["root_dir"],
        fingerprint=entry["fingerprint"],
        status=entry["status"],
        built_at=entry.get("built_at"),
        built_by=entry.get("built_by"),
        build_log_tail=entry.get("build_log_tail", ""),
    )


def _resolve_heap_for_session(heap_pool, task_group: str, heap_session: str | None, project: str | None):
    """Resolve + gate a heap for session creation (staleness included).

    Returns (theories, field, session_dirs, dependency_extra): the wrapper states
    the QUALIFIED heap theory names (load-bearing — the document's visible context
    comes from the wrapper), the session starts on field=<heap session name> with
    dirs=[root_dir]. HeapPoolError subclasses carry their HTTP status (mapped in
    main.py)."""
    entry = heap_pool.resolve_for_session(task_group, heap_session, project)
    theories = [
        f"{entry['session_name']}.{_Path(f['path']).stem}"
        for f in entry.get("theory_files", [])
    ]
    dependency_extra = f"{task_group}:{entry['fingerprint']}"
    return theories, entry["session_name"], [entry["root_dir"]], dependency_extra


@router.get("/api/v1/sessions/{session_id}/command_at_line", response_model=CommandAtLineResponse)
async def command_at_line(session_id: str, line: int = Query(..., ge=1), x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Read-only jEdit-style query: the command containing `line` (1-based) of the
    current node. Snapshot-based — the proof script, rollback chain, and command
    history are untouched, and it works past a trailing theory `end`."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        result = await asyncio.to_thread(session.command_at_line, line)
        return CommandAtLineResponse(
            found=bool(result.get("found", False)),
            kind=result.get("kind"),
            source=result.get("source"),
            range=_parse_command_range(result.get("range")),
            error=result.get("error"),
        )


@router.get("/api/v1/sessions/{session_id}/goals", response_model=GoalsResponse)
async def goals_at_line(session_id: str, line: int = Query(..., ge=1), x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Read-only jEdit-style query: rendered goal state before/after the command
    containing `line` (1-based). Snapshot-based like command_at_line. Requires
    show_states on (ISABELLE_SHOW_STATES, default true); goal lists are empty
    otherwise."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        result = await asyncio.to_thread(session.goals_at_line, line)
        return GoalsResponse(
            found=bool(result.get("found", False)),
            command=_parse_located_command(result.get("command")),
            goals_before=[_ascii(str(g)) for g in (result.get("goals_before") or [])],
            goals_after=[_ascii(str(g)) for g in (result.get("goals_after") or [])],
            error=result.get("error"),
        )


@router.get("/api/v1/sessions/{session_id}/hover", response_model=HoverResponse)
async def hover_at(session_id: str, line: int = Query(..., ge=1), col: int = Query(..., ge=1), x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Hover info at a 1-based line/col (UTF-16 columns). Snapshot + Rendering —
    no evaluation, no edits; works at any document position."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        result = await asyncio.to_thread(session.hover_at, line, col)
        return HoverResponse(
            found=bool(result.get("found", False)),
            range=_parse_command_range(result.get("range")),
            contents=[_ascii(str(c)) for c in (result.get("contents") or [])],
            error=result.get("error"),
        )


@router.get("/api/v1/sessions/{session_id}/definition", response_model=DefinitionResponse)
async def definition_at(session_id: str, line: int = Query(..., ge=1), col: int = Query(..., ge=1), x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Go-to-definition at a 1-based line/col. Heap/source entities resolve to
    file positions; entry-document entities to in-node line ranges."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        result = await asyncio.to_thread(session.definition_at, line, col)
        targets = [
            DefinitionTarget(**{k: v for k, v in t.items() if k in DefinitionTarget.model_fields})
            for t in (result.get("targets") or [])
            if isinstance(t, dict)
        ]
        return DefinitionResponse(
            found=bool(result.get("found", False)),
            targets=targets,
            error=result.get("error"),
        )


@router.post("/api/v1/sessions/{session_id}/sledgehammer_at", response_model=SledgehammerAtResponse)
async def sledgehammer_at(session_id: str, request: SledgehammerAtRequest, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    """Position-explicit sledgehammer (overlay print op; no text edits). Shares
    the server-wide sledgehammer semaphore with the tip-based endpoint."""
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        start = time.time()
        # Bound concurrent sledgehammers so a burst cannot OOM-kill the gateway.
        sem = getattr(session_manager, "sledgehammer_sem", None)

        async def _run() -> dict:
            return await asyncio.to_thread(
                session.sledgehammer_at, request.line, request.subgoal, request.timeout_s)

        metrics.sledgehammer_inflight.inc()
        try:
            if sem is not None:
                async with sem:
                    result = await _run()
            else:
                result = await _run()
        finally:
            metrics.sledgehammer_inflight.dec()
        return SledgehammerAtResponse(
            found=bool(result.get("found", False)),
            results=[_ascii(str(r)) for r in (result.get("results") or [])],
            error=result.get("error"),
            execution_time=time.time() - start,
        )


@router.get("/api/v1/sessions/{session_id}/source")
async def get_source(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.debug("fetching theory source")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        source_result = await asyncio.to_thread(session.get_source)
        current_thy = await asyncio.to_thread(lambda: session.current_thy)
        source_text = source_result.total_output() if hasattr(source_result, "total_output") else str(source_result)
        return {"source": source_text, "theory": current_thy}


@router.post("/api/v1/sessions/{session_id}/checkpoints", response_model=StateCheckpoint)
async def save_checkpoint(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.info("saving checkpoint")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        cp = await asyncio.to_thread(session.save_checkpoint)
        logger.info("checkpoint saved checkpoint_id=%s", getattr(cp, "checkpoint_id", None))
        return StateCheckpoint(
            checkpoint_id=int(getattr(cp, "checkpoint_id")),
            timestamp=float(getattr(cp, "timestamp")),
        )


@router.post("/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(session_id: str, checkpoint_id: int, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.info("restoring checkpoint checkpoint_id=%s", checkpoint_id)
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        success = await asyncio.to_thread(session.restore_checkpoint, checkpoint_id)
        ok = bool(success) if isinstance(success, bool) else False
        logger.info("checkpoint restore finished success=%s checkpoint_id=%s", ok, checkpoint_id)
        return {
            "success": ok,
            "checkpoint_id": checkpoint_id,
            "message": "State restored successfully" if ok else "Restoration failed",
        }


@router.post("/api/v1/sessions/{session_id}/rollback")
async def rollback(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.info("rolling back latest command")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        result = await asyncio.to_thread(session.rollback)
        output = result.total_output() if hasattr(result, "total_output") else ""
        if output and "No text edits have been made to rollback" in str(output):
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": "no edits to roll back", "output": output},
            )
        return {"success": True, "output": output}

@router.post(
    "/api/v1/sessions/{session_id}/sledgehammer",
    response_model=SledgehammerResponse,
)
async def sledgehammer(
    session_id: str,
    request: SledgehammerRequest,
    x_lease_id: str | None = Header(None, alias="X-Lease-Id"),
    session_manager=Depends(get_session_manager),
):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(
            session_id, lease_id=lease_id, require_lease=True
        )
        logger.info("sledgehammer requested timeout_s=%s", request.timeout_s)
        start = time.time()
        # Bound concurrent sledgehammers so a burst cannot OOM-kill the gateway.
        # Extra requests queue here (backpressure) rather than oversubscribing.
        sem = getattr(session_manager, "sledgehammer_sem", None)

        async def _run() -> list:
            return await asyncio.to_thread(session.sledgehammer, request.timeout_s)

        metrics.sledgehammer_inflight.inc()
        try:
            if sem is not None:
                async with sem:
                    suggestions: list = await _run()
            else:
                suggestions = await _run()
        except Exception:
            metrics.sledgehammer_total.labels("failure").inc()
            raise
        finally:
            metrics.sledgehammer_inflight.dec()
            metrics.sledgehammer_seconds.observe(time.time() - start)
        elapsed = time.time() - start
        found = len(suggestions) > 0
        metrics.sledgehammer_total.labels("success" if found else "failure").inc()
        logger.info(
            "sledgehammer finished found=%s suggestions=%s elapsed=%.2f",
            found, len(suggestions), elapsed,
        )
        return SledgehammerResponse(
            success=found,
            suggestions=[_ascii(str(s)) for s in suggestions],
            raw_output="\n".join(_ascii(str(s)) for s in suggestions),
            execution_time=elapsed,
        )


@router.post("/api/v1/sessions/{session_id}/enter_theory/{theory_name}")
async def enter_theory(session_id: str, theory_name: str, request: EnterTheoryRequest | None = None, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        imports = request.imports if request else None
        logger.info("entering theory theory_name=%s imports=%s", theory_name, imports)
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        await asyncio.to_thread(lambda: session.enter_thy(theory_name, imports=imports))
        return {"success": True, "message": f"Entered theory {theory_name}", "imports": imports}


@router.get("/api/v1/sessions/{session_id}/history")
async def get_command_history(session_id: str, limit: int = 50, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        history = session.command_history[-limit:]
        logger.debug("returning command history entries=%s", len(history))
        return {
            "session_id": session_id,
            "total_commands": len(session.command_history),
            "history": history,
        }


@router.post("/api/v1/sessions/bigstep", response_model=CommandResponse)
async def execute_big_step(request: BigStepTheoryRequest, session_manager=Depends(get_session_manager)):
    result = await session_manager.verify_big_step_build(
        theory_name=request.theory_name,
        theory=request.theory,
        dependencies=request.dependencies,
        field=request.field,
        timeout=request.timeout,
    )
    return CommandResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        subgoals=result.subgoals,
        execution_time=result.execution_time,
        mode=result.mode,
        theory_verified=result.theory_verified,
    )


@router.get("/api/v1/sessions/{session_id}/stats")
async def get_session_stats(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        successful = sum(1 for cmd in session.command_history if cmd.get("success"))
        failed = len(session.command_history) - successful
        logger.debug("returning session stats")
        return {
            "session_id": session_id,
            "created_at": session.created_at,
            "duration": time.time() - session.created_at,
            "last_activity": session.last_activity,
            "total_commands": len(session.command_history),
            "successful_commands": successful,
            "failed_commands": failed,
            "success_rate": (successful / len(session.command_history)) if session.command_history else 0,
            "checkpoints_saved": len(session.checkpoints),
        }


# ---------------------------------------------------------------------------
# Heap pool (Stage 3): verified per-project heaps + task-group tenancy.
# NOTE: no auth — task groups are namespace isolation / accident-proofing, NOT
# a security boundary (same model as the rest of the API).
# Project paths contain slashes, so manifest/delete use a `:path` converter:
# GET /api/v1/heaps/alpha//tmp/hp1 (note the doubled slash).
# ---------------------------------------------------------------------------


@router.post("/api/v1/heaps/build", response_model=HeapEntryResponse)
async def build_heap(request: HeapBuildRequest, heap_pool=Depends(get_heap_pool)):
    """Build or rebuild the heap for (task_group, project). 409 while a build
    for that key is in progress; build passing IS the verification gate."""
    with logging_context():
        logger.info("heap build requested group=%s project=%s", request.task_group, request.project)
        entry = await heap_pool.build(
            request.task_group, request.project, request.session_name,
            built_by=request.task_group,
        )
        return _heap_entry_response(entry)


@router.get("/api/v1/heaps", response_model=HeapListResponse)
async def list_heaps(task_group: str | None = Query(None), heap_pool=Depends(get_heap_pool)):
    """Pool listing; omit task_group to list all groups (admin)."""
    with logging_context():
        return HeapListResponse(
            heaps=[_heap_entry_response(e) for e in heap_pool.list(task_group)]
        )


@router.get("/api/v1/heaps/available", response_model=AvailableHeapsResponse)
async def list_available_heaps(heap_pool=Depends(get_heap_pool)):
    """Admin: every heap image on disk — base session images (user-built, e.g.
    HOL-Analysis, and distribution ones, origin user/distribution) plus
    pool-built images (origin pool). The pool listing above only covers
    pool-built heaps; this is the full "what can sessions start from" view."""
    with logging_context():
        return AvailableHeapsResponse(heaps=heap_pool.list_available_heaps())


@router.get("/api/v1/heaps/{task_group}/{project:path}", response_model=HeapManifestResponse)
async def get_heap_manifest(task_group: str, project: str, heap_pool=Depends(get_heap_pool)):
    """Full manifest: theory files with sha256/mtime, ROOT text, fingerprint,
    status, log tail. The project path follows the group segment verbatim
    (`:path` converter) — e.g. /api/v1/heaps/alpha//tmp/hp1."""
    with logging_context():
        entry = heap_pool.get(task_group, project)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"no heap for group {task_group!r} project {project!r}",
            )
        return HeapManifestResponse(
            **_heap_entry_response(entry).model_dump(),
            root_text=entry.get("root_text", ""),
            theory_files=[HeapTheoryFile(**f) for f in entry.get("theory_files", [])],
        )


@router.delete("/api/v1/heaps/images/{session}")
async def delete_heap_image(session: str, platform: str | None = Query(None), heap_pool=Depends(get_heap_pool)):
    """Admin: delete a base heap image from the USER heaps dir (frees disk).
    Distribution images can never be deleted through this path. Declared
    before the generic {task_group}/{project} DELETE so `images` wins."""
    with logging_context():
        try:
            return heap_pool.delete_heap_image(session, platform)
        except HeapNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))


@router.delete("/api/v1/heaps/{task_group}/{project:path}")
async def delete_heap(task_group: str, project: str, heap_pool=Depends(get_heap_pool)):
    """Admin: remove the heap record (manifest + scratch ROOT). The heap image
    under ~/.isabelle/heaps is left on disk; live sessions are unaffected."""
    with logging_context():
        if not heap_pool.delete(task_group, project):
            raise HTTPException(
                status_code=404,
                detail=f"no heap for group {task_group!r} project {project!r}",
            )
        return {"deleted": True, "task_group": task_group, "project": project}


@router.get("/api/v1/heap_groups", response_model=HeapGroupsResponse)
async def list_heap_groups(heap_pool=Depends(get_heap_pool)):
    with logging_context():
        return HeapGroupsResponse(
            groups=[HeapGroupInfo(**g) for g in heap_pool.groups()]
        )


@router.delete("/api/v1/heap_groups/{group}")
async def delete_heap_group(group: str, heap_pool=Depends(get_heap_pool)):
    """Admin: delete all of a group's heap records. Does NOT kill live sessions
    (they keep their loaded heaps); blocks new session creation against it."""
    with logging_context():
        removed = heap_pool.delete_group(group)
        return {"deleted": removed, "task_group": group}
