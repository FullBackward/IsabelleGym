from fastapi import Request

from server.app.core.logging import get_logger
from server.app.services.session_manager import SessionManager

logger = get_logger(__name__)



def get_session_manager(request: Request) -> SessionManager:
    session_manager = getattr(request.app.state, "session_manager", None)
    if session_manager is None:
        logger.error("session_manager missing from application state")
        raise RuntimeError("Session manager is not initialized")
    return session_manager
