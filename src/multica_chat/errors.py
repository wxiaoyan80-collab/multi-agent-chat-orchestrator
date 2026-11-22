class PlatformError(Exception):
    """Base class for domain errors raised by the platform service."""


class AuthorizationError(PlatformError):
    """Caller lacks invoke/read/review rights, or session is invalid."""


class BudgetExceeded(PlatformError):
    """A configured limit (participants, concurrency, depth, messages) was hit."""


class LoopDetected(BudgetExceeded):
    """Invocation depth / seen-agent cycle would create a triggering loop."""


class InvalidTransition(PlatformError):
    """A requested state transition is not permitted."""


class NotFound(PlatformError):
    """No persisted record matches the identifier."""


class ValidationError(PlatformError):
    """Malformed request (e.g. missing supervisor id or review feedback)."""
