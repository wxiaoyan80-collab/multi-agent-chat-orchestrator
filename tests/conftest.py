import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from multica_chat.config import ServiceConfig
from multica_chat.dispatcher import FakeDispatcher
from multica_chat.security import Session, StaticAuthorizationProvider
from multica_chat.service import MultiTriggerService, TriggerInput
from multica_chat.storage import RequestStore


@pytest.fixture
def config():
    return ServiceConfig()


@pytest.fixture
def authz():
    provider = StaticAuthorizationProvider()
    provider.add_session(
        "human-token",
        Session(
            session_id="s1",
            initiator_id="alice",
            workspace_id="ws1",
            is_human=True,
            invocable_agents={"a1", "a2", "a3", "sup", "worker"},
            readable_agents={"a1", "a2", "a3", "worker"},
        ),
    )
    provider.add_session(
        "sup-token",
        Session(
            session_id="s2",
            initiator_id="sup",
            workspace_id="ws1",
            is_human=False,
            invocable_agents=set(),
            readable_agents=set(),
        ),
    )
    return provider


@pytest.fixture
def dispatcher():
    return FakeDispatcher()


@pytest.fixture
def store(tmp_path):
    instance = RequestStore(str(tmp_path / "requests.db"))
    yield instance
    instance.close()


@pytest.fixture
def service(store, dispatcher, authz, config):
    return MultiTriggerService(store, dispatcher, authz, config)


@pytest.fixture
def parallel_input():
    return TriggerInput(
        user_prompt="Please review",
        participant_agent_ids=["a1", "a2"],
        idempotency_key="key-parallel",
    )
