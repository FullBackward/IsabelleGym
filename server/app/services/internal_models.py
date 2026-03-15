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
    subgoals: List[str]
    execution_time: float


class BigStepDiagnostic(BaseModel):
    stage: str
    index: int
    success: bool
    preview: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    execution_time: float


class BigStepFailureLocation(BaseModel):
    block_index: int
    chunk_index: Optional[int] = None
    preview: Optional[str] = None


class BigStepExecuteResult(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    execution_time: float
    mode: str = "full"
    diagnostics: List[BigStepDiagnostic] = Field(default_factory=list)
    failure_location: Optional[BigStepFailureLocation] = None


class SessionExecutionError(BaseModel):
    execution_time: float
    error: str


class ProofState(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    current_theory: str


class CheckPointInfo(BaseModel):
    checkpoint_id: int
    timestamp: float
