from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from .schemas.API_models import (
    BigStepTheoryRequest,
    ChunkVerifyRequest,
    ChunkVerifyResponse,
    CommandMessage,
    CommandRequest,
    CommandResponse,
    CommandStatus,
    EnterTheoryRequest,
    ProofStateResponse,
    SessionAcquireRequest,
    SessionAcquireResponse,
    SessionCreateRequest,
    SessionResponse,
    StateCheckpoint,
    SledgehammerRequest,
    SledgehammerResponse,
)
from server.app.core.config import API, Logging
from server.app.core.logging import get_logger, logging_context
from server.app.core import metrics
from server.app.dependencies import get_session_manager
from server.app.errors import SessionLeaseError

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
):
    if request is None:
        request = SessionCreateRequest()

    theories = request.theories if request.theories else None
    field = request.field
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        field = None

    with logging_context(field=field or "default"):
        logger.info("creating session theories=%s", theories or [])
        session, lease_id = await session_manager.create_leased_session(theories=theories, field=field)

        logger.info("session created session_id=%s lease_id=%s", session.session_id, lease_id)
        return SessionResponse(
            session_id=str(session.session_id),
            created_at=session.created_at,
            theories=session.theories or [],
            status=session.status.value if hasattr(session.status, "value") else str(session.status),
            lease_id=lease_id,
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
):
    theories = request.theories if request.theories else None
    field = request.field
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        field = None

    with logging_context(field=field or "default"):
        logger.info(
            "acquire_session requested theories=%s reuse_dirty=%s",
            theories or [],
            request.reuse_dirty,
        )

        session, reused, lease_id = await session_manager.acquire_session(
            theories=theories,
            field=field,
            reuse_dirty=request.reuse_dirty,
        )

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
                messages=[CommandMessage(sev=str(m.get("sev", "")), text=str(m.get("text", "")))
                          for m in (c.get("messages", []) or [])],
            )
            for c in (report.get("commands", []) or [])
        ]
        timed_out = bool(report.get("timed_out", False))
        proof_open = bool(report.get("proof_open", False))
        used_sorry = bool(report.get("used_sorry", False))
        stuck_line = next((c.line for c in commands if c.status == "running"), None)
        success = (not timed_out) and len(commands) > 0 and all(c.status == "ok" for c in commands)
        logger.info(
            "verify_chunk done success=%s proof_open=%s used_sorry=%s timed_out=%s commands=%s stuck_line=%s",
            success, proof_open, used_sorry, timed_out, len(commands), stuck_line,
        )
        return ChunkVerifyResponse(
            success=success,
            proof_open=proof_open,
            used_sorry=used_sorry,
            timed_out=timed_out,
            stuck_line=stuck_line,
            commands=commands,
            execution_time=float(result.get("execution_time", 0.0) or 0.0),
        )


@router.get("/api/v1/sessions/{session_id}/state", response_model=ProofStateResponse)
async def get_proof_state(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        logger.debug("fetching proof state")
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        state = await asyncio.to_thread(session.get_proof_state)
        return ProofStateResponse(
            subgoals=getattr(state, "subgoals", []) or [],
            proof_finished=bool(getattr(state, "proof_finished", False)),
            current_theory=getattr(state, "current_theory", None),
        )


@router.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session_id: str, x_lease_id: str | None = Header(None, alias="X-Lease-Id"), session_manager=Depends(get_session_manager)):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        state = await asyncio.to_thread(session.get_proof_state)
        subgoals = getattr(state, "subgoals", []) or []
        logger.debug("returning %s subgoals", len(subgoals))
        return {
            "subgoals": subgoals,
            "count": len(subgoals),
            "proof_finished": bool(getattr(state, "proof_finished", False)),
        }


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
        output = result.total_output() if hasattr(result, "total_output") else None
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
            suggestions=suggestions,
            raw_output="\n".join(suggestions),
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
        diagnostics=result.diagnostics,
        failure_location=result.failure_location,
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
