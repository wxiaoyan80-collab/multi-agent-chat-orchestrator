"""Multi-trigger platform service (MARS-20), aligned with the MARS-22 API spec
and the MARS-20/22 offline contract draft.

Owns request/task state machines, three execution strategies, attempt records,
dispatch idempotency, permission re-checks, budgets, audit and aggregation.
"""

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .config import ServiceConfig
from .dispatcher import RUN_COMPLETED, RUN_FAILED, RUN_TIMEOUT, Dispatcher, RunResult
from .errors import (
    AuthorizationError,
    Conflict,
    InvalidReviewTransition,
    InvalidTransition,
    NotFound,
    PreconditionFailed,
    ReviewFeedbackRequired,
    TaskNotFound,
    ValidationError,
)
from .models import (
    Attempt,
    AttemptStatus,
    AuditEvent,
    Feedback,
    MergeStrategy,
    Mode,
    MultiTriggerRequest,
    Participant,
    ParticipantStatus,
    RequestStatus,
    Task,
    TaskStatus,
    utcnow,
)
from .security import (
    AuthorizationProvider,
    contains_trigger_instruction,
    enforce_invocation_limits,
    enforce_participant_budget,
    sanitize_for_agent,
    summarize,
)
from .state_machine import assert_request_transition, assert_task_transition, is_terminal_request

_DISAGREEMENT = re.compile(r"不同意|disagree|however|但是|相反|conflict|contradict", re.IGNORECASE)


@dataclass
class TriggerInput:
    user_prompt: str
    participant_agent_ids: List[str]
    idempotency_key: str
    mode: str = Mode.PARALLEL.value
    supervisor_agent_id: Optional[str] = None
    timeout_seconds: int = 120
    merge_strategy: str = MergeStrategy.SUMMARY.value
    max_attempts: int = 3
    upstream_depth: int = 0
    seen_agents: List[str] = field(default_factory=list)
    allow_loop_override: bool = False


class MultiTriggerService:
    def __init__(
        self,
        store: Any,
        dispatcher: Dispatcher,
        authz: AuthorizationProvider,
        config: Optional[ServiceConfig] = None,
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.authz = authz
        self.config = config or ServiceConfig()
        self._lock = threading.RLock()

    # ---- helpers -------------------------------------------------------------
    @staticmethod
    def _canonical_ids(ids: List[str]) -> List[str]:
        seen, result = set(), []
        for agent_id in ids:
            if agent_id not in seen:
                seen.add(agent_id)
                result.append(agent_id)
        return result

    @classmethod
    def _body_hash(cls, inp: TriggerInput) -> str:
        normalized = {
            "user_prompt": inp.user_prompt,
            "participant_agent_ids": cls._canonical_ids(inp.participant_agent_ids),
            "mode": inp.mode,
            "supervisor_agent_id": inp.supervisor_agent_id,
            "timeout_seconds": inp.timeout_seconds,
            "merge_strategy": inp.merge_strategy,
            "max_attempts": inp.max_attempts,
        }
        raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _audit(
        self, request: MultiTriggerRequest, action: str, actor: str, **detail: Any
    ) -> None:
        request.audit.append(
            AuditEvent(len(request.audit) + 1, utcnow(), actor, action, detail)
        )

    def _transition(
        self, request: MultiTriggerRequest, new_status: str, actor: str = "system", **detail: Any
    ) -> None:
        assert_request_transition(request.status, new_status)
        request.status = new_status
        request.version += 1
        request.updated_at = utcnow()
        self._audit(request, "state_transition", actor, to=new_status, **detail)

    def _attempt(
        self,
        request: MultiTriggerRequest,
        agent_id: str,
        attempt_no: int,
        task_id: Optional[str] = None,
    ) -> Attempt:
        """Get-or-create an attempt. Same key => same record, even on replay."""
        suffix = task_id or agent_id
        key = f"mt:{request.request_id}:{suffix}:{attempt_no}"
        candidate = Attempt(
            downstream_key=key,
            request_id=request.request_id,
            agent_id=agent_id,
            attempt_no=attempt_no,
            task_id=task_id,
        )
        stored = self.store.get_or_create_attempt(candidate)
        if request.attempt(stored.downstream_key) is None:
            request.attempts.append(stored)
        return stored

    def _enqueue_attempt(
        self,
        attempt: Attempt,
        payload: Dict[str, Any],
        context: Optional[str] = None,
    ) -> str:
        # Replay with a lost ack reuses the same downstream key; the dispatcher
        # deduplicates, so no second run is created.
        if attempt.run_id is None:
            attempt.run_id = self.dispatcher.enqueue(
                attempt.agent_id, attempt.downstream_key, payload, context
            )
            attempt.status = AttemptStatus.DISPATCHED.value
            attempt.updated_at = utcnow()
            self.store.update_attempt(attempt)
        return attempt.run_id

    def _authorized(self, session: Any, agent_ids: List[str]) -> List[str]:
        return [a for a in agent_ids if self.authz.can_invoke(session, a)]

    # ---- preview ------------------------------------------------------------
    def preview(self, token: str, inp: TriggerInput) -> Dict[str, Any]:
        with self._lock:
            session = self.authz.resolve_session(token)
            if not session.is_human:
                raise AuthorizationError("only a human session can originate a group chat")
            ids = self._canonical_ids(inp.participant_agent_ids)
            enforce_participant_budget(ids, self.config)
            planned, denied = [], []
            for agent_id in ids:
                if self.authz.can_invoke(session, agent_id):
                    planned.append({"agent_id": agent_id, "status": "triggerable"})
                else:
                    # Do not reveal existence / online status of invisible agents.
                    denied.append(
                        {"agent_id": agent_id, "status": "denied", "reason": "unavailable"}
                    )
            return {"participants": planned + denied, "advisory": True}

    # ---- trigger ------------------------------------------------------------
    def trigger(self, token: str, inp: TriggerInput) -> MultiTriggerRequest:
        with self._lock:
            session = self.authz.resolve_session(token)
            if not session.is_human:
                raise AuthorizationError("only a human session can originate a group chat")

            body_hash = self._body_hash(inp)
            known = self.store.get_idempotency(
                session.workspace_id, session.initiator_id, inp.idempotency_key
            )
            if known:
                request_id, known_hash = known
                if known_hash != body_hash:
                    raise Conflict(
                        "idempotency key reused with a different body",
                        idempotency_key=inp.idempotency_key,
                    )
                request = self.store.get_by_id(request_id)
                self._audit(request, "idempotent_replay", session.initiator_id)
                self.store.save(request)
                return request

            ids = self._canonical_ids(inp.participant_agent_ids)
            enforce_participant_budget(ids, self.config)
            override = bool(inp.allow_loop_override and session.is_human)
            enforce_invocation_limits(
                ids,
                inp.supervisor_agent_id,
                inp.upstream_depth,
                inp.seen_agents,
                override,
                self.config,
            )
            if inp.mode == Mode.SUPERVISOR.value and not inp.supervisor_agent_id:
                raise ValidationError("supervisor_agent_id is required in supervisor mode")
            if inp.mode == Mode.SUPERVISOR.value and inp.supervisor_agent_id in ids:
                raise ValidationError("supervisor_agent_id must not be a participant")

            request = MultiTriggerRequest(
                request_id=f"req-{uuid4().hex[:16]}",
                idempotency_key=inp.idempotency_key,
                initiator_id=session.initiator_id,
                workspace_id=session.workspace_id,
                session_id=session.session_id,
                user_prompt=inp.user_prompt,
                mode=inp.mode,
                participant_ids=ids,
                supervisor_agent_id=inp.supervisor_agent_id,
                timeout_seconds=inp.timeout_seconds or self.config.default_timeout_seconds,
                merge_strategy=inp.merge_strategy,
                max_attempts=inp.max_attempts or self.config.default_max_attempts,
                upstream_depth=inp.upstream_depth,
                seen_agents=list(inp.seen_agents),
                allow_loop_override=override,
                body_hash=body_hash,
            )
            self._audit(request, "request_created", session.initiator_id, participants=ids)
            self._transition(request, RequestStatus.QUEUED.value, session.initiator_id)

            existing = self.store.create_request_atomic(request, body_hash)
            if existing is not None:
                if existing.body_hash != body_hash:
                    raise Conflict("idempotency key reused with a different body")
                self._audit(existing, "idempotent_replay", session.initiator_id)
                self.store.save(existing)
                return existing

            if request.mode == Mode.PARALLEL.value:
                self._run_parallel(request, session, ids)
            elif request.mode == Mode.LINEAR.value:
                self._run_linear(request, session, ids)
            elif request.mode == Mode.SUPERVISOR.value:
                self._run_supervisor(request, session, ids)
            else:
                raise ValidationError(f"unknown mode: {request.mode}")
            self.store.save(request)
            return request

    # ---- execution strategies ----------------------------------------------
    def _dispatch_participant(
        self,
        request: MultiTriggerRequest,
        session: Any,
        participant: Participant,
        context: Optional[str],
        actor: str,
    ) -> bool:
        if not self.authz.can_invoke(session, participant.agent_id):
            participant.status = ParticipantStatus.FAILED.value
            participant.trigger_outcome = "denied"
            participant.error = "not_authorized"
            self._audit(
                request,
                "dispatch_denied",
                actor,
                agent_id=participant.agent_id,
                reason="invoke gate denied at dispatch",
            )
            return False

        attempt = self._attempt(request, participant.agent_id, 1)
        payload = sanitize_for_agent(
            {"user_prompt": request.user_prompt, "context": context or ""}
        )
        run_id = self._enqueue_attempt(attempt, payload, context)
        participant.run_id = run_id
        participant.downstream_key = attempt.downstream_key
        participant.status = ParticipantStatus.RUNNING.value
        participant.injected_context = context
        request.invocation_graph.append(
            {"from": session.initiator_id, "to": participant.agent_id, "run_id": run_id}
        )
        self._audit(
            request,
            "dispatched",
            actor,
            agent_id=participant.agent_id,
            run_id=run_id,
            downstream_key=attempt.downstream_key,
            attempt_no=attempt.attempt_no,
        )
        self.store.save(request)
        return True

    def _collect(self, request: MultiTriggerRequest, participant: Participant, actor: str) -> None:
        result: RunResult = self.dispatcher.await_result(
            participant.run_id, request.timeout_seconds
        )
        participant.latency_ms = result.latency_ms
        attempt = request.attempt(participant.downstream_key)
        if result.status == RUN_COMPLETED:
            participant.status = ParticipantStatus.COMPLETED.value
            participant.content = result.content
            if attempt:
                attempt.status = AttemptStatus.COMPLETED.value
        elif result.status == RUN_TIMEOUT:
            participant.status = ParticipantStatus.TIMEOUT.value
            participant.error = result.error or "timeout"
            if attempt:
                attempt.status = AttemptStatus.TIMEOUT.value
        else:
            participant.status = ParticipantStatus.FAILED.value
            participant.error = result.error or "run failed"
            if attempt:
                attempt.status = AttemptStatus.FAILED.value
        if attempt:
            attempt.updated_at = utcnow()
            self.store.update_attempt(attempt)
        self._audit(
            request,
            "result_collected",
            actor,
            agent_id=participant.agent_id,
            run_id=participant.run_id,
            result_status=result.status,
        )
        self.store.save(request)

    def _degradation_hint(self, request: MultiTriggerRequest, planned: int) -> None:
        failed = sum(
            1
            for p in request.participants
            if p.status
            in (ParticipantStatus.FAILED.value, ParticipantStatus.TIMEOUT.value)
        )
        if planned and failed * 2 > planned:  # majority failed; denominator = planned units
            request.degradation = {
                "reason": "majority_failed",
                "failed_units": failed,
                "planned_units": planned,
                "suggestion": "human_takeover_or_single_expert",
            }
            self._audit(request, "degradation_suggested", "system", failed=failed, planned=planned)

    def _finish_from_participants(
        self, request: MultiTriggerRequest, session: Any, actor: str
    ) -> None:
        total = len(request.participants)
        completed = sum(
            1 for p in request.participants if p.status == ParticipantStatus.COMPLETED.value
        )
        self._transition(request, RequestStatus.RUNNING.value, actor)
        if total == 0 or completed == 0:
            self._transition(request, RequestStatus.FAILED.value, actor)
        elif completed == total:
            self._transition(request, RequestStatus.COMPLETED.value, actor)
        else:
            self._transition(request, RequestStatus.PARTIAL.value, actor)
        # Denominator is planned execution units, not attempts.
        self._degradation_hint(request, len(request.participant_ids))
        request.final_answer = self._aggregate(request, session)
        self._audit(request, "merged", actor, strategy=request.merge_strategy)

    def _run_parallel(
        self, request: MultiTriggerRequest, session: Any, ids: List[str]
    ) -> None:
        actor = session.initiator_id
        for agent_id in ids:
            participant = request.participant(agent_id)
            if participant is None:
                participant = Participant(agent_id=agent_id)
                request.participants.append(participant)
            self._dispatch_participant(request, session, participant, None, actor)
        for participant in request.participants:
            if participant.status == ParticipantStatus.RUNNING.value:
                self._collect(request, participant, actor)
        self._finish_from_participants(request, session, actor)

    def _run_linear(
        self, request: MultiTriggerRequest, session: Any, ids: List[str]
    ) -> None:
        actor = session.initiator_id
        context: Optional[str] = None
        for agent_id in ids:
            participant = Participant(agent_id=agent_id)
            request.participants.append(participant)
            if context is None and request.participants.index(participant) > 0:
                # Strong dependency: predecessor failed -> mark skipped, no dispatch.
                participant.status = ParticipantStatus.SKIPPED.value
                self._audit(request, "participant_skipped", actor, agent_id=agent_id)
                continue
            if not self._dispatch_participant(request, session, participant, context, actor):
                context = None
                continue
            self._collect(request, participant, actor)
            if participant.status == ParticipantStatus.COMPLETED.value and participant.content:
                context = participant.content
            else:
                context = None
        self._finish_from_participants(request, session, actor)

    def _run_supervisor(
        self, request: MultiTriggerRequest, session: Any, ids: List[str]
    ) -> None:
        actor = session.initiator_id
        supervisor = request.supervisor_agent_id
        if not supervisor or not self.authz.can_invoke(session, supervisor):
            self._transition(request, RequestStatus.RUNNING.value, actor)
            self._transition(
                request,
                RequestStatus.FAILED.value,
                actor,
                reason="supervisor_unauthorized",
            )
            return

        sup_attempt = self._attempt(request, supervisor, 1)
        sup_payload = sanitize_for_agent(
            {"user_prompt": request.user_prompt, "context": "supervisor planning"}
        )
        sup_run = self._enqueue_attempt(sup_attempt, sup_payload)
        request.invocation_graph.append(
            {"from": actor, "to": supervisor, "run_id": sup_run, "role": "supervisor"}
        )
        self._audit(request, "supervisor_triggered", actor, run_id=sup_run)

        for agent_id in ids:
            if not self.authz.can_invoke(session, agent_id):
                participant = Participant(agent_id=agent_id)
                request.participants.append(participant)
                self._dispatch_participant(request, session, participant, None, actor)
                continue
            task = Task(
                task_id=f"task-{uuid4().hex[:12]}",
                parent_request_id=request.request_id,
                supervisor_agent_id=supervisor,
                worker_agent_id=agent_id,
                payload={"user_prompt": request.user_prompt},
                max_attempts=request.max_attempts,
            )
            request.tasks.append(task)
            self._assign_task(request, task, actor)
            self._start_and_submit_task(request, task, actor)

        self._transition(request, RequestStatus.RUNNING.value, actor)
        if not request.tasks:
            self._transition(request, RequestStatus.FAILED.value, actor, reason="no_tasks")
            return
        self.store.save(request)

    # ---- supervisor task transitions ----------------------------------------
    def _assign_task(self, request: MultiTriggerRequest, task: Task, actor: str) -> None:
        assert_task_transition(task.status, TaskStatus.ASSIGNED.value)
        task.status = TaskStatus.ASSIGNED.value
        task.attempt_count += 1  # 1-based; increment happens only when a new attempt is created
        task.updated_at = utcnow()
        self._audit(
            request,
            "task_assigned",
            actor,
            task_id=task.task_id,
            worker_agent_id=task.worker_agent_id,
            attempt_count=task.attempt_count,
        )

    def _start_and_submit_task(
        self,
        request: MultiTriggerRequest,
        task: Task,
        actor: str,
        feedback: Optional[Feedback] = None,
    ) -> None:
        assert_task_transition(task.status, TaskStatus.IN_PROGRESS.value)
        task.status = TaskStatus.IN_PROGRESS.value
        task.updated_at = utcnow()
        self._audit(request, "task_started", actor, task_id=task.task_id)

        payload = sanitize_for_agent(
            {
                "user_prompt": request.user_prompt,
                "task": task.payload,
                "feedback": {
                    "reject_reason": feedback.reject_reason if feedback else "",
                    "change_instructions": (
                        feedback.change_instructions if feedback else ""
                    ),
                },
            }
        )
        # The attempt key is request/task/attempt_no -> one execution per attempt.
        attempt = self._attempt(request, task.worker_agent_id, task.attempt_count, task.task_id)
        run_id = self._enqueue_attempt(attempt, payload)
        task.run_id = run_id
        if run_id not in task.run_ids:
            task.run_ids.append(run_id)
        request.invocation_graph.append(
            {
                "from": task.supervisor_agent_id,
                "to": task.worker_agent_id,
                "run_id": run_id,
                "task_id": task.task_id,
                "attempt": task.attempt_count,
            }
        )
        self._audit(
            request,
            "task_dispatched",
            actor,
            task_id=task.task_id,
            run_id=run_id,
            downstream_key=attempt.downstream_key,
            attempt_count=task.attempt_count,
        )
        result = self.dispatcher.await_result(run_id, request.timeout_seconds)
        if result.status != RUN_COMPLETED:
            assert_task_transition(task.status, TaskStatus.FAILED.value)
            task.status = TaskStatus.FAILED.value
            task.result = None
            attempt.status = (
                AttemptStatus.TIMEOUT.value
                if result.status == RUN_TIMEOUT
                else AttemptStatus.FAILED.value
            )
            self.store.update_attempt(attempt)
            self._audit(
                request,
                "task_failed",
                actor,
                task_id=task.task_id,
                result_status=result.status,
            )
            task.updated_at = utcnow()
            return

        assert_task_transition(task.status, TaskStatus.SUBMITTED.value)
        task.status = TaskStatus.SUBMITTED.value
        task.result = result.content
        task.updated_at = utcnow()
        attempt.status = AttemptStatus.COMPLETED.value
        self.store.update_attempt(attempt)
        self._audit(request, "task_submitted", actor, task_id=task.task_id)

    def review_task(
        self,
        request_id: str,
        task_id: str,
        token: str,
        action: str,
        feedback: Optional[Dict[str, Any]] = None,
        expected_version: Optional[int] = None,
        decision_id: Optional[str] = None,
    ) -> MultiTriggerRequest:
        session = self.authz.resolve_session(token)

        def mutate(request: MultiTriggerRequest) -> None:
            task = request.task(task_id)
            if task is None:
                raise TaskNotFound(f"task {task_id} not found")
            if session.initiator_id not in (
                request.initiator_id,
                request.supervisor_agent_id,
            ):
                raise AuthorizationError("caller may not review this task")

            # Idempotent replay applies only to a decision on the SAME attempt.
            # A new attempt (after a reject) accepts a fresh decision even when
            # the feedback text is identical.
            if task.review_decision and task.review_decision.get("attempt_no") == task.attempt_count:
                decision = task.review_decision
                same = (
                    decision.get("action") == action
                    and decision.get("feedback") == (feedback or {})
                )
                if expected_version is not None and expected_version != decision.get(
                    "expected_version"
                ):
                    raise PreconditionFailed(
                        "task version changed",
                        expected=expected_version,
                        actual=task.version,
                    )
                if same:
                    self._audit(
                        request, "review_replay", session.initiator_id, task_id=task_id
                    )
                    return
                if task.status != TaskStatus.SUBMITTED.value:
                    raise InvalidReviewTransition(
                        f"task is {task.status}; no further decision is accepted"
                    )
                raise Conflict("a different decision was already recorded", task_id=task_id)

            if expected_version is not None and task.version != expected_version:
                raise PreconditionFailed(
                    "task version changed", expected=expected_version, actual=task.version
                )
            if task.status != TaskStatus.SUBMITTED.value:
                raise InvalidReviewTransition(
                    f"task is {task.status}; only a submitted task can be reviewed"
                )
            decision_attempt = task.attempt_count
            if action not in ("approve", "reject"):
                raise ValidationError("action must be approve or reject")
            parsed: Optional[Feedback] = None
            if action == "reject":
                parsed = Feedback.from_dict(feedback)
                if not parsed.reject_reason or not parsed.change_instructions:
                    raise ReviewFeedbackRequired(
                        "reject requires reject_reason and change_instructions"
                    )

            assert_task_transition(task.status, TaskStatus.REVIEWED.value)
            task.status = TaskStatus.REVIEWED.value
            task.updated_at = utcnow()
            self._audit(
                request,
                "task_reviewed",
                session.initiator_id,
                task_id=task_id,
                review_action=action,
            )

            new_run_id: Optional[str] = None
            if action == "approve":
                assert_task_transition(task.status, TaskStatus.APPROVED.value)
                task.status = TaskStatus.APPROVED.value
                assert_task_transition(task.status, TaskStatus.COMPLETED.value)
                task.status = TaskStatus.COMPLETED.value
                task.feedback = None
                self._audit(request, "task_approved", session.initiator_id, task_id=task_id)
            else:
                assert_task_transition(task.status, TaskStatus.REJECTED.value)
                task.status = TaskStatus.REJECTED.value
                task.feedback = parsed
                self._audit(
                    request,
                    "task_rejected",
                    session.initiator_id,
                    task_id=task_id,
                    feedback=parsed.__dict__,
                )
                # attempt_count reaches max_attempts on the third rejection -> fail.
                if task.attempt_count >= task.max_attempts:
                    assert_task_transition(task.status, TaskStatus.FAILED.value)
                    task.status = TaskStatus.FAILED.value
                    self._audit(
                        request,
                        "task_failed_max_attempts",
                        session.initiator_id,
                        task_id=task_id,
                        attempt_count=task.attempt_count,
                    )
                else:
                    self._assign_task(request, task, session.initiator_id)
                    self._start_and_submit_task(request, task, session.initiator_id, parsed)
                    new_run_id = task.run_ids[-1] if task.run_ids else None
                    self._audit(request, "task_redispatched", session.initiator_id, task_id=task_id)
                    self._audit(request, "task_retry", session.initiator_id, task_id=task_id)

            task.version += 1
            task.review_decision = {
                "action": action,
                "feedback": feedback or {},
                "attempt_no": decision_attempt,
                "expected_version": expected_version,
                "version": task.version,
                "decision_id": decision_id or f"dec-{task_id}-{task.version}",
                "new_run_id": new_run_id,
            }
            task.updated_at = utcnow()
            self._maybe_finalize_supervisor(request, session)

        self.store.update_atomic(request_id, mutate)
        return self.store.get_by_id(request_id)

    def review_response(
        self, request_id: str, task_id: str, token: str
    ) -> Dict[str, Any]:
        self.authz.resolve_session(token)
        request = self.store.get_by_id(request_id)
        if request is None:
            raise NotFound(f"request {request_id} not found")
        task = request.task(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id} not found")
        decision = task.review_decision or {}
        return {
            "task_id": task.task_id,
            "status": task.status,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "remaining_attempts": task.remaining_attempts,
            "feedback": task.feedback.__dict__ if task.feedback else None,
            "new_run_id": decision.get("new_run_id"),
            "version": task.version,
            "request_status": request.status,
        }

    def _maybe_finalize_supervisor(
        self, request: MultiTriggerRequest, session: Any
    ) -> None:
        if request.status != RequestStatus.RUNNING.value or not request.tasks:
            return
        statuses = [t.status for t in request.tasks]
        if all(s == TaskStatus.COMPLETED.value for s in statuses):
            self._transition(request, RequestStatus.COMPLETED.value, session.initiator_id)
        elif all(
            s in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value) for s in statuses
        ):
            self._transition(request, RequestStatus.PARTIAL.value, session.initiator_id)
        else:
            return
        request.final_answer = self._aggregate(request, session)
        self._audit(request, "merged", session.initiator_id, strategy=request.merge_strategy)

    # ---- cancellation / late replies / reads ---------------------------------
    def cancel_request(self, request_id: str, token: str) -> MultiTriggerRequest:
        session = self.authz.resolve_session(token)

        def mutate(request: MultiTriggerRequest) -> None:
            if session.initiator_id != request.initiator_id:
                raise AuthorizationError("caller may not cancel this request")
            if is_terminal_request(request.status):
                raise InvalidTransition("request is already final")
            self._transition(request, RequestStatus.CANCELLED.value, session.initiator_id)
            for participant in request.participants:
                if not is_terminal_request(
                    participant.status
                ) and participant.status != ParticipantStatus.COMPLETED.value:
                    participant.status = ParticipantStatus.CANCELLED.value
            for task in request.tasks:
                if task.status not in (
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                ):
                    task.status = TaskStatus.CANCELLED.value
            # No further dispatch: mark pending attempts cancelled.
            for attempt in request.attempts:
                if attempt.status == AttemptStatus.PENDING_DISPATCH.value:
                    attempt.status = AttemptStatus.CANCELLED.value
            self._audit(request, "cancelled", session.initiator_id)

        self.store.update_atomic(request_id, mutate)
        return self.store.get_by_id(request_id)

    def ingest_late_reply(
        self, request_id: str, agent_id: str, content: str, token: str
    ) -> MultiTriggerRequest:
        session = self.authz.resolve_session(token)

        def mutate(request: MultiTriggerRequest) -> None:
            if session.initiator_id != request.initiator_id:
                raise AuthorizationError("caller may not ingest replies for this request")
            entry = {
                "seq": len(request.late_replies) + 1,
                "agent_id": agent_id,
                "content": content,
                "received_at": utcnow(),
                "request_was_final": is_terminal_request(request.status),
                "included_in_summary": False,
            }
            request.late_replies.append(entry)
            request.version += 1
            request.updated_at = utcnow()
            # Late replies are recorded for display/trace only. They NEVER change
            # participant content, request status, or a final aggregation.
            self._audit(
                request,
                "late_reply_received",
                session.initiator_id,
                agent_id=agent_id,
                request_was_final=entry["request_was_final"],
            )

        self.store.update_atomic(request_id, mutate)
        return self.store.get_by_id(request_id)

    def get_status(
        self, request_id: str, token: str, since_version: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        self.authz.resolve_session(token)
        request = self.store.get_by_id(request_id)
        if request is None:
            raise NotFound(f"request {request_id} not found")
        if since_version is not None and request.version <= since_version:
            return None  # -> HTTP 304
        return self._view(request)

    def _view(self, request: MultiTriggerRequest) -> Dict[str, Any]:
        data = request.to_dict()
        data["initiator_user_id"] = request.initiator_id
        data["run_ids"] = []
        for attempt in request.attempts:
            if attempt.run_id and attempt.run_id not in data["run_ids"]:
                data["run_ids"].append(attempt.run_id)
        for task in data["tasks"]:
            task["remaining_attempts"] = max(
                0, task["max_attempts"] - task["attempt_count"]
            )
        return data

    def get_runs(
        self, request_id: str, token: str, since_version: Optional[int] = None
    ) -> Dict[str, Any]:
        self.authz.resolve_session(token)
        request = self.store.get_by_id(request_id)
        if request is None:
            raise NotFound(f"request {request_id} not found")
        runs = []
        for attempt in request.attempts:
            if attempt.run_id:
                runs.append(
                    {
                        "run_id": attempt.run_id,
                        "agent_id": attempt.agent_id,
                        "kind": "worker" if attempt.task_id else "participant",
                        "task_id": attempt.task_id,
                        "attempt": attempt.attempt_no,
                        "status": attempt.status,
                        "downstream_key": attempt.downstream_key,
                    }
                )
        return {"request_id": request_id, "version": request.version, "runs": runs}

    def get_trace(
        self, request_id: str, token: str, since_seq: int = 0
    ) -> Dict[str, Any]:
        self.authz.resolve_session(token)
        if self.store.get_by_id(request_id) is None:
            raise NotFound(f"request {request_id} not found")
        events = [e for e in self.store.get_audit(request_id) if e["seq"] > since_seq]
        next_seq = events[-1]["seq"] if events else since_seq
        return {"request_id": request_id, "events": events, "next_seq": next_seq}

    def get_audit(self, request_id: str, token: str) -> List[Dict[str, Any]]:
        return self.get_trace(request_id, token, 0)["events"]

    # ---- aggregation (untrusted agent text) ----------------------------------
    def _aggregate(
        self, request: MultiTriggerRequest, session: Any
    ) -> Dict[str, Any]:
        sources: List[Dict[str, Any]] = []
        suppressed: List[Dict[str, Any]] = []

        if request.mode == Mode.SUPERVISOR.value:
            # Supervisor mode uses only approved results; failed tasks are never
            # held open waiting for approval.
            candidates = [
                (t.worker_agent_id, t.run_id, t.result, t.attempt_count)
                for t in request.tasks
                if t.status == TaskStatus.COMPLETED.value and t.result
            ]
        else:
            candidates = [
                (p.agent_id, p.run_id, p.content, None)
                for p in request.participants
                if p.status == ParticipantStatus.COMPLETED.value and p.content
            ]

        for agent_id, run_id, content, attempts in candidates:
            # Re-check read permission before aggregation (no aggregation bypass).
            if not self.authz.can_read(session, agent_id):
                suppressed.append({"agent_id": agent_id, "reason": "no_read_permission"})
                self._audit(
                    request,
                    "aggregation_source_suppressed",
                    session.initiator_id,
                    agent_id=agent_id,
                    reason="no_read_permission",
                )
                continue
            if contains_trigger_instruction(content):
                suppressed.append({"agent_id": agent_id, "reason": "trigger_instruction_ignored"})
                self._audit(
                    request,
                    "participant_trigger_suppressed",
                    session.initiator_id,
                    agent_id=agent_id,
                )
            source = {
                "agent_id": agent_id,
                "run_id": run_id,
                "summary": summarize(content),
                "quote": content,
            }
            if attempts is not None:
                source["attempt_count"] = attempts
            sources.append(source)

        if request.merge_strategy == MergeStrategy.CONCATENATE.value:
            text = "\n\n".join(f"[{s['agent_id']}] {s['quote']}" for s in sources)
        elif request.merge_strategy == MergeStrategy.CHOOSE_BEST.value:
            best = max(sources, key=lambda s: len(s["quote"] or "")) if sources else None
            text = f"[{best['agent_id']}] {best['quote']}" if best else ""
        else:
            text = "\n".join(f"[{s['agent_id']}] {s['summary']}" for s in sources)

        disagreements = [
            s["agent_id"] for s in sources if _DISAGREEMENT.search(s["quote"] or "")
        ]
        return {
            "strategy": request.merge_strategy,
            "final_answer": text,
            "content": text,
            "sources": sources,
            "suppressed": suppressed,
            "disagreement_preserved": disagreements,
            "note": "Agent replies are untrusted text; each source is cited by run_id.",
        }
