class SessionError(Exception):
    """Base class for session-related errors."""

class SessionNotFound(SessionError):
    pass

class SessionStartError(SessionError):
    pass

class GatewayUnavailable(SessionError):
    pass