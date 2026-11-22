"""Multi-trigger platform service.

Owns the request/task state machines, the three execution strategies, dispatch
idempotency, permission re-checks, budgets, audit and aggregation.
"""

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .config import ServiceConfig
from .dispatcher import RUN_COMPLETED, RUN_FAILED, RUN_TIMEOUT, Dispatcher, RunResult
from .errors import AuthorizationError, NotFound, ValidationError
from .models import (
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
from .state_machine import assert_request_transition, assert_task_transition

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
    def _audit(
        self, request: MultiTriggerRequest, action: str, actor: str, **detail: Any
    ) -> None:
        request.audit.append(AuditEvent(utcnow(), actor, action, detail))

    def _transition(
        self, request: MultiTriggerRequest, new_status: str, actor: str = "system", **detail: Any
    ) -> None:
        assert_request_transition(request.status, new_status)
        request.status = new_status
        request.updated_at = utcnow()
        self._audit(request, "request_status", actor, to=new_status, **detail)

    def _downstream_key(self, request: MultiTriggerRequest, agent_id: str, attempt: int) -> str:
        # Deterministic: stable across restarts and client resends.
        return f"mt:{request.request_id}:{agent_id}:{attempt}"

    def _planned_participants(self, ids: List[str]) -> List[str]:
        seen, result = set(), []
        for agent_id in ids:
            if agent_id not in seen:
                seen.add(agent_id)
                result.append(agent_id)
        return result

    def _authorized(self, session: Any, agent_ids: List[str]) -> List[str]:
        # Re-check at actual dispatch; no reliance on a prior preview.
        return [a for a in agent_ids if self.authz.can_invoke(session, a)]

    # ---- preview (advisory only) --------------------------------------------
    def preview(self, token: str, inp: TriggerInput) -> Dict[str, Any]:
        with self._lock:
            session = self.authz.resolve_session(token)
            if not session.is_human:
                raise AuthorizationError("only a human session can originate a group chat")
            ids = self._planned_participants(inp.participant_agent_ids)
            enforce_participant_budget(ids, self.config)
            planned, denied = [], []
            for agent_id in ids:
                if self.authz.can_invoke(session, agent_id):
                    planned.append({"agent_id": agent_id, "invocable": True})
                else:
                    # Do not reveal existence or online status of invisible agents.
                    denied.append({"agent_id": agent_id, "invocable": False, "note": "unavailable"})
            return {"advisory": True, "planned": planned, "denied": denied}

    # ---- trigger ------------------------------------------------------------
    def trigger(self, token: str, inp: TriggerInput) -> MultiTriggerRequest:
        with self._lock:
            session = self.authz.resolve_session(token)
            if not session.is_human:
                raise AuthorizationError("only a human session can originate a group chat")

            existing = self.store.get_by_idempotency_key(inp.idempotency_key)
            if existing is not None:
                self._audit(existing, "idempotent_replay", session.initiator_id)
                self.store.save(existing)
                return existing

            ids = self._planned_participants(inp.participant_agent_ids)
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
            )
            self._audit(request, "request_created", session.initiator_id, participants=ids)
            self._transition(request, RequestStatus.QUEUED.value, session.initiator_id)
            self.store.save(request)

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
            participant.error = "not_authorized"
            self._audit(
                request,
                "dispatch_denied",
                actor,
                agent_id=participant.agent_id,
                reason="invoke gate denied at dispatch",
            )
            return False
        payload = sanitize_for_agent(
            {"user_prompt": request.user_prompt, "context": context or ""}
        )
        participant.downstream_key = self._downstream_key(
            request, participant.agent_id, 1
        )
        participant.run_id = self.dispatcher.enqueue(
            participant.agent_id,
            participant.downstream_key,
            payload,
            context,
        )
        participant.status = ParticipantStatus.RUNNING.value
        participant.injected_context = context
        request.invocation_graph.append(
            {"from": session.initiator_id, "to": participant.agent_id, "run_id": participant.run_id}
        )
        self._audit(
            request,
            "dispatched",
            actor,
            agent_id=participant.agent_id,
            run_id=participant.run_id,
        )
        self.store.save(request)
        return True

    def _collect(self, request: MultiTriggerRequest, participant: Participant, actor: str) -> None:
        result: RunResult = self.dispatcher.await_result(
            participant.run_id, request.timeout_seconds
        )
        participant.latency_ms = result.latency_ms
        if result.status == RUN_COMPLETED:
            participant.status = ParticipantStatus.COMPLETED.value
            participant.content = result.content
        elif result.status == RUN_TIMEOUT:
            participant.status = ParticipantStatus.TIMEOUT.value
            participant.error = result.error or "timeout"
        else:
            participant.status = ParticipantStatus.FAILED.value
            participant.error = result.error or "run failed"
        self._audit(
            request,
            "result_collected",
            actor,
            agent_id=participant.agent_id,
            run_id=participant.run_id,
            result_status=result.status,
        )
        self.store.save(request)

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
        request.final_answer = self._aggregate(request, session)
        self._audit(request, "aggregated", actor, strategy=request.merge_strategy)

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
        previous_ok = True
        for agent_id in ids:
            participant = Participant(agent_id=agent_id)
            request.participants.append(participant)
            if not previous_ok:
                # Strong dependency: no usable predecessor context -> skip dispatch.
                participant.status = ParticipantStatus.FAILED.value
                participant.error = "dependency_failed"
                self._audit(request, "dependency_failed", actor, agent_id=agent_id)
                self.store.save(request)
                continue
            if not self._dispatch_participant(request, session, participant, context, actor):
                previous_ok = False
                continue
            self._collect(request, participant, actor)
            if participant.status == ParticipantStatus.COMPLETED.value and participant.content:
                context = participant.content
            else:
                previous_ok = False
        self._finish_from_participants(request, session, actor)

    def _run_supervisor(
        self, request: MultiTriggerRequest, session: Any, ids: List[str]
    ) -> None:
        actor = session.initiator_id
        supervisor = request.supervisor_agent_id
        if not supervisor or not self.authz.can_invoke(session, supervisor):
            self._transition(
                request, RequestStatus.RUNNING.value, actor
            )
            self._transition(
                request,
                RequestStatus.FAILED.value,
                actor,
                reason="supervisor_unauthorized",
            )
            return

        sup_payload = sanitize_for_agent(
            {"user_prompt": request.user_prompt, "context": "supervisor planning"}
        )
        sup_key = self._downstream_key(request, supervisor, 0)
        sup_run = self.dispatcher.enqueue(supervisor, sup_key, sup_payload, None)
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
        task.attempt_count += 1  # attempts are 1-based
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
        task.downstream_key = self._downstream_key(
            request, task.worker_agent_id, task.attempt_count
        )
        task.run_id = self.dispatcher.enqueue(
            task.worker_agent_id, task.downstream_key, payload, None
        )
        request.invocation_graph.append(
            {
                "from": task.supervisor_agent_id,
                "to": task.worker_agent_id,
                "run_id": task.run_id,
                "task_id": task.task_id,
                "attempt": task.attempt_count,
            }
        )
        self._audit(
            request,
            "task_dispatched",
            actor,
            task_id=task.task_id,
            run_id=task.run_id,
            attempt_count=task.attempt_count,
        )
        result = self.dispatcher.await_result(task.run_id, request.timeout_seconds)
        if result.status != RUN_COMPLETED:
            assert_task_transition(task.status, TaskStatus.FAILED.value)
            task.status = TaskStatus.FAILED.value
            task.result = None
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
        self._audit(request, "task_submitted", actor, task_id=task.task_id)

    def review_task(
        self,
        request_id: str,
        task_id: str,
        token: str,
        action: str,
        feedback: Optional[Dict[str, Any]] = None,
    ) -> MultiTriggerRequest:
        with self._lock:
            session = self.authz.resolve_session(token)
            request = self.store.get_by_id(request_id)
            if request is None:
                raise NotFound(f"request {request_id} not found")
            if session.initiator_id not in (
                request.initiator_id,
                request.supervisor_agent_id,
            ):
                raise AuthorizationError("caller may not review this task")
            task = request.task(task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if task.status != TaskStatus.SUBMITTED.value:
                raise ValidationError(
                    f"task is {task.status}, only a submitted task can be reviewed"
                )
            if action not in ("approve", "reject"):
                raise ValidationError("action must be approve or reject")
            parsed: Optional[Feedback] = None
            if action == "reject":
                parsed = Feedback.from_dict(feedback)
                if not parsed.reject_reason:
                    raise ValidationError("reject requires feedback.reject_reason")

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
                    # _assign_task increments attempt_count for the new attempt.
                    self._assign_task(request, task, session.initiator_id)
                    self._start_and_submit_task(request, task, session.initiator_id, parsed)
                    # retry keeps the worker run id current; no duplicate submission
                    self._audit(request, "task_retry", session.initiator_id, task_id=task_id)

            task.updated_at = utcnow()
            self._maybe_finalize_supervisor(request, session)
            self.store.save(request)
            return request

    def _maybe_finalize_supervisor(
        self, request: MultiTriggerRequest, session: Any
    ) -> None:
        if request.status != RequestStatus.RUNNING.value or not request.tasks:
            return
        statuses = [t.status for t in request.tasks]
        if all(s == TaskStatus.COMPLETED.value for s in statuses):
            self._transition(request, RequestStatus.COMPLETED.value, session.initiator_id)
        elif all(s in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value) for s in statuses):
            self._transition(request, RequestStatus.PARTIAL.value, session.initiator_id)
        else:
            return
        request.final_answer = self._aggregate(request, session)
        self._audit(request, "aggregated", session.initiator_id, strategy=request.merge_strategy)

    # ---- late replies & reads ------------------------------------------------
    def ingest_late_reply(
        self, request_id: str, agent_id: str, content: str, token: str
    ) -> MultiTriggerRequest:
        with self._lock:
            session = self.authz.resolve_session(token)
            request = self.store.get_by_id(request_id)
            if request is None:
                raise NotFound(f"request {request_id} not found")
            if session.initiator_id != request.initiator_id:
                raise AuthorizationError("caller may not ingest replies for this request")
            participant = request.participant(agent_id)
            if participant is None:
                participant = Participant(agent_id=agent_id)
                request.participants.append(participant)
            if participant.status in (
                ParticipantStatus.COMPLETED.value,
                ParticipantStatus.RUNNING.value,
            ):
                return request
            participant.content = content
            participant.status = ParticipantStatus.COMPLETED.value
            participant.latency_ms = participant.latency_ms
            self._audit(
                request,
                "late_reply_ingested",
                session.initiator_id,
                agent_id=agent_id,
            )
            if (
                request.status == RequestStatus.PARTIAL.value
                and all(
                    p.status == ParticipantStatus.COMPLETED.value
                    for p in request.participants
                )
            ):
                self._transition(request, RequestStatus.COMPLETED.value, session.initiator_id)
                request.final_answer = self._aggregate(request, session)
            self.store.save(request)
            return request

    def get_status(self, request_id: str, token: str) -> Dict[str, Any]:
        with self._lock:
            self.authz.resolve_session(token)
            request = self.store.get_by_id(request_id)
            if request is None:
                raise NotFound(f"request {request_id} not found")
            return request.to_dict()

    def get_audit(self, request_id: str, token: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.authz.resolve_session(token)
            if self.store.get_by_id(request_id) is None:
                raise NotFound(f"request {request_id} not found")
            return self.store.get_audit(request_id)

    # ---- aggregation (untrusted agent text) ----------------------------------
    def _aggregate(
        self, request: MultiTriggerRequest, session: Any
    ) -> Dict[str, Any]:
        sources: List[Dict[str, Any]] = []
        suppressed: List[Dict[str, Any]] = []

        if request.mode == Mode.SUPERVISOR.value:
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
                # Treat agent output as untrusted: never act on embedded triggers.
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
            "sources": sources,
            "suppressed": suppressed,
            "disagreement_preserved": disagreements,
            "note": "Agent replies are untrusted text; each source is cited by run_id.",
        }
