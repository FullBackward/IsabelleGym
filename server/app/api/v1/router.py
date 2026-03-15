from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from .schemas.API_models import (
    BigStepTheoryRequest,
    CommandRequest,
    CommandResponse,
    ProofStateResponse,
    SessionCreateRequest,
    SessionResponse,
    StateCheckpoint,
)
from server.app.dependencies import get_session_manager

router = APIRouter()


@router.get("/")
async def root(session_manager=Depends(get_session_manager)):
    """Root endpoint with server info"""
    lru = session_manager.get_lru_info() if hasattr(session_manager, "get_lru_info") else {}
    return {
        "service": "IsabelleGym Server",
        "version": "1.0.0",
        "status": "healthy",
        "active_sessions": lru.get("active_sessions", 0),
        "max_pool_size": lru.get("max_pool_size", 0),
        "timestamp": datetime.now().isoformat(),
    }


@router.post("/api/v1/sessions", response_model=SessionResponse)
async def create_session(
    request: SessionCreateRequest | None = None,
    session_manager=Depends(get_session_manager),
):
    """Create a new Isabelle proving session"""
    if request is None:
        request = SessionCreateRequest()

    theories = request.theories if request.theories else None
    field = request.field
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        field = None

    if asyncio.iscoroutinefunction(session_manager.create_session):
        session = await session_manager.create_session(theories=theories, field=field)
    else:
        session = await asyncio.to_thread(session_manager.create_session, theories=theories, field=field)

    return SessionResponse(
        session_id=str(session.session_id),
        created_at=session.created_at,
        theories=session.theories or [],
        status=session.status.value if hasattr(session.status, "value") else str(session.status),
    )


@router.get("/api/v1/sessions")
async def list_sessions(session_manager=Depends(get_session_manager)):
    """List all active sessions"""
    sessions = session_manager.list_sessions()
    return {"sessions": sessions} if sessions else {"sessions": []}


@router.get("/api/v1/sessions/{session_id}")
async def get_session_info(session_id: str, session_manager=Depends(get_session_manager)):
    """Get information about a specific session"""
    session = session_manager.get_session(session_id)
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
    }


@router.delete("/api/v1/sessions/{session_id}")
async def close_session(session_id: str, session_manager=Depends(get_session_manager)):
    """Close a session"""
    ok = await asyncio.to_thread(session_manager.close_session, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}


@router.post("/api/v1/sessions/{session_id}/commands", response_model=CommandResponse)
async def execute_command(session_id: str, request: CommandRequest, session_manager=Depends(get_session_manager)):
    """Execute an Isar command in the session"""
    session = session_manager.get_session(session_id)
    result = await asyncio.to_thread(session.execute_command, request.command, request.timeout)

    return CommandResponse(
        success=getattr(result, "success", False),
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
        subgoals=getattr(result, "subgoals", []) or [],
        execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
    )


@router.get("/api/v1/sessions/{session_id}/state", response_model=ProofStateResponse)
async def get_proof_state(session_id: str, session_manager=Depends(get_session_manager)):
    """Get current proof state"""
    session = session_manager.get_session(session_id)
    state = await asyncio.to_thread(session.get_proof_state)

    return ProofStateResponse(
        subgoals=getattr(state, "subgoals", []) or [],
        proof_finished=bool(getattr(state, "proof_finished", False)),
        current_theory=getattr(state, "current_theory", None),
    )


@router.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session_id: str, session_manager=Depends(get_session_manager)):
    """Get current open subgoals"""
    session = session_manager.get_session(session_id)
    state = await asyncio.to_thread(session.get_proof_state)

    subgoals = getattr(state, "subgoals", []) or []
    return {
        "subgoals": subgoals,
        "count": len(subgoals),
        "proof_finished": bool(getattr(state, "proof_finished", False)),
    }


@router.get("/api/v1/sessions/{session_id}/source")
async def get_source(session_id: str, session_manager=Depends(get_session_manager)):
    """Get theory source code"""
    session = session_manager.get_session(session_id)
    source_result = await asyncio.to_thread(session.get_source)
    current_thy = await asyncio.to_thread(lambda: session.current_thy)
    source_text = source_result.total_output() if hasattr(source_result, "total_output") else str(source_result)
    return {"source": source_text, "theory": current_thy}


@router.post("/api/v1/sessions/{session_id}/checkpoints", response_model=StateCheckpoint)
async def save_checkpoint(session_id: str, session_manager=Depends(get_session_manager)):
    """Save current state as checkpoint"""
    session = session_manager.get_session(session_id)
    cp = await asyncio.to_thread(session.save_checkpoint)
    return StateCheckpoint(
        checkpoint_id=int(getattr(cp, "checkpoint_id")),
        timestamp=float(getattr(cp, "timestamp")),
    )


@router.post("/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(session_id: str, checkpoint_id: int, session_manager=Depends(get_session_manager)):
    """Restore from checkpoint"""
    session = session_manager.get_session(session_id)
    success = await asyncio.to_thread(session.restore_checkpoint, checkpoint_id)
    ok = bool(success) if isinstance(success, bool) else False
    return {
        "success": ok,
        "checkpoint_id": checkpoint_id,
        "message": "State restored successfully" if ok else "Restoration failed",
    }


@router.post("/api/v1/sessions/{session_id}/rollback")
async def rollback(session_id: str, session_manager=Depends(get_session_manager)):
    """Rollback last command"""
    session = session_manager.get_session(session_id)
    result = await asyncio.to_thread(session.rollback)
    output = result.total_output() if hasattr(result, "total_output") else None
    return {"success": True, "output": output}


@router.post("/api/v1/sessions/{session_id}/enter_theory/{theory_name}")
async def enter_theory(session_id: str, theory_name: str, session_manager=Depends(get_session_manager)):
    """Enter a specific theory"""
    session = session_manager.get_session(session_id)
    await asyncio.to_thread(session.enter_thy, theory_name)
    return {"success": True, "message": f"Entered theory {theory_name}"}


@router.get("/api/v1/sessions/{session_id}/history")
async def get_command_history(session_id: str, limit: int = 50, session_manager=Depends(get_session_manager)):
    """Get command execution history"""
    session = session_manager.get_session(session_id)
    history = session.command_history[-limit:]
    return {
        "session_id": session_id,
        "total_commands": len(session.command_history),
        "history": history,
    }


@router.post("/api/v1/sessions/bigstep", response_model=CommandResponse)
async def execute_big_step(request: BigStepTheoryRequest, session_manager=Depends(get_session_manager)):
    """Execute a big step command with dependency-aware session reuse and localized fallback."""
    dependency_key = session_manager.build_dependency_key(request.dependencies, request.field)

    available_sessions = [
        info
        for info in session_manager.list_sessions()
        if info.get("dependency_key") == dependency_key and info.get("field") == (request.field or info.get("field"))
    ]

    if not available_sessions:
        session = await session_manager.create_session(theories=request.dependencies, field=request.field)
    else:
        available_sessions.sort(key=lambda x: x["last_activity"], reverse=True)
        session = session_manager.get_session(available_sessions[0]["session_id"])

    result = await asyncio.to_thread(
        session.big_step,
        request.theory_name,
        request.theory,
        request.timeout,
    )

    return CommandResponse(
        success=getattr(result, "success", False),
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
        subgoals=getattr(result, "subgoals", []) or [],
        execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
        mode=getattr(result, "mode", None),
        diagnostics=getattr(result, "diagnostics", []) or [],
        failure_location=getattr(result, "failure_location", None),
    )


@router.get("/api/v1/sessions/{session_id}/stats")
async def get_session_stats(session_id: str, session_manager=Depends(get_session_manager)):
    """Get session statistics"""
    session = session_manager.get_session(session_id)
    successful = sum(1 for cmd in session.command_history if cmd.get("success"))
    failed = len(session.command_history) - successful

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
