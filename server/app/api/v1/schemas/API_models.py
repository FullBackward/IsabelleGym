from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    theories: List[str] | None = None
    field: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str


class CommandRequest(BaseModel):
    command: str
    timeout: Optional[float] = 30.0


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


class SessionAcquireRequest(BaseModel):
    """Request to find an existing session matching the given dependencies/field, or create one."""
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


class BigStepTheoryRequest(BaseModel):
    theory_name: str
    dependencies: List[str] = Field(default_factory=list)
    field: str | None = None
    theory: str
    timeout: float = 300.0
