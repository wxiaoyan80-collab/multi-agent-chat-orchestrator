from .errors import InvalidTransition
from .models import RequestStatus, TaskStatus

# Request: pending -> queued -> running -> completed | failed | partial
# ``partial -> completed`` is a controlled recovery transition for late replies.
REQUEST_TRANSITIONS = {
    RequestStatus.PENDING.value: {RequestStatus.QUEUED.value},
    RequestStatus.QUEUED.value: {
        RequestStatus.RUNNING.value,
        RequestStatus.FAILED.value,
    },
    RequestStatus.RUNNING.value: {
        RequestStatus.COMPLETED.value,
        RequestStatus.FAILED.value,
        RequestStatus.PARTIAL.value,
    },
    RequestStatus.PARTIAL.value: {RequestStatus.COMPLETED.value},
    RequestStatus.COMPLETED.value: set(),
    RequestStatus.FAILED.value: set(),
}

# Supervisor task state machine. ``rejected -> assigned`` is only allowed when
# attempt_count < max_attempts; that guard lives in the service layer.
TASK_TRANSITIONS = {
    TaskStatus.PENDING.value: {TaskStatus.ASSIGNED.value},
    TaskStatus.ASSIGNED.value: {
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.FAILED.value,
    },
    TaskStatus.IN_PROGRESS.value: {
        TaskStatus.SUBMITTED.value,
        TaskStatus.FAILED.value,
    },
    TaskStatus.SUBMITTED.value: {TaskStatus.REVIEWED.value},
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
        RequestStatus.FAILED.value,
        RequestStatus.PARTIAL.value,
    )
