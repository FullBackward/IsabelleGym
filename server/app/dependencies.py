from fastapi import Request

from server.app.core.logging import get_logger
from server.app.services.session_manager import SessionManager
from server.app.services.heap_pool import HeapPool

logger = get_logger(__name__)



def get_session_manager(request: Request) -> SessionManager:
    session_manager = getattr(request.app.state, "session_manager", None)
    if session_manager is None:
        logger.error("session_manager missing from application state")
        raise RuntimeError("Session manager is not initialized")
    return session_manager


def get_heap_pool(request: Request) -> HeapPool:
    heap_pool = getattr(request.app.state, "heap_pool", None)
    if heap_pool is None:
        logger.error("heap_pool missing from application state")
        raise RuntimeError("Heap pool is not initialized")
    return heap_pool
