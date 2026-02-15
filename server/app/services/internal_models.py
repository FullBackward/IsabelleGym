from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from enum import Enum

class SessionStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"

class ExecuteResult(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    subgoals: List[str]
    execution_time: float

class SessionError(Exception):
    execution_time: float
    error: str

class ProofState(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    current_theory: str

class CheckPointInfo(BaseModel):
    checkpoint_id: int
    timestamp: float