"""HTTP API for the multi-trigger platform service (MARS-22 contract).

Identity comes from the authenticated session header. Domain errors are mapped
to the stable error codes / HTTP statuses defined in the MARS-22 API spec.
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .errors import PlatformError, ValidationError
from .service import MultiTriggerService, TriggerInput

_IF_MATCH = re.compile(r'^"task:(\d+)"$')


class TriggerPayload(BaseModel):
    user_prompt: str = Field(..., min_length=1, max_length=8000)
    participant_agent_ids: List[str] = Field(..., min_items=1)
    idempotency_key: Optional[str] = None
    mode: str = "parallel"
    supervisor_agent_id: Optional[str] = None
    timeout_seconds: int = Field(120, ge=1, le=600)
    merge_strategy: str = "summary"
    max_attempts: int = Field(3, ge=1, le=10)
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


def _error(exc: PlatformError, request_id: Optional[str] = None) -> JSONResponse:
    details = dict(exc.details)
    if request_id:
        details["request_id"] = request_id
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "details": details,
            }
        },
    )


def create_app(service: MultiTriggerService) -> FastAPI:
    app = FastAPI(title="Multi-Agent Chat Platform Service")

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_request: Request, exc: PlatformError) -> JSONResponse:
        return _error(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(ValidationError("invalid argument", errors=str(exc.errors())))

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/chat/multi-trigger")
    def trigger(
        payload: TriggerPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ) -> Response:
        key = idempotency_key or payload.idempotency_key
        if not key:
            return _error(ValidationError("idempotency_key is required"))
        inp = TriggerInput(**payload.dict())
        inp.idempotency_key = key
        request = service.trigger(x_session_token, inp)
        replayed = bool(
            request.audit and request.audit[-1].action == "idempotent_replay"
        )
        headers = {"Idempotent-Replay": "true"} if replayed else None
        return JSONResponse(
            status_code=200 if replayed else 202,
            content=service.get_status(request.request_id, x_session_token),
            headers=headers,
        )

    @app.post("/api/v1/chat/multi-trigger/preview")
    @app.post("/api/v1/chat/trigger-preview")  # backward-compatible alias
    def trigger_preview(
        payload: TriggerPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        return service.preview(x_session_token, TriggerInput(**payload.dict()))

    @app.get("/api/v1/chat/multi-trigger/{request_id}")
    def get_status(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
        since_version: Optional[int] = None,
    ) -> Response:
        snapshot = service.get_status(request_id, x_session_token, since_version)
        if snapshot is None:
            return Response(status_code=304)
        return JSONResponse(content=snapshot)

    @app.get("/api/v1/chat/multi-trigger/{request_id}/runs")
    def get_runs(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
        since_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        return service.get_runs(request_id, x_session_token, since_version)

    @app.get("/api/v1/chat/multi-trigger/{request_id}/trace")
    def get_trace(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
        since_seq: int = 0,
    ) -> Dict[str, Any]:
        return service.get_trace(request_id, x_session_token, since_seq)

    @app.get("/api/v1/chat/multi-trigger/{request_id}/audit")
    def get_audit(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> List[Dict[str, Any]]:
        return service.get_audit(request_id, x_session_token)

    @app.post("/api/v1/chat/multi-trigger/{request_id}/cancel")
    def cancel(
        request_id: str,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        return service.cancel_request(request_id, x_session_token).to_dict()

    @app.post("/api/v1/chat/multi-trigger/{request_id}/late-reply")
    def late_reply(
        request_id: str,
        payload: LateReplyPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
    ) -> Dict[str, Any]:
        return service.ingest_late_reply(
            request_id, payload.agent_id, payload.content, x_session_token
        ).to_dict()

    @app.post("/api/v1/chat/multi-trigger/{request_id}/tasks/{task_id}/review")
    def review_task(
        request_id: str,
        task_id: str,
        payload: ReviewPayload,
        x_session_token: str = Header(..., alias="X-Session-Token"),
        if_match: Optional[str] = Header(None, alias="If-Match"),
    ) -> Dict[str, Any]:
        expected_version: Optional[int] = None
        if if_match:
            match = _IF_MATCH.match(if_match.strip())
            if not match:
                return _error(ValidationError("If-Match must be \"task:<version>\""))
            expected_version = int(match.group(1))
        service.review_task(
            request_id,
            task_id,
            x_session_token,
            payload.action,
            payload.feedback.dict() if payload.feedback else None,
            expected_version=expected_version,
        )
        return service.review_response(request_id, task_id, x_session_token)

    return app
