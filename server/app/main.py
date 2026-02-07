from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from server.app.services.session_manager import SessionManager
from datetime import datetime
import time
from server.app.core.logging import logger
from server.app.middleware import LoggerMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the ML model
    """Initialize background tasks on startup"""
    logger.info("Starting IsabelleGym Server...")
    app.state.session_manager = SessionManager(idle_timeout=600)
    await app.state.session_manager.initialize()
    app.state.session_manager.start_cleanup_task()
    yield
    # Clean up the ML models and release the resources
    """Cleanup on shutdown"""
    logger.info("Shutting down IsabelleGym Server...")
    app.state.session_manager.shutdown()

app = FastAPI(
    title="IsabelleGym Server",
    description="RESTful API for Isabelle theorem proving",
    version="0.0.1",
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

app.add_middleware(LoggerMiddleware)


from server.app.api.v1.router import router as api_router

app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting IsabelleGym Server...")
    logger.info("API documentation will be available at: http://localhost:8000/docs")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )