import asyncio

import httpx
import pytest

from multica_chat.api import create_app


class ApiClient:
    """Thin sync wrapper over httpx ASGI transport (avoids starlette TestClient
    version coupling)."""

    def __init__(self, app):
        self.app = app

    def request(self, method, url, **kwargs):
        async def _call():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_call())

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


@pytest.fixture
def client(service):
    return ApiClient(create_app(service))


HEADERS = {"X-Session-Token": "human-token"}


def test_trigger_and_get_status(client):
    response = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "hi",
            "participant_agent_ids": ["a1", "a2"],
            "idempotency_key": "api-key-1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    request_id = body["request_id"]
    assert body["status"] == "completed"

    got = client.get(f"/api/v1/chat/multi-trigger/{request_id}", headers=HEADERS)
    assert got.status_code == 200
    assert got.json()["request_id"] == request_id

    # Repeated send with same idempotency key returns the same record.
    again = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "hi",
            "participant_agent_ids": ["a1", "a2"],
            "idempotency_key": "api-key-1",
        },
    )
    assert again.json()["request_id"] == request_id


def test_supervisor_review_via_api(client, dispatcher):
    dispatcher.set_response("a1", "work")
    triggered = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "do work",
            "participant_agent_ids": ["a1"],
            "idempotency_key": "api-sup",
            "mode": "supervisor",
            "supervisor_agent_id": "sup",
        },
    ).json()
    task_id = triggered["tasks"][0]["task_id"]
    reviewed = client.post(
        f"/api/v1/chat/multi-trigger/{triggered['request_id']}/tasks/{task_id}/review",
        headers=HEADERS,
        json={"action": "approve"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "completed"


def test_error_status_mapping(client):
    bad_session = client.post(
        "/api/v1/chat/multi-trigger",
        headers={"X-Session-Token": "nope"},
        json={"user_prompt": "hi", "participant_agent_ids": ["a1"], "idempotency_key": "x"},
    )
    assert bad_session.status_code == 403

    no_supervisor = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "hi",
            "participant_agent_ids": ["a1"],
            "idempotency_key": "y",
            "mode": "supervisor",
        },
    )
    assert no_supervisor.status_code == 400

    over_cap = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "hi",
            "participant_agent_ids": [f"a{i}" for i in range(6)],
            "idempotency_key": "z",
        },
    )
    assert over_cap.status_code == 429
