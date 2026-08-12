from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class SmallStepExecuteResult(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    subgoal_error: Optional[str] = None
    subgoals: List[str]
    execution_time: float


class BigStepExecuteResult(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    execution_time: float
    subgoals: List[str] = Field(default_factory=list)
    mode: str = "strict_full"
    theory_verified: bool = False


class SessionExecutionError(BaseModel):
    execution_time: float
    error: str


class ProofState(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    pending_qed: bool = False
    current_theory: str


class CheckPointInfo(BaseModel):
    checkpoint_id: int
    timestamp: float
