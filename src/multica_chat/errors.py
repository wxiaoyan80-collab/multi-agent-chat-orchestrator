class PlatformError(Exception):
    """Base class for domain errors raised by the platform service."""

    http_status = 400
    code = "invalid_argument"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


class AuthorizationError(PlatformError):
    http_status = 403
    code = "agent_invoke_denied"


class Unauthenticated(AuthorizationError):
    http_status = 401
    code = "unauthenticated"


class BudgetExceeded(PlatformError):
    http_status = 422
    code = "budget_exceeded"


class LoopDetected(BudgetExceeded):
    pass


class RateLimited(BudgetExceeded):
    http_status = 429
    code = "rate_limited"


class InvalidTransition(PlatformError):
    http_status = 409
    code = "request_already_final"


class Conflict(PlatformError):
    http_status = 409
    code = "idempotency_key_reused"


class PreconditionFailed(PlatformError):
    http_status = 412
    code = "precondition_failed"


class NotFound(PlatformError):
    http_status = 404
    code = "request_not_found"


class TaskNotFound(NotFound):
    code = "task_not_found"


class ValidationError(PlatformError):
    http_status = 400
    code = "invalid_argument"


class ReviewFeedbackRequired(ValidationError):
    code = "review_feedback_required"


class InvalidReviewTransition(ValidationError):
    code = "invalid_review_transition"
