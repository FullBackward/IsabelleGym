from __future__ import annotations

import asyncio
import time
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])

# Untested code; WebSocket endpoint for real-time updates


class ConnectionManager:
    """Manages WebSocket connections per session_id."""

    def __init__(self) -> None:
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket

    async def disconnect(self, session_id: str) -> None:
        ws = self.active_connections.pop(session_id, None)
        if ws is not None:
            # nothing else to do; ws disconnects itself
            pass

    async def send_update(self, session_id: str, message: dict) -> None:
        ws = self.active_connections.get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json(message)
        except Exception:
            await self.disconnect(session_id)


manager = ConnectionManager()


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time updates.

    Expects JSON messages with:
      - {"type": "execute_command", "command": "..."}
      - {"type": "get_state"}
      - {"type": "ping"}
    """
    # Access SessionManager set in app.state.session_manager (your lifespan does this)
    session_manager = websocket.app.state.session_manager

    # Verify session exists
    try:
        session = session_manager.get_session(session_id)
    except Exception:
        await websocket.close(code=1008, reason="Session not found")
        return

    await manager.connect(session_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            command_type = data.get("type")

            if command_type == "execute_command":
                command = data.get("command")
                if not isinstance(command, str) or not command.strip():
                    await websocket.send_json(
                        {"type": "command_result", "success": False, "error": "Missing command"}
                    )
                    continue

                # IMPORTANT: Isabelle work can block -> run in thread
                result = await asyncio.to_thread(session.execute_command, command)

                await websocket.send_json(
                    {
                        "type": "command_result",
                        "success": getattr(result, "success", False),
                        "subgoals": getattr(result, "subgoals", []),
                        "output": getattr(result, "output", None),
                        "error": getattr(result, "error", None),
                        "execution_time": getattr(result, "execution_time", None),
                    }
                )

            elif command_type == "get_state":
                # IMPORTANT: also thread it (may call backend)
                state = await asyncio.to_thread(session.get_proof_state)

                await websocket.send_json(
                    {
                        "type": "proof_state",
                        "subgoals": getattr(state, "subgoals", []),
                        "proof_finished": getattr(state, "proof_finished", False),
                        "current_theory": getattr(state, "current_theory", None),
                    }
                )

            elif command_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})

            else:
                await websocket.send_json(
                    {"type": "error", "error": f"Unknown message type: {command_type}"}
                )

    except WebSocketDisconnect:
        await manager.disconnect(session_id)
    except Exception as e:
        # Don't crash the server; close this connection
        try:
            await websocket.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
        await manager.disconnect(session_id)
        try:
            await websocket.close(code=1011, reason="WebSocket internal error")
        except Exception:
            pass