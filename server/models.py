from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class SessionCreateRequest(BaseModel):
    theories: Optional[List[str]] = None
    enable_cache: bool = True
    session_name: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str


class CommandRequest(BaseModel):
    command: str
    timeout: Optional[float] = 30.0


class CommandResponse(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    subgoals: List[str]
    execution_time: float


class ProofStateResponse(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    current_theory: str


class StateCheckpoint(BaseModel):
    checkpoint_id: int
    timestamp: float


class ProofAttemptRequest(BaseModel):
    theorem: str
    agent_type: str = "simple_mcts"
    max_steps: int = 100
    timeout: float = 60.0


class ProofAttemptResponse(BaseModel):
    proof_id: str
    status: str
    message: str


class ProofStatusResponse(BaseModel):
    proof_id: str
    status: str
    progress: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
