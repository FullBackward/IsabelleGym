from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from server.app.services.session_manager import SessionManager
from datetime import datetime
import time


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