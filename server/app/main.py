from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from server.app.services.session_manager import SessionManager
from fastapi.responses import JSONResponse

from server.app.errors import (
    SessionNotFound,
    SessionStartError,
    GatewayUnavailable,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    sm = SessionManager(idle_timeout=600, pool_size=8)
    await sm.startup()
    sm.start_cleanup_task()
    app.state.session_manager = sm
    yield
    await sm.shutdown()

app = FastAPI(
    title="IsabelleGym Server",
    description="RESTful API for Isabelle theorem proving",
    version="0.0.1",
    lifespan=lifespan
)

@app.exception_handler(SessionNotFound)
async def handle_session_not_found(request: Request, exc: SessionNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc) or "Session not found"})

@app.exception_handler(GatewayUnavailable)
async def handle_gateway_unavailable(request: Request, exc: GatewayUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc) or "REPL gateway unavailable"})

@app.exception_handler(SessionStartError)
async def handle_session_start_error(request: Request, exc: SessionStartError):
    return JSONResponse(status_code=500, content={"detail": str(exc) or "Session start failed"})

@app.exception_handler(Exception)
async def handle_uncaught_exception(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from server.app.api.v1.router import router as api_router

app.include_router(api_router)

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