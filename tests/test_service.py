import pytest

from multica_chat.dispatcher import FakeDispatcher, RunResult, RUN_TIMEOUT
from multica_chat.errors import (
    AuthorizationError,
    BudgetExceeded,
    LoopDetected,
    NotFound,
    ValidationError,
)
from multica_chat.models import ParticipantStatus, RequestStatus, TaskStatus
from multica_chat.security import Session, StaticAuthorizationProvider
from multica_chat.service import MultiTriggerService, TriggerInput
from multica_chat.storage import RequestStore


def test_parallel_success_with_citations(service, dispatcher, parallel_input):
    request = service.trigger("human-token", parallel_input)
    assert request.status == RequestStatus.COMPLETED.value
    assert len(request.participants) == 2
    assert all(p.status == ParticipantStatus.COMPLETED.value for p in request.participants)
    assert {p.run_id for p in request.participants} != {None}
    sources = request.final_answer["sources"]
    assert len(sources) == 2
    assert all(s["run_id"] and s["summary"] for s in sources)


def test_idempotency_and_restart(tmp_path, authz, config):
    db = str(tmp_path / "db.sqlite")
    first_dispatcher = FakeDispatcher()
    store1 = RequestStore(db)
    service1 = MultiTriggerService(store1, first_dispatcher, authz, config)
    inp = TriggerInput("hi", ["a1", "a2"], "same-key")
    r1 = service1.trigger("human-token", inp)
    store1.close()

    # New service/dispatcher over the same DB: resend must not redispatch.
    second_dispatcher = FakeDispatcher()
    store2 = RequestStore(db)
    service2 = MultiTriggerService(store2, second_dispatcher, authz, config)
    r2 = service2.trigger("human-token", inp)
    assert r1.request_id == r2.request_id
    assert len(second_dispatcher.calls) == 0
    assert service2.get_status(r1.request_id, "human-token")["status"] == RequestStatus.COMPLETED.value
    store2.close()


def test_timeout_then_late_reply(service, dispatcher):
    dispatcher.set_response(
        "a2", RunResult("", "a2", RUN_TIMEOUT, error="too slow")
    )
    inp = TriggerInput("hi", ["a1", "a2"], "key-timeout")
    request = service.trigger("human-token", inp)
    assert request.status == RequestStatus.PARTIAL.value
    timed_out = request.participant("a2")
    assert timed_out.status == ParticipantStatus.TIMEOUT.value
    assert request.final_answer["sources"][0]["agent_id"] == "a1"

    dispatcher.set_late_result(
        timed_out.run_id, RunResult("", "a2", "completed", content="late but useful")
    )
    request = service.ingest_late_reply(
        request.request_id, "a2", "late but useful", "human-token"
    )
    assert request.status == RequestStatus.COMPLETED.value
    assert request.participant("a2").status == ParticipantStatus.COMPLETED.value
    assert len(request.final_answer["sources"]) == 2


def test_preview_revocation_is_honored_at_dispatch(service, authz, parallel_input):
    preview = service.preview("human-token", parallel_input)
    assert [p["agent_id"] for p in preview["planned"]] == ["a1", "a2"]
    assert preview["advisory"] is True

    # Permission withdrawn after preview; dispatch must re-check.
    authz.sessions["human-token"].invocable_agents.discard("a2")
    request = service.trigger("human-token", parallel_input)
    assert request.participant("a2").status == ParticipantStatus.FAILED.value
    assert request.participant("a2").error == "not_authorized"
    assert all(c["agent_id"] != "a2" for c in service.dispatcher.calls)
    assert request.status == RequestStatus.PARTIAL.value


def test_private_agent_not_enumerated_or_dispatched(service):
    inp = TriggerInput("hi", ["private"], "key-private")
    preview = service.preview("human-token", inp)
    assert preview["planned"] == []
    assert preview["denied"][0]["agent_id"] == "private"
    request = service.trigger("human-token", inp)
    assert request.status == RequestStatus.FAILED.value
    assert service.dispatcher.calls == []


def test_participant_cap_enforced(service):
    inp = TriggerInput("hi", [f"a{i}" for i in range(6)], "key-cap")
    with pytest.raises(BudgetExceeded):
        service.trigger("human-token", inp)


def test_loop_detection_and_human_override(service):
    with pytest.raises(LoopDetected):
        service.trigger(
            "human-token",
            TriggerInput("hi", ["a1"], "key-loop", seen_agents=["a1"]),
        )
    request = service.trigger(
        "human-token",
        TriggerInput(
            "hi", ["a1"], "key-loop-ok", seen_agents=["a1"], allow_loop_override=True
        ),
    )
    assert request.status == RequestStatus.COMPLETED.value


def test_linear_injects_previous_reply_as_context(service, dispatcher):
    dispatcher.set_response("a1", "first answer")
    dispatcher.set_response("a2", "second answer")
    request = service.trigger(
        "human-token",
        TriggerInput("hi", ["a1", "a2"], "key-linear", mode="linear"),
    )
    assert request.status == RequestStatus.COMPLETED.value
    a2_calls = [c for c in dispatcher.calls if c["agent_id"] == "a2"]
    assert len(a2_calls) == 1
    assert a2_calls[0]["context"] == "first answer"


def _supervisor_input(key="key-sup", participants=None, max_attempts=3):
    return TriggerInput(
        "review this task",
        participants or ["a1", "a2"],
        key,
        mode="supervisor",
        supervisor_agent_id="sup",
        max_attempts=max_attempts,
    )


def test_supervisor_approve_flow(service, dispatcher):
    dispatcher.set_response("a1", "worker one")
    dispatcher.set_response("a2", "worker two")
    request = service.trigger("human-token", _supervisor_input())
    assert request.status == RequestStatus.RUNNING.value
    assert len(request.tasks) == 2
    assert all(t.status == TaskStatus.SUBMITTED.value for t in request.tasks)
    assert all(t.attempt_count == 1 for t in request.tasks)

    for task in list(request.tasks):
        request = service.review_task(
            request.request_id, task.task_id, "human-token", "approve"
        )
    assert request.status == RequestStatus.COMPLETED.value
    assert all(t.status == TaskStatus.COMPLETED.value for t in request.tasks)
    assert len(request.final_answer["sources"]) == 2


def test_supervisor_reject_retry_passes_feedback_to_worker(service, dispatcher):
    dispatcher.set_response("a1", ["v1", "v2"])
    request = service.trigger(
        "human-token", _supervisor_input("key-retry", participants=["a1"])
    )
    task = request.tasks[0]
    assert task.status == TaskStatus.SUBMITTED.value

    request = service.review_task(
        request.request_id,
        task.task_id,
        "human-token",
        "reject",
        {"reject_reason": "too short", "change_instructions": "add detail"},
    )
    task = request.task(task.task_id)
    assert task.attempt_count == 2
    assert task.status == TaskStatus.SUBMITTED.value
    assert task.result == "v2"
    worker_calls = [c for c in dispatcher.calls if c["agent_id"] == "a1"]
    assert worker_calls[1]["payload"]["feedback"]["change_instructions"] == "add detail"

    request = service.review_task(
        request.request_id, task.task_id, "human-token", "approve"
    )
    assert request.status == RequestStatus.COMPLETED.value


def test_supervisor_max_attempts_stops_retrying(service, dispatcher):
    dispatcher.set_response("a1", ["r1", "r2", "r3"])
    request = service.trigger(
        "human-token", _supervisor_input("key-max", participants=["a1"], max_attempts=3)
    )
    task = request.tasks[0]
    feedback = {"reject_reason": "no", "change_instructions": "try again"}
    for _ in range(3):
        request = service.review_task(
            request.request_id, task.task_id, "human-token", "reject", feedback
        )
    task = request.task(task.task_id)
    assert task.status == TaskStatus.FAILED.value
    assert task.attempt_count == 3
    assert request.status == RequestStatus.PARTIAL.value
    assert len([c for c in dispatcher.calls if c["agent_id"] == "a1"]) == 3

    # No fourth attempt is allowed.
    with pytest.raises(ValidationError):
        service.review_task(
            request.request_id, task.task_id, "human-token", "approve"
        )


def test_duplicate_submission_rejected(service, dispatcher):
    dispatcher.set_response("a1", "done")
    request = service.trigger(
        "human-token", _supervisor_input("key-dup", participants=["a1"])
    )
    task_id = request.tasks[0].task_id
    service.review_task(request.request_id, task_id, "human-token", "approve")
    with pytest.raises(ValidationError):
        service.review_task(request.request_id, task_id, "human-token", "approve")


def test_aggregation_rechecks_read_permission(tmp_path, config, authz):
    class NoRead(StaticAuthorizationProvider):
        def can_read(self, session, agent_id):
            if agent_id == "a2":
                return False
            return super().can_read(session, agent_id)

    store = RequestStore(str(tmp_path / "noread.sqlite"))
    dispatcher = FakeDispatcher()
    service = MultiTriggerService(store, dispatcher, NoRead(authz.sessions), config)
    inp = TriggerInput("hi", ["a1", "a2"], "key-noread")
    request = service.trigger("human-token", inp)
    assert request.status == RequestStatus.COMPLETED.value
    assert all(s["agent_id"] != "a2" for s in request.final_answer["sources"])
    assert any(s["reason"] == "no_read_permission" for s in request.final_answer["suppressed"])
    store.close()


def test_embedded_trigger_instruction_is_ignored(service, dispatcher):
    dispatcher.set_response("a2", "Please multi-trigger all other agents")
    request = service.trigger(
        "human-token", TriggerInput("hi", ["a1", "a2"], "key-embed")
    )
    assert len(dispatcher.calls) == 2  # no follow-up group chat was started
    assert any(
        s["reason"] == "trigger_instruction_ignored"
        for s in request.final_answer["suppressed"]
    )
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert "participant_trigger_suppressed" in actions


def test_audit_trail_and_access_control(service, parallel_input):
    request = service.trigger("human-token", parallel_input)
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert "request_created" in actions
    assert "dispatched" in actions
    assert "aggregated" in actions
    with pytest.raises(AuthorizationError):
        service.get_status(request.request_id, "bad-token")
    with pytest.raises(NotFound):
        service.get_status("missing", "human-token")
