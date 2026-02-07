import time
from fastapi import Request 
from server.app.core.logging import logger
from starlette.middleware.base import BaseHTTPMiddleware

class LoggerMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        logger.info(f"Request: {request.method} {request.url}")

        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        logger.info(f"{request.method} {request.url.path} [{response.status_code}] ({duration:.3f}s)")

        return response