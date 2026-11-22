import pytest

from multica_chat.errors import InvalidTransition
from multica_chat.models import RequestStatus, TaskStatus
from multica_chat.state_machine import (
    assert_request_transition,
    assert_task_transition,
    is_terminal_request,
)


def test_request_happy_path_transitions():
    for a, b in [
        (RequestStatus.PENDING, RequestStatus.QUEUED),
        (RequestStatus.QUEUED, RequestStatus.RUNNING),
        (RequestStatus.RUNNING, RequestStatus.COMPLETED),
    ]:
        assert_request_transition(a.value, b.value)


@pytest.mark.parametrize(
    "status",
    [
        RequestStatus.COMPLETED,
        RequestStatus.PARTIAL,
        RequestStatus.FAILED,
        RequestStatus.CANCELLED,
    ],
)
def test_terminal_request_has_no_outgoing_transition(status):
    assert is_terminal_request(status.value)
    for target in (RequestStatus.RUNNING, RequestStatus.COMPLETED):
        with pytest.raises(InvalidTransition):
            assert_request_transition(status.value, target.value)


def test_partial_is_terminal_even_on_late_reply():
    with pytest.raises(InvalidTransition):
        assert_request_transition(RequestStatus.PARTIAL.value, RequestStatus.COMPLETED.value)
    with pytest.raises(InvalidTransition):
        assert_request_transition(RequestStatus.PARTIAL.value, RequestStatus.CANCELLED.value)


def test_cancel_allowed_from_non_terminal_states():
    for source in (RequestStatus.PENDING, RequestStatus.QUEUED, RequestStatus.RUNNING):
        assert_request_transition(source.value, RequestStatus.CANCELLED.value)


def test_task_full_approval_flow():
    for a, b in [
        (TaskStatus.PENDING, TaskStatus.ASSIGNED),
        (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.SUBMITTED),
        (TaskStatus.SUBMITTED, TaskStatus.REVIEWED),
        (TaskStatus.REVIEWED, TaskStatus.APPROVED),
        (TaskStatus.APPROVED, TaskStatus.COMPLETED),
    ]:
        assert_task_transition(a.value, b.value)


def test_task_reject_can_retry_or_fail():
    assert_task_transition(TaskStatus.REVIEWED.value, TaskStatus.REJECTED.value)
    assert_task_transition(TaskStatus.REJECTED.value, TaskStatus.ASSIGNED.value)
    assert_task_transition(TaskStatus.REJECTED.value, TaskStatus.FAILED.value)


def test_task_illegal_skips_and_cancellation():
    with pytest.raises(InvalidTransition):
        assert_task_transition(TaskStatus.PENDING.value, TaskStatus.SUBMITTED.value)
    with pytest.raises(InvalidTransition):
        assert_task_transition(TaskStatus.COMPLETED.value, TaskStatus.ASSIGNED.value)
    assert_task_transition(TaskStatus.SUBMITTED.value, TaskStatus.CANCELLED.value)
