from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from server.app.core.config import Timeouts


class SessionCreateRequest(BaseModel):
    theories: List[str] | None = None
    field: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str
    lease_id: str = Field(description="Exclusive lease identifier required for session-specific endpoints.")


class CommandRequest(BaseModel):
    command: str
    timeout: Optional[float] = Timeouts.COMMAND_DEFAULT


class FailureLocationResponse(BaseModel):
    block_index: int
    chunk_index: int | None = None
    preview: str | None = None


class CommandResponse(BaseModel):
    success: bool
    output: str | None = None
    error: str | None = None
    subgoal_error: str | None = None
    subgoals: List[str]
    execution_time: float
    mode: str | None = None
    diagnostics: List[Any] = Field(default_factory=list)
    failure_location: FailureLocationResponse | None = None
    theory_verified: bool = False


class ProofStateResponse(BaseModel):
    subgoals: List[str]
    proof_finished: bool
    current_theory: str


class StateCheckpoint(BaseModel):
    checkpoint_id: int
    timestamp: float


class ProofAttemptResponse(BaseModel):
    proof_id: str
    status: str
    message: str


class ProofStatusResponse(BaseModel):
    proof_id: str
    status: str
    progress: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None


class SessionAcquireRequest(BaseModel):
    theories: List[str] = Field(default_factory=list)
    field: str | None = None
    reuse_dirty: bool = Field(
        default=True,
        description="If True, reuse sessions that already have commands executed. "
                    "If False, only match sessions with an empty command history.",
    )


class SessionAcquireResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str
    reused: bool = Field(description="True if an existing session was returned, False if a new one was created.")
    lease_id: str = Field(description="Exclusive lease identifier. Pass to /release to return the session to the pool.")


class BigStepTheoryRequest(BaseModel):
    theory_name: str
    dependencies: List[str] = Field(default_factory=list)
    field: str | None = None
    theory: str
    timeout: float = Timeouts.BIGSTEP_DEFAULT
