from __future__ import annotations


class SessionError(Exception):

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


class PoolExhausted(SessionError):
    """Raised when the session pool is full and all sessions are actively
    processing requests, so no eviction candidate is available."""
    pass


class SessionLeaseError(SessionError):
    """Raised when a caller uses an invalid or missing lease token."""
    pass


class SessionBusyError(SessionError):
    """Raised when a session cannot be released/closed because work is in flight."""
    pass
