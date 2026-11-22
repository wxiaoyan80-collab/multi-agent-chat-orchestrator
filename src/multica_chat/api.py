"""HTTP API for the multi-trigger platform service.

Identity is taken from the authenticated session header, never from the body.
Domain errors are mapped to stable HTTP status codes.
"""

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    AuthorizationError,
    BudgetExceeded,
    InvalidTransition,
    NotFound,
    PlatformError,
    ValidationError,
)
from .service import MultiTriggerService, TriggerInput


class TriggerPayload(BaseModel):
    user_prompt: str
    participant_agent_ids: List[str]
    idempotency_key: str
    mode: str = "parallel"
    supervisor_agent_id: Optional[str] = None
    timeout_seconds: int = 120
    merge_strategy: str = "summary"
    max_attempts: int = 3
    upstream_depth: int = 0
    seen_agents: List[str] = Field(default_factory=list)
    allow_loop_override: bool = False


class FeedbackPayload(BaseModel):
    reject_reason: str = ""
    change_instructions: str = ""


class ReviewPayload(BaseModel):
    action: str
    feedback: Optional[FeedbackPayload] = None


class LateReplyPayload(BaseModel):
    agent_id: str
    content: str


_STATUS = {
    AuthorizationError: 403,
    BudgetExceeded: 429,
    InvalidTransition: 409,
    ValidationError: 400,
    NotFound: 404,
}


def create_app(service: MultiTriggerService) -> FastAPI:
    app = FastAPI(title="Multi-Agent Chat Platform Service")

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_request: Request, exc: PlatformError) -> JSONResponse:
        status = _STATUS.get(type(exc), 400)
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/chat/multi-trigger")
    def trigger(
        payload: TriggerPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        request = service.trigger(x_session_token, TriggerInput(**payload.dict()))
        return request.to_dict()

    @app.post("/api/v1/chat/trigger-preview")
    def trigger_preview(
        payload: TriggerPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        return service.preview(x_session_token, TriggerInput(**payload.dict()))

    @app.get("/api/v1/chat/multi-trigger/{request_id}")
    def get_status(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        return service.get_status(request_id, x_session_token)

    @app.get("/api/v1/chat/multi-trigger/{request_id}/audit")
    def get_audit(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> List[Dict[str, Any]]:
        return service.get_audit(request_id, x_session_token)

    @app.post("/api/v1/chat/multi-trigger/{request_id}/tasks/{task_id}/review")
    def review_task(
        request_id: str,
        task_id: str,
        payload: ReviewPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        feedback = payload.feedback.dict() if payload.feedback else None
        request = service.review_task(
            request_id, task_id, x_session_token, payload.action, feedback
        )
        return request.to_dict()

    @app.post("/api/v1/chat/multi-trigger/{request_id}/late-reply")
    def late_reply(
        request_id: str,
        payload: LateReplyPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        request = service.ingest_late_reply(
            request_id, payload.agent_id, payload.content, x_session_token
        )
        return request.to_dict()

    return app
