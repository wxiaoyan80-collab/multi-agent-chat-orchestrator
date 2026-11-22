import asyncio

import httpx
import pytest

from multica_chat.api import create_app


class ApiClient:
    """Sync wrapper over httpx ASGI transport."""

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


def test_trigger_status_and_idempotent_replay(client):
    body = {
        "user_prompt": "hi",
        "participant_agent_ids": ["a1", "a2"],
        "idempotency_key": "api-key-1",
    }
    response = client.post("/api/v1/chat/multi-trigger", headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    assert response.json()["status"] == "completed"
    assert "request_id" in response.json()

    got = client.get(f"/api/v1/chat/multi-trigger/{request_id}", headers=HEADERS)
    assert got.status_code == 200
    assert got.json()["request_id"] == request_id

    not_modified = client.get(
        f"/api/v1/chat/multi-trigger/{request_id}?since_version={got.json()['version']}",
        headers=HEADERS,
    )
    assert not_modified.status_code == 304

    again = client.post("/api/v1/chat/multi-trigger", headers=HEADERS, json=body)
    assert again.status_code == 200
    assert again.headers["idempotent-replay"] == "true"
    assert again.json()["request_id"] == request_id


def test_runs_trace_and_cancel_endpoints(client):
    created = client.post(
        "/api/v1/chat/multi-trigger",
        headers={**HEADERS, "Idempotency-Key": "api-key-runs"},
        json={"user_prompt": "hi", "participant_agent_ids": ["a1"]},
    ).json()
    rid = created["request_id"]
    runs = client.get(f"/api/v1/chat/multi-trigger/{rid}/runs", headers=HEADERS)
    assert runs.status_code == 200
    assert len(runs.json()["runs"]) == 1
    trace = client.get(f"/api/v1/chat/multi-trigger/{rid}/trace?since_seq=0", headers=HEADERS)
    assert trace.json()["events"][0]["seq"] == 1
    # Already final -> cancellation is refused (409 request_already_final)
    cancelled = client.post(f"/api/v1/chat/multi-trigger/{rid}/cancel", headers=HEADERS)
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "request_already_final"


def test_supervisor_review_via_api_with_if_match(client, dispatcher):
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
    version = triggered["tasks"][0]["version"]
    reviewed = client.post(
        f"/api/v1/chat/multi-trigger/{triggered['request_id']}/tasks/{task_id}/review",
        headers={**HEADERS, "If-Match": f'"task:{version}"'},
        json={"action": "approve"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "completed"
    assert reviewed.json()["request_status"] == "completed"

    stale = client.post(
        f"/api/v1/chat/multi-trigger/{triggered['request_id']}/tasks/{task_id}/review",
        headers={**HEADERS, "If-Match": '"task:0"'},
        json={"action": "approve"},
    )
    assert stale.status_code == 412


def test_error_status_mapping(client):
    unauthenticated = client.post(
        "/api/v1/chat/multi-trigger",
        headers={"X-Session-Token": "nope"},
        json={"user_prompt": "hi", "participant_agent_ids": ["a1"], "idempotency_key": "x"},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "unauthenticated"

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
    assert no_supervisor.json()["error"]["code"] == "invalid_argument"

    over_cap = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={
            "user_prompt": "hi",
            "participant_agent_ids": [f"a{i}" for i in range(6)],
            "idempotency_key": "z",
        },
    )
    assert over_cap.status_code == 422
    assert over_cap.json()["error"]["code"] == "budget_exceeded"

    client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={"user_prompt": "original", "participant_agent_ids": ["a1"], "idempotency_key": "dup-key"},
    )
    conflict = client.post(
        "/api/v1/chat/multi-trigger",
        headers=HEADERS,
        json={"user_prompt": "changed", "participant_agent_ids": ["a1"], "idempotency_key": "dup-key"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"
