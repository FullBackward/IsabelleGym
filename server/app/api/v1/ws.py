from __future__ import annotations

import asyncio
import time
import uuid
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.app.core.logging import get_logger, logging_context

router = APIRouter(tags=["ws"])
logger = get_logger(__name__)


class ConnectionManager:
    """Manages WebSocket connections per session_id."""

    def __init__(self) -> None:
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket
        logger.info("websocket connected")

    async def disconnect(self, session_id: str) -> None:
        ws = self.active_connections.pop(session_id, None)
        if ws is not None:
            logger.info("websocket disconnected")

    async def send_update(self, session_id: str, message: dict) -> None:
        ws = self.active_connections.get(session_id)
        if ws is None:
            logger.debug("dropping websocket update because connection is missing")
            return
        try:
            await ws.send_json(message)
        except Exception:
            logger.exception("failed to send websocket update")
            await self.disconnect(session_id)


manager = ConnectionManager()


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    session_manager = websocket.app.state.session_manager
    request_id = websocket.headers.get("x-request-id") or f"ws-{uuid.uuid4().hex[:10]}"

    with logging_context(request_id=request_id, session_id=session_id):
        try:
            session = session_manager.get_session(session_id)
        except Exception:
            logger.warning("websocket rejected because session does not exist")
            await websocket.close(code=1008, reason="Session not found")
            return

        await manager.connect(session_id, websocket)

        try:
            while True:
                data = await websocket.receive_json()
                command_type = data.get("type")
                logger.debug("websocket message received type=%s", command_type)

                if command_type == "execute_command":
                    command = data.get("command")
                    if not isinstance(command, str) or not command.strip():
                        await websocket.send_json(
                            {"type": "command_result", "success": False, "error": "Missing command"}
                        )
                        continue

                    result = await asyncio.to_thread(session.execute_command, command)
                    logger.info(
                        "websocket command finished success=%s execution_time=%s",
                        getattr(result, "success", False),
                        getattr(result, "execution_time", None),
                    )
                    await websocket.send_json(
                        {
                            "type": "command_result",
                            "success": getattr(result, "success", False),
                            "subgoals": getattr(result, "subgoals", []),
                            "output": getattr(result, "output", None),
                            "error": getattr(result, "error", None),
                            "warning": getattr(result, "warning", None),
                            "execution_time": getattr(result, "execution_time", None),
                        }
                    )

                elif command_type == "get_state":
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
                    logger.warning("unknown websocket message type=%s", command_type)
                    await websocket.send_json(
                        {"type": "error", "error": f"Unknown message type: {command_type}"}
                    )

        except WebSocketDisconnect:
            logger.info("websocket client disconnected")
            await manager.disconnect(session_id)
        except Exception as e:
            logger.exception("websocket internal error")
            try:
                await websocket.send_json({"type": "error", "error": str(e)})
            except Exception:
                pass
            await manager.disconnect(session_id)
            try:
                await websocket.close(code=1011, reason="WebSocket internal error")
            except Exception:
                pass
