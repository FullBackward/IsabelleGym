from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from server.app.core.config import Timeouts
from server.app.core.diagnostic_guard import validate_diagnostic_command


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


class EnterTheoryRequest(BaseModel):
    imports: Optional[List[str]] = Field(
        default=None,
        description="If given, the server begins the theory with a correctly-quoted "
                    "'theory <name> imports ... begin' header. If omitted, the caller must "
                    "supply the header itself (e.g. a corpus .thy file).",
    )


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


class SledgehammerRequest(BaseModel):
    timeout_s: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Isabelle sledgehammer timeout in seconds (1–300).",
    )


class SledgehammerResponse(BaseModel):
    success: bool
    suggestions: List[str]
    raw_output: str
    execution_time: float


class DiagnosticRequest(BaseModel):
    command: str = Field(
        description="A single READ-ONLY Isabelle diagnostic command: thm, term, prop, typ, "
        "prf, find_theorems, find_consts, or any print_*/find_* inspector. The command runs "
        "transiently (it does NOT alter the proof script). Code-executing / IO commands (ML, "
        "setup, *_file, ...) are rejected with HTTP 422.",
    )
    timeout: Optional[float] = Timeouts.COMMAND_DEFAULT

    @field_validator("command")
    @classmethod
    def _gatekeep_command(cls, v: str) -> str:
        # Raises ValueError -> FastAPI returns 422 with the reason. Keeps the
        # allowlist/denylist policy in one place (core.diagnostic_guard).
        return validate_diagnostic_command(v)


class DiagnosticResponse(BaseModel):
    success: bool = Field(description="False if the command produced an error message.")
    output: str | None = Field(
        default=None,
        description="The diagnostic's writeln/state output, e.g. the theorem statement for "
        "`thm`, the matches for `find_theorems`, the printed term for `term`.",
    )
    error: str | None = None
    execution_time: float


class CommandMessage(BaseModel):
    sev: str = Field(description="Message severity: 'error' or 'warning'.")
    text: str


class CommandStatus(BaseModel):
    index: int = Field(description="Command position in the node (source order).")
    line: int = Field(description="1-based start line of the command WITHIN the submitted chunk.")
    node_line: Optional[int] = Field(
        default=None, description="Absolute 1-based line in the accumulated theory node (debug).")
    kind: str = Field(description="Command keyword, e.g. 'have', 'lemma', 'by'.")
    status: str = Field(description="One of: ok | failed | running | unprocessed.")
    messages: List[CommandMessage] = Field(default_factory=list)


class ChunkVerifyRequest(BaseModel):
    chunk: str = Field(description="A whole proof chunk (one or more Isar commands).")
    timeout: float = Field(
        default=Timeouts.COMMAND_DEFAULT,
        description="Single overall wall budget (seconds) for checking the chunk. "
                    "On expiry the report is partial; no per-command timeouts are raised.",
    )

    @field_validator("chunk")
    @classmethod
    def _non_empty_chunk(cls, v: str) -> str:
        # An empty/whitespace chunk would otherwise return success=False with
        # zero commands and no error — indistinguishable from a real failure.
        if not v or not v.strip():
            raise ValueError("chunk must contain at least one Isar command")
        return v


class ChunkVerifyResponse(BaseModel):
    success: bool = Field(description="True iff every command is 'ok' and not timed out. "
                                      "NOTE: this means 'no command errors', NOT 'theorem proved' "
                                      "— check `proof_open` for that.")
    proof_open: bool = Field(
        default=False,
        description="True if the chunk left an UNCLOSED proof (e.g. `theorem ... using assms` "
                    "or a trailing `have ...` with no `qed`). The chunk is still kept (so you "
                    "can sledgehammer the open goal), but the theorem is NOT proved; close it "
                    "or rollback before starting a new theorem/lemma. A fully proved chunk has "
                    "success=True and proof_open=False.",
    )
    used_sorry: bool = Field(
        default=False,
        description="True if the chunk contains a `sorry` or `oops` command (detected on the "
                    "PARSED commands, so occurrences in comments/strings don't count). Such a "
                    "theorem is NOT actually proved. A genuinely proved chunk has success=True, "
                    "proof_open=False, and used_sorry=False.",
    )
    timed_out: bool = Field(description="True if the overall wall budget elapsed.")
    stuck_line: int | None = Field(
        default=None,
        description="On timeout, the line still 'running' (the likely loop), if any.",
    )
    commands: List[CommandStatus]
    execution_time: float
    error: str | None = Field(
        default=None,
        description="Backend-level error when nothing could be checked (e.g. "
                    "'theory not begun'). None when commands were processed.",
    )