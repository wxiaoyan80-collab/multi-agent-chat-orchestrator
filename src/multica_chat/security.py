"""Invoke-gate, data-isolation and budget enforcement.

Everything here runs server-side: identity is derived from the authenticated
session, authorization is re-checked at actual dispatch time (preview is only
advisory), and payloads are scrubbed with a field whitelist before they reach
any agent.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set

from .config import ServiceConfig
from .errors import AuthorizationError, BudgetExceeded, LoopDetected, Unauthenticated


@dataclass
class Session:
    """An authenticated session. ``initiator_id`` is the human at the top of
    the delegation chain; agent identities never become the chain top."""

    session_id: str
    initiator_id: str
    workspace_id: str
    is_human: bool = True
    invocable_agents: Set[str] = field(default_factory=set)
    readable_agents: Set[str] = field(default_factory=set)


class AuthorizationProvider:
    def resolve_session(self, token: str) -> Session:  # pragma: no cover - interface
        raise NotImplementedError

    def can_invoke(self, session: Session, agent_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def can_read(self, session: Session, agent_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError


class StaticAuthorizationProvider(AuthorizationProvider):
    """In-memory provider suitable for tests and local prototyping. Replace
    with the platform's real session/invoke gate in production."""

    def __init__(self, sessions: Optional[Dict[str, Session]] = None):
        self.sessions: Dict[str, Session] = dict(sessions or {})

    def add_session(self, token: str, session: Session) -> None:
        self.sessions[token] = session

    def resolve_session(self, token: str) -> Session:
        session = self.sessions.get(token)
        if session is None:
            raise Unauthenticated("invalid or expired session")
        return session

    def can_invoke(self, session: Session, agent_id: str) -> bool:
        return agent_id in session.invocable_agents

    def can_read(self, session: Session, agent_id: str) -> bool:
        # Invoking implies reading; readability can be narrower.
        return agent_id in session.readable_agents or agent_id in session.invocable_agents


# ---- secret non-propagation -------------------------------------------------
_SENSITIVE_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "credential",
    "custom_env",
    "mcp_config",
    "env",
    "connector",
    "tool_log",
    "private_history",
    "authorization",
    "cookie",
)
_ALLOWED_TOP_LEVEL = {
    "user_prompt",
    "task",
    "context",
    "attachments",
    "metadata",
    "feedback",
}


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _is_sensitive_key(str(k))}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def sanitize_for_agent(
    payload: Dict[str, Any], allowed_fields: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """Return only whitelisted, recursively-scrubbed fields for an agent."""
    allowed = set(allowed_fields) if allowed_fields is not None else _ALLOWED_TOP_LEVEL
    clean: Dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key not in allowed or _is_sensitive_key(str(key)):
            continue
        clean[key] = _scrub(value)
    return clean


# ---- budgets & loop detection ----------------------------------------------
def enforce_participant_budget(participant_ids: Iterable[str], config: ServiceConfig) -> None:
    count = len(list(participant_ids))
    if count > config.max_participants:
        raise BudgetExceeded(
            f"participant count {count} exceeds limit {config.max_participants}"
        )
    if count > config.max_concurrent_runs:
        raise BudgetExceeded(
            f"participant count {count} exceeds concurrency quota {config.max_concurrent_runs}"
        )


def enforce_invocation_limits(
    participant_ids: Iterable[str],
    supervisor_agent_id: Optional[str],
    upstream_depth: int,
    seen_agents: Iterable[str],
    allow_override: bool,
    config: ServiceConfig,
) -> None:
    if upstream_depth >= config.max_depth:
        raise LoopDetected(
            f"invocation depth {upstream_depth} reached max_depth {config.max_depth}"
        )
    ids = set(participant_ids)
    if supervisor_agent_id:
        ids.add(supervisor_agent_id)
    cycle = ids & set(seen_agents)
    if cycle and not allow_override:
        raise LoopDetected(f"agents already in the invocation chain: {sorted(cycle)}")
    projected = upstream_depth + 1 + len(ids)
    if projected > config.max_messages_per_request:
        raise BudgetExceeded(
            f"projected messages {projected} exceed {config.max_messages_per_request}"
        )


# ---- untrusted agent text ---------------------------------------------------
_TRIGGER_INSTRUCTION = re.compile(
    r"(multi[-_\s]?trigger|群聊|trigger\s+(?:new\s+)?(?:chat|agent)|发起新的?群聊)",
    re.IGNORECASE,
)


def contains_trigger_instruction(text: Optional[str]) -> bool:
    """Detect (so we can *ignore*) instructions inside agent output that try to
    start a new group chat. First release never acts on these."""
    return bool(_TRIGGER_INSTRUCTION.search(text or ""))


def summarize(text: Optional[str], max_lines: int = 2, max_chars: int = 240) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    summary = " ".join(lines[:max_lines])
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3] + "..."
    return summary
