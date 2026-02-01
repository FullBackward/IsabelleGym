from fastapi import APIRouter, Depends, HTTPException
from .schemas.API_models import *
from datetime import datetime
import time

router = APIRouter()

from server.app.dependencies import get_session_manager


@router.get("/")
async def root(session_manager = Depends(get_session_manager)):
    """Root endpoint with server info"""
    return {
        "service": "IsabelleGym Server",
        "version": "1.0.0",
        "status": "running",
        "active_sessions": len(session_manager.sessions),
        "timestamp": datetime.now().isoformat()
    }


@router.get("/health")
async def health_check(session_manager = Depends(get_session_manager)):
    """Health check endpoint"""
    return {
        "status": "healthy",
        "active_sessions": len(session_manager.sessions),
        "timestamp": time.time()
    }


# Session Management Endpoints

@router.post("/api/v1/sessions", response_model=SessionResponse)
async def create_session(request: SessionCreateRequest, session_manager = Depends(get_session_manager)):
    """Create a new Isabelle proving session"""
    try:
        print(request.theories)
        if request.theories is []:
            request.theories = None

        session = session_manager.create_session(
            theories=request.theories,
            enable_cache=request.enable_cache
        )
        
        return SessionResponse(
            session_id=session.session_id,
            created_at=session.created_at,
            theories=session.theories,
            status=session.status.value
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/sessions")
async def list_sessions(session_manager = Depends(get_session_manager)):
    """List all active sessions"""
    return {"sessions": session_manager.list_sessions()}


@router.get("/api/v1/sessions/{session_id}")
async def get_session_info(session_id: str, session_manager = Depends(get_session_manager)):
    """Get information about a specific session"""
    session = session_manager.get_session(session_id)
    
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "last_activity": session.last_activity,
        "status": session.status.value,
        "theories": session.theories,
        "commands_executed": len(session.command_history),
        "checkpoints": len(session.checkpoints)
    }


@router.delete("/api/v1/sessions/{session_id}")
async def close_session(session_id: str, session_manager = Depends(get_session_manager)):
    """Close a session"""
    success = session_manager.close_session(session_id)
    
    if success:
        return {"message": f"Session {session_id} closed successfully"}
    else:
        raise HTTPException(status_code=404, detail="Session not found")


# Proof Interaction Endpoints

@router.post("/api/v1/sessions/{session_id}/commands", response_model=CommandResponse)
async def execute_command(session_id: str, request: CommandRequest, session_manager = Depends(get_session_manager)):
    """Execute an Isar command in the session"""
    session = session_manager.get_session(session_id)
    
    return session.execute_command(
        command=request.command,
        timeout=request.timeout
    )


@router.get("/api/v1/sessions/{session_id}/state", response_model=ProofStateResponse)
async def get_proof_state(session_id: str, session_manager = Depends(get_session_manager)):
    """Get current proof state"""
    session = session_manager.get_session(session_id)
    return session.get_proof_state()


@router.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session_id: str, session_manager = Depends(get_session_manager)):
    """Get current open subgoals"""
    session = session_manager.get_session(session_id)
    state = session.get_proof_state()
    
    return {
        "subgoals": state.subgoals,
        "count": len(state.subgoals),
        "proof_finished": state.proof_finished
    }


@router.get("/api/v1/sessions/{session_id}/source")
async def get_source(session_id: str, session_manager = Depends(get_session_manager)):
    """Get theory source code"""
    session = session_manager.get_session(session_id)
    
    try:
        source_result = session.gym.get_source()
        return {
            "source": source_result.total_output(),
            "theory": session.gym.current_thy
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# State Management Endpoints

@router.post("/api/v1/sessions/{session_id}/checkpoints", response_model=StateCheckpoint)
async def save_checkpoint(session_id: str, session_manager = Depends(get_session_manager)):
    """Save current state as checkpoint"""
    session = session_manager.get_session(session_id)
    return session.save_checkpoint()


@router.post("/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(session_id: str, checkpoint_id: int, session_manager = Depends(get_session_manager)):
    """Restore from checkpoint"""
    session = session_manager.get_session(session_id)
    success = session.restore_checkpoint(checkpoint_id)
    
    return {
        "success": success,
        "checkpoint_id": checkpoint_id,
        "message": "State restored successfully" if success else "Restoration failed"
    }


@router.post("/api/v1/sessions/{session_id}/rollback")
async def rollback(session_id: str, session_manager = Depends(get_session_manager)):
    """Rollback last command"""
    session = session_manager.get_session(session_id)
    
    try:
        result = session.gym.rollback()
        return {
            "success": True,
            "output": result.total_output() if hasattr(result, 'total_output') else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# History and Statistics

@router.get("/api/v1/sessions/{session_id}/history")
async def get_command_history(session_id: str, limit: int = 50, session_manager = Depends(get_session_manager)):
    """Get command execution history"""
    session = session_manager.get_session(session_id)
    
    history = session.command_history[-limit:]
    return {
        "session_id": session_id,
        "total_commands": len(session.command_history),
        "history": history
    }


@router.get("/api/v1/sessions/{session_id}/stats")
async def get_session_stats(session_id: str, session_manager = Depends(get_session_manager)):
    """Get session statistics"""
    session = session_manager.get_session(session_id)
    
    successful = sum(1 for cmd in session.command_history if cmd.get('success'))
    failed = len(session.command_history) - successful
    
    return {
        "session_id": session_id,
        "created_at": session.created_at,
        "duration": time.time() - session.created_at,
        "last_activity": session.last_activity,
        "total_commands": len(session.command_history),
        "successful_commands": successful,
        "failed_commands": failed,
        "success_rate": successful / len(session.command_history) if session.command_history else 0,
        "checkpoints_saved": len(session.checkpoints)
    }

from server.app.api.v1 import router as ws_router

router.include_router(ws_router)