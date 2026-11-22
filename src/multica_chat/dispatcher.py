"""Dispatcher abstraction.

The platform service talks to this interface instead of the concrete Multica
trigger/enqueue path, which keeps state-machine behaviour testable. The real
adapter can wrap ``trigger-preview``/mention enqueue and run subscription.
"""

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Union
from uuid import uuid4

RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_TIMEOUT = "timeout"


@dataclass
class RunResult:
    run_id: str
    agent_id: str
    status: str
    content: Optional[str] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class Dispatcher:
    def enqueue(
        self,
        agent_id: str,
        idempotency_key: str,
        payload: Dict[str, Any],
        context: Optional[str] = None,
    ) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def await_result(self, run_id: str, timeout_seconds: int) -> RunResult:  # pragma: no cover
        raise NotImplementedError


class FakeDispatcher(Dispatcher):
    """Deterministic in-memory dispatcher with downstream idempotency.

    ``set_response(agent_id, result_or_list)`` controls what an enqueued run
    returns. A list is consumed one item per enqueue, which is how retry /
    queue-retry scenarios are simulated.
    """

    def __init__(self) -> None:
        self.enqueued: Dict[str, str] = {}
        self.results: Dict[str, RunResult] = {}
        self.responses: Dict[str, Union[RunResult, List[RunResult], Deque[RunResult]]] = {}
        self.calls: List[Dict[str, Any]] = []

    def set_response(
        self,
        agent_id: str,
        result: Union[RunResult, List[RunResult], str],
    ) -> None:
        def normalize(item: Any) -> RunResult:
            if isinstance(item, str):
                return RunResult("", agent_id, RUN_COMPLETED, content=item)
            return item

        if isinstance(result, (list, deque)):
            self.responses[agent_id] = deque(normalize(item) for item in result)
        else:
            self.responses[agent_id] = normalize(result)

    def enqueue(
        self,
        agent_id: str,
        idempotency_key: str,
        payload: Dict[str, Any],
        context: Optional[str] = None,
    ) -> str:
        # Downstream idempotency: a repeated enqueue with the same key resolves
        # to the same run instead of creating a duplicate.
        if idempotency_key in self.enqueued:
            return self.enqueued[idempotency_key]

        run_id = f"run-{uuid4().hex[:12]}"
        self.enqueued[idempotency_key] = run_id
        self.calls.append(
            {
                "agent_id": agent_id,
                "idempotency_key": idempotency_key,
                "payload": payload,
                "context": context,
                "run_id": run_id,
            }
        )

        configured = self.responses.get(agent_id)
        if isinstance(configured, deque):
            result = configured.popleft() if configured else self._default(agent_id)
        elif isinstance(configured, list):
            configured = deque(configured)
            self.responses[agent_id] = configured
            result = configured.popleft() if configured else self._default(agent_id)
        elif isinstance(configured, RunResult):
            result = configured
        else:
            result = self._default(agent_id)

        self.results[run_id] = RunResult(
            run_id=run_id,
            agent_id=agent_id,
            status=result.status,
            content=result.content,
            latency_ms=result.latency_ms if result.latency_ms is not None else 42,
            error=result.error,
        )
        return run_id

    @staticmethod
    def _default(agent_id: str) -> RunResult:
        return RunResult(
            run_id="",
            agent_id=agent_id,
            status=RUN_COMPLETED,
            content=f"mock reply from {agent_id}",
            latency_ms=42,
        )

    def await_result(self, run_id: str, timeout_seconds: int) -> RunResult:
        result = self.results.get(run_id)
        if result is None:
            return RunResult(run_id, "", RUN_TIMEOUT, error="no result")
        return result

    def set_late_result(self, run_id: str, result: RunResult) -> None:
        """Simulate a reply arriving after the collection window closed."""
        self.results[run_id] = RunResult(
            run_id=run_id,
            agent_id=result.agent_id,
            status=result.status,
            content=result.content,
            latency_ms=result.latency_ms,
            error=result.error,
        )
