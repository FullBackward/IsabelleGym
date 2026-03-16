from __future__ import annotations


class SessionError(Exception):
    """Base class for session-related errors with optional execution metadata."""

    def __init__(
        self,
        message: str | None = None,
        *,
        error: str | None = None,
        execution_time: float | None = None,
    ) -> None:
        detail = error or message or self.__class__.__name__
        super().__init__(detail)
        self.error = detail
        self.execution_time = execution_time


class SessionNotFound(SessionError):
    pass


class SessionStartError(SessionError):
    pass


class GatewayUnavailable(SessionError):
    pass
