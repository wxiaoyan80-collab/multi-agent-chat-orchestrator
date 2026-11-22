from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Mode(str, Enum):
    LINEAR = "linear"
    PARALLEL = "parallel"
    SUPERVISOR = "supervisor"


class MergeStrategy(str, Enum):
    SUMMARY = "summary"
    CHOOSE_BEST = "choose_best"
    CONCATENATE = "concatenate"


class RequestStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class ParticipantStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class TaskStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass
class Feedback:
    reject_reason: str = ""
    change_instructions: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Feedback":
        if not data:
            return cls()
        return cls(
            reject_reason=data.get("reject_reason", "") or "",
            change_instructions=data.get("change_instructions", "") or "",
        )


@dataclass
class Participant:
    agent_id: str
    status: str = ParticipantStatus.QUEUED.value
    run_id: Optional[str] = None
    downstream_key: str = ""
    content: Optional[str] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    injected_context: Optional[str] = None


@dataclass
class Task:
    task_id: str
    parent_request_id: str
    supervisor_agent_id: str
    worker_agent_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    status: str = TaskStatus.PENDING.value
    feedback: Optional[Feedback] = None
    run_id: Optional[str] = None
    downstream_key: str = ""
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        d = dict(d)
        d["feedback"] = Feedback.from_dict(d.get("feedback"))
        return cls(**d)


@dataclass
class AuditEvent:
    timestamp: str
    actor: str
    action: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiTriggerRequest:
    request_id: str
    idempotency_key: str
    initiator_id: str
    workspace_id: str
    session_id: str
    user_prompt: str
    mode: str
    participant_ids: List[str]
    supervisor_agent_id: Optional[str] = None
    timeout_seconds: int = 120
    merge_strategy: str = MergeStrategy.SUMMARY.value
    max_attempts: int = 3
    upstream_depth: int = 0
    seen_agents: List[str] = field(default_factory=list)
    allow_loop_override: bool = False
    status: str = RequestStatus.PENDING.value
    participants: List[Participant] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    audit: List[AuditEvent] = field(default_factory=list)
    final_answer: Optional[Dict[str, Any]] = None
    invocation_graph: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MultiTriggerRequest":
        d = dict(d)
        d["participants"] = [Participant(**p) for p in d.get("participants", [])]
        d["tasks"] = [Task.from_dict(t) for t in d.get("tasks", [])]
        d["audit"] = [AuditEvent(**a) for a in d.get("audit", [])]
        return cls(**d)

    # ---- convenience lookups -------------------------------------------------
    def participant(self, agent_id: str) -> Optional[Participant]:
        return next((p for p in self.participants if p.agent_id == agent_id), None)

    def task(self, task_id: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.task_id == task_id), None)
