"""Local runnable app.

Demo wiring only: replace the static authz provider and fake dispatcher with
the platform's real session gate and trigger adapter for deployment.
"""

from .api import create_app
from .config import ServiceConfig
from .dispatcher import FakeDispatcher
from .security import Session, StaticAuthorizationProvider
from .service import MultiTriggerService
from .storage import RequestStore

_demo_agents = {"agent-a", "agent-b", "agent-c", "supervisor"}
_authz = StaticAuthorizationProvider(
    {
        "demo-token": Session(
            session_id="demo-session",
            initiator_id="demo-user",
            workspace_id="demo-workspace",
            invocable_agents=set(_demo_agents),
            readable_agents=set(_demo_agents),
        )
    }
)
_service = MultiTriggerService(
    RequestStore("multica_chat.sqlite3"),
    FakeDispatcher(),
    _authz,
    ServiceConfig(),
)

app = create_app(_service)
