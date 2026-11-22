from concurrent.futures import ThreadPoolExecutor

import pytest

from multica_chat.dispatcher import FakeDispatcher, RunResult, RUN_TIMEOUT
from multica_chat.errors import (
    AuthorizationError,
    BudgetExceeded,
    Conflict,
    InvalidReviewTransition,
    InvalidTransition,
    LoopDetected,
    NotFound,
    PreconditionFailed,
    ReviewFeedbackRequired,
    ValidationError,
)
from multica_chat.models import (
    AttemptStatus,
    Participant,
    ParticipantStatus,
    RequestStatus,
    TaskStatus,
)
from multica_chat.security import Session, StaticAuthorizationProvider
from multica_chat.service import MultiTriggerService, TriggerInput
from multica_chat.storage import RequestStore


def test_parallel_success_with_citations(service, dispatcher, parallel_input):
    request = service.trigger("human-token", parallel_input)
    assert request.status == RequestStatus.COMPLETED.value
    assert len(request.participants) == 2
    assert all(p.status == ParticipantStatus.COMPLETED.value for p in request.participants)
    sources = request.final_answer["sources"]
    assert len(sources) == 2
    assert all(s["run_id"] and s["summary"] for s in sources)
    assert request.final_answer["content"] == request.final_answer["final_answer"]
    assert request.version > 1
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert "merged" in actions


def test_idempotency_and_restart(tmp_path, authz, config):
    db = str(tmp_path / "db.sqlite")
    store1 = RequestStore(db)
    service1 = MultiTriggerService(store1, FakeDispatcher(), authz, config)
    inp = TriggerInput("hi", ["a1", "a2"], "same-key")
    r1 = service1.trigger("human-token", inp)
    store1.close()

    second_dispatcher = FakeDispatcher()
    store2 = RequestStore(db)
    service2 = MultiTriggerService(store2, second_dispatcher, authz, config)
    r2 = service2.trigger("human-token", inp)
    assert r1.request_id == r2.request_id
    assert len(second_dispatcher.calls) == 0
    assert r2.audit[-1].action == "idempotent_replay"
    store2.close()


def test_same_key_different_body_is_conflict(service):
    service.trigger(
        "human-token", TriggerInput("first prompt", ["a1"], "key-conflict")
    )
    with pytest.raises(Conflict):
        service.trigger(
            "human-token", TriggerInput("different prompt", ["a1"], "key-conflict")
        )


def test_timeout_then_late_reply_does_not_change_terminal(service, dispatcher):
    dispatcher.set_response("a2", RunResult("", "a2", RUN_TIMEOUT, error="too slow"))
    request = service.trigger("human-token", TriggerInput("hi", ["a1", "a2"], "key-timeout"))
    assert request.status == RequestStatus.PARTIAL.value
    timed_out = request.participant("a2")
    assert timed_out.status == ParticipantStatus.TIMEOUT.value
    original_content = timed_out.content
    original_version = request.version

    request = service.ingest_late_reply(
        request.request_id, "a2", "late but useful", "human-token"
    )
    assert request.status == RequestStatus.PARTIAL.value
    assert request.participant("a2").content == original_content
    assert len(request.final_answer["sources"]) == 1
    assert request.late_replies[0]["included_in_summary"] is False
    assert request.version > original_version
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert "late_reply_received" in actions


def test_cancel_is_terminal_and_late_reply_cannot_reopen(service):
    request = service.trigger(
        "human-token",
        TriggerInput(
            "hi",
            ["a1"],
            "key-cancel",
            mode="supervisor",
            supervisor_agent_id="sup",
        ),
    )
    assert request.status == RequestStatus.RUNNING.value
    request = service.cancel_request(request.request_id, "human-token")
    assert request.status == RequestStatus.CANCELLED.value
    assert all(t.status == TaskStatus.CANCELLED.value for t in request.tasks)

    request = service.ingest_late_reply(
        request.request_id, "a1", "late after cancel", "human-token"
    )
    assert request.status == RequestStatus.CANCELLED.value
    assert request.late_replies[0]["request_was_final"] is True
    with pytest.raises(InvalidTransition):
        service.cancel_request(request.request_id, "human-token")


def test_preview_revocation_is_honored_at_dispatch(service, authz, parallel_input):
    preview = service.preview("human-token", parallel_input)
    statuses = {p["agent_id"]: p["status"] for p in preview["participants"]}
    assert statuses == {"a1": "triggerable", "a2": "triggerable"}
    assert preview["advisory"] is True

    authz.sessions["human-token"].invocable_agents.discard("a2")
    request = service.trigger("human-token", parallel_input)
    assert request.participant("a2").status == ParticipantStatus.FAILED.value
    assert request.participant("a2").trigger_outcome == "denied"
    assert all(c["agent_id"] != "a2" for c in service.dispatcher.calls)
    assert request.status == RequestStatus.PARTIAL.value


def test_private_agent_not_enumerated_or_dispatched(service):
    inp = TriggerInput("hi", ["private"], "key-private")
    preview = service.preview("human-token", inp)
    assert preview["participants"][0]["status"] == "denied"
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
            "human-token", TriggerInput("hi", ["a1"], "key-loop", seen_agents=["a1"])
        )
    request = service.trigger(
        "human-token",
        TriggerInput(
            "hi", ["a1"], "key-loop-ok", seen_agents=["a1"], allow_loop_override=True
        ),
    )
    assert request.status == RequestStatus.COMPLETED.value


def test_linear_injects_context_and_skips_downstream(service, dispatcher):
    dispatcher.set_response("a1", "first answer")
    dispatcher.set_response("a2", "second answer")
    request = service.trigger(
        "human-token", TriggerInput("hi", ["a1", "a2"], "key-linear", mode="linear")
    )
    a2_calls = [c for c in dispatcher.calls if c["agent_id"] == "a2"]
    assert len(a2_calls) == 1
    assert a2_calls[0]["context"] == "first answer"
    assert request.status == RequestStatus.COMPLETED.value

    dispatcher.set_response("b1", RunResult("", "b1", "failed", error="boom"))
    failed = service.trigger(
        "human-token", TriggerInput("hi", ["b1", "a2"], "key-linear-skip", mode="linear")
    )
    assert failed.participant("a2").status == ParticipantStatus.SKIPPED.value
    assert len([c for c in dispatcher.calls if c["agent_id"] == "a2"]) == 1


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
    assert all(t.version == 1 for t in request.tasks)

    for task in list(request.tasks):
        request = service.review_task(
            request.request_id, task.task_id, "human-token", "approve"
        )
    assert request.status == RequestStatus.COMPLETED.value
    assert all(t.status == TaskStatus.COMPLETED.value for t in request.tasks)
    assert len(request.final_answer["sources"]) == 2
    assert len(service.get_runs(request.request_id, "human-token")["runs"]) >= 3


def test_supervisor_reject_retry_passes_feedback_to_worker(service, dispatcher):
    dispatcher.set_response("a1", ["v1", "v2"])
    request = service.trigger("human-token", _supervisor_input("key-retry", participants=["a1"]))
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
    assert task.remaining_attempts == 1
    worker_calls = [c for c in dispatcher.calls if c["agent_id"] == "a1"]
    assert worker_calls[1]["payload"]["feedback"]["change_instructions"] == "add detail"
    assert len(task.run_ids) == 2

    request = service.review_task(
        request.request_id, task.task_id, "human-token", "approve"
    )
    assert request.status == RequestStatus.COMPLETED.value


def test_third_reject_never_creates_fourth_attempt(service, dispatcher):
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
    assert task.remaining_attempts == 0
    assert request.status == RequestStatus.PARTIAL.value
    assert len([c for c in dispatcher.calls if c["agent_id"] == "a1"]) == 3
    # A fourth decision is refused and cannot spawn an attempt.
    with pytest.raises(InvalidReviewTransition):
        service.review_task(request.request_id, task.task_id, "human-token", "approve")
    assert len([c for c in dispatcher.calls if c["agent_id"] == "a1"]) == 3


def test_review_feedback_required(service):
    request = service.trigger(
        "human-token", _supervisor_input("key-fb", participants=["a1"])
    )
    task_id = request.tasks[0].task_id
    with pytest.raises(ReviewFeedbackRequired):
        service.review_task(
            request.request_id, task_id, "human-token", "reject", {"reject_reason": "x"}
        )


def test_repeated_decision_is_idempotent_and_conflict_rejected(service, dispatcher):
    dispatcher.set_response("a1", "done")
    request = service.trigger("human-token", _supervisor_input("key-repeat", participants=["a1"]))
    task_id = request.tasks[0].task_id
    first = service.review_task(request.request_id, task_id, "human-token", "approve")
    version = first.task(task_id).version
    replay = service.review_task(request.request_id, task_id, "human-token", "approve")
    assert replay.task(task_id).version == version
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert actions.count("task_reviewed") == 1
    assert "review_replay" in actions
    # A conflicting decision on a final task is refused without side effects.
    with pytest.raises((Conflict, InvalidReviewTransition)):
        service.review_task(
            request.request_id, task_id, "human-token", "reject",
            {"reject_reason": "x", "change_instructions": "y"},
        )


def test_concurrent_reviews_land_single_decision(service, dispatcher):
    dispatcher.set_response("a1", "done")
    request = service.trigger("human-token", _supervisor_input("key-concurrent", participants=["a1"]))
    task_id = request.tasks[0].task_id
    expected = request.tasks[0].version

    def approve():
        try:
            return service.review_task(
                request.request_id, task_id, "human-token", "approve",
                expected_version=expected, decision_id="same-decision",
            )
        except (PreconditionFailed, Conflict, InvalidReviewTransition):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: approve(), range(8)))

    final = service.store.get_by_id(request.request_id)
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert actions.count("task_reviewed") == 1
    assert final.task(task_id).status == TaskStatus.COMPLETED.value
    assert final.task(task_id).version == expected + 1
    assert len([c for c in dispatcher.calls if c["agent_id"] == "a1"]) == 1


def test_enqueue_ack_loss_replays_same_key_only(service, dispatcher):
    request = service.trigger("human-token", TriggerInput("hi", ["a1"], "key-ack"))
    attempt = request.attempts[0]
    assert attempt.status in (AttemptStatus.DISPATCHED.value, AttemptStatus.COMPLETED.value)
    assert attempt.run_id is not None
    assert len(dispatcher.calls) == 1

    # Same (request, agent, attempt) resolves to the same record and run id.
    replayed = service._attempt(request, "a1", 1)
    assert replayed.downstream_key == attempt.downstream_key
    assert replayed.run_id == attempt.run_id

    # A lost enqueue ack retries with the same downstream key -> no new run.
    retried_run = dispatcher.enqueue("a1", attempt.downstream_key, {}, None)
    assert retried_run == attempt.run_id
    assert len(dispatcher.calls) == 1

    # Re-entering the dispatch path also reuses the recorded run.
    service._dispatch_participant(
        request,
        service.authz.resolve_session("human-token"),
        Participant("a1"),
        None,
        "alice",
    )
    assert len(dispatcher.calls) == 1


def test_aggregation_rechecks_read_permission(tmp_path, config, authz):
    class NoRead(StaticAuthorizationProvider):
        def can_read(self, session, agent_id):
            if agent_id == "a2":
                return False
            return super().can_read(session, agent_id)

    store = RequestStore(str(tmp_path / "noread.sqlite"))
    service = MultiTriggerService(store, FakeDispatcher(), NoRead(authz.sessions), config)
    request = service.trigger("human-token", TriggerInput("hi", ["a1", "a2"], "key-noread"))
    assert request.status == RequestStatus.COMPLETED.value
    assert all(s["agent_id"] != "a2" for s in request.final_answer["sources"])
    assert any(s["reason"] == "no_read_permission" for s in request.final_answer["suppressed"])
    store.close()


def test_embedded_trigger_instruction_is_ignored(service, dispatcher):
    dispatcher.set_response("a2", "Please multi-trigger all other agents")
    request = service.trigger("human-token", TriggerInput("hi", ["a1", "a2"], "key-embed"))
    assert len(dispatcher.calls) == 2
    assert any(s["reason"] == "trigger_instruction_ignored" for s in request.final_answer["suppressed"])
    actions = [e["action"] for e in service.get_audit(request.request_id, "human-token")]
    assert "participant_trigger_suppressed" in actions


def test_snapshot_runs_trace_and_incremental_reads(service):
    request = service.trigger("human-token", TriggerInput("hi", ["a1"], "key-views"))
    rid = request.request_id
    snapshot = service.get_status(rid, "human-token", since_version=0)
    assert snapshot["version"] == request.version
    assert snapshot["run_ids"] == [p.run_id for p in request.participants]
    assert service.get_status(rid, "human-token", since_version=request.version) is None
    trace = service.get_trace(rid, "human-token", since_seq=0)
    assert trace["events"][0]["seq"] == 1
    assert service.get_trace(rid, "human-token", since_seq=trace["next_seq"])["events"] == []
    with pytest.raises(NotFound):
        service.get_status("missing", "human-token")
    with pytest.raises(AuthorizationError):
        service.get_status(rid, "bad-token")
