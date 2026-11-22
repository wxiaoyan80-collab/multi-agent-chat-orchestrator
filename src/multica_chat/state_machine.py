from .errors import InvalidTransition
from .models import RequestStatus, TaskStatus

# Request: pending -> queued -> running -> completed | partial | failed | cancelled
# Terminal states have no outgoing transitions. Late replies NEVER reopen them.
REQUEST_TRANSITIONS = {
    RequestStatus.PENDING.value: {
        RequestStatus.QUEUED.value,
        RequestStatus.CANCELLED.value,
    },
    RequestStatus.QUEUED.value: {
        RequestStatus.RUNNING.value,
        RequestStatus.FAILED.value,
        RequestStatus.CANCELLED.value,
    },
    RequestStatus.RUNNING.value: {
        RequestStatus.COMPLETED.value,
        RequestStatus.FAILED.value,
        RequestStatus.PARTIAL.value,
        RequestStatus.CANCELLED.value,
    },
    RequestStatus.COMPLETED.value: set(),
    RequestStatus.PARTIAL.value: set(),
    RequestStatus.FAILED.value: set(),
    RequestStatus.CANCELLED.value: set(),
}

# Supervisor task. ``rejected -> assigned`` only while attempt_count < max_attempts
# (guard lives in the service). Cancellation is allowed before a final decision.
TASK_TRANSITIONS = {
    TaskStatus.PENDING.value: {
        TaskStatus.ASSIGNED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.ASSIGNED.value: {
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.IN_PROGRESS.value: {
        TaskStatus.SUBMITTED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.SUBMITTED.value: {
        TaskStatus.REVIEWED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.REVIEWED.value: {
        TaskStatus.APPROVED.value,
        TaskStatus.REJECTED.value,
    },
    TaskStatus.APPROVED.value: {TaskStatus.COMPLETED.value},
    TaskStatus.REJECTED.value: {
        TaskStatus.ASSIGNED.value,
        TaskStatus.FAILED.value,
    },
    TaskStatus.COMPLETED.value: set(),
    TaskStatus.FAILED.value: set(),
    TaskStatus.CANCELLED.value: set(),
}


def assert_request_transition(current: str, new: str) -> None:
    allowed = REQUEST_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise InvalidTransition(f"request: {current} -> {new} is not allowed")


def assert_task_transition(current: str, new: str) -> None:
    allowed = TASK_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise InvalidTransition(f"task: {current} -> {new} is not allowed")


def is_terminal_request(status: str) -> bool:
    return status in (
        RequestStatus.COMPLETED.value,
        RequestStatus.PARTIAL.value,
        RequestStatus.FAILED.value,
        RequestStatus.CANCELLED.value,
    )
