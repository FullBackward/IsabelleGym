from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from server.session import IsabelleSession, SessionStatus, SessionManager
from server.models import *
from datetime import datetime
import time


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the ML model
    """Initialize background tasks on startup"""
    print("Starting IsabelleGym Server...")
    session_manager.start_cleanup_task()
    yield
    # Clean up the ML models and release the resources
    """Cleanup on shutdown"""
    print("Shutting down IsabelleGym Server...")
    session_manager.shutdown()

session_manager = SessionManager(idle_timeout=300)

app = FastAPI(
    title="IsabelleGym Server",
    description="RESTful API for Isabelle theorem proving",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/")
async def root():
    """Root endpoint with server info"""
    return {
        "service": "IsabelleGym Server",
        "version": "1.0.0",
        "status": "running",
        "active_sessions": len(session_manager.sessions),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "active_sessions": len(session_manager.sessions),
        "timestamp": time.time()
    }


# Session Management Endpoints

@app.post("/api/v1/sessions", response_model=SessionResponse)
async def create_session(request: SessionCreateRequest):
    """Create a new Isabelle proving session"""
    try:
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


@app.get("/api/v1/sessions")
async def list_sessions():
    """List all active sessions"""
    return {"sessions": session_manager.list_sessions()}


@app.get("/api/v1/sessions/{session_id}")
async def get_session_info(session_id: str):
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


@app.delete("/api/v1/sessions/{session_id}")
async def close_session(session_id: str):
    """Close a session"""
    success = session_manager.close_session(session_id)
    
    if success:
        return {"message": f"Session {session_id} closed successfully"}
    else:
        raise HTTPException(status_code=404, detail="Session not found")


# Proof Interaction Endpoints

@app.post("/api/v1/sessions/{session_id}/commands", response_model=CommandResponse)
async def execute_command(session_id: str, request: CommandRequest):
    """Execute an Isar command in the session"""
    session = session_manager.get_session(session_id)
    
    return session.execute_command(
        command=request.command,
        timeout=request.timeout
    )


@app.get("/api/v1/sessions/{session_id}/state", response_model=ProofStateResponse)
async def get_proof_state(session_id: str):
    """Get current proof state"""
    session = session_manager.get_session(session_id)
    return session.get_proof_state()


@app.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session_id: str):
    """Get current open subgoals"""
    session = session_manager.get_session(session_id)
    state = session.get_proof_state()
    
    return {
        "subgoals": state.subgoals,
        "count": len(state.subgoals),
        "proof_finished": state.proof_finished
    }


@app.get("/api/v1/sessions/{session_id}/source")
async def get_source(session_id: str):
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

@app.post("/api/v1/sessions/{session_id}/checkpoints", response_model=StateCheckpoint)
async def save_checkpoint(session_id: str):
    """Save current state as checkpoint"""
    session = session_manager.get_session(session_id)
    return session.save_checkpoint()


@app.post("/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(session_id: str, checkpoint_id: int):
    """Restore from checkpoint"""
    session = session_manager.get_session(session_id)
    success = session.restore_checkpoint(checkpoint_id)
    
    return {
        "success": success,
        "checkpoint_id": checkpoint_id,
        "message": "State restored successfully" if success else "Restoration failed"
    }


@app.post("/api/v1/sessions/{session_id}/rollback")
async def rollback(session_id: str):
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

@app.get("/api/v1/sessions/{session_id}/history")
async def get_command_history(session_id: str, limit: int = 50):
    """Get command execution history"""
    session = session_manager.get_session(session_id)
    
    history = session.command_history[-limit:]
    return {
        "session_id": session_id,
        "total_commands": len(session.command_history),
        "history": history
    }


@app.get("/api/v1/sessions/{session_id}/stats")
async def get_session_stats(session_id: str):
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


# ============================================================================
# WebSocket Support (for real-time updates)
# ============================================================================

class ConnectionManager:
    """Manages WebSocket connections"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        print(f"WebSocket connected for session {session_id}")
    
    async def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
            print(f"WebSocket disconnected for session {session_id}")
    
    async def send_update(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            try:
                await self.active_connections[session_id].send_json(message)
            except Exception as e:
                print(f"Error sending WebSocket message: {e}")
                await self.disconnect(session_id)


manager = ConnectionManager()


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time updates"""
    
    # Verify session exists
    try:
        session = session_manager.get_session(session_id)
    except HTTPException:
        await websocket.close(code=404, reason="Session not found")
        return
    
    await manager.connect(session_id, websocket)
    
    try:
        while True:
            # Receive commands via WebSocket
            data = await websocket.receive_json()
            
            command_type = data.get('type')
            
            if command_type == 'execute_command':
                command = data.get('command')
                result = session.execute_command(command)
                
                await websocket.send_json({
                    'type': 'command_result',
                    'success': result.success,
                    'subgoals': result.subgoals,
                    'output': result.output,
                    'error': result.error
                })
            
            elif command_type == 'get_state':
                state = session.get_proof_state()
                await websocket.send_json({
                    'type': 'proof_state',
                    'subgoals': state.subgoals,
                    'proof_finished': state.proof_finished,
                    'current_theory': state.current_theory
                })
            
            elif command_type == 'ping':
                await websocket.send_json({'type': 'pong', 'timestamp': time.time()})
            
    except WebSocketDisconnect:
        await manager.disconnect(session_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await manager.disconnect(session_id)


# ============================================================================
# Run Server
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    print("Starting IsabelleGym Server...")
    print("API documentation will be available at: http://localhost:8000/docs")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )