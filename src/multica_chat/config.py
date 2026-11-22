from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceConfig:
    """Server-side limits. All are enforced in the service layer, never trusted
    from the request body.

    The first release explicitly disables participants triggering new group
    chats from their output (``allow_participant_triggered_chat = False``).
    """

    max_participants: int = 5
    max_concurrent_runs: int = 10
    max_depth: int = 3
    max_messages_per_request: int = 30
    max_requests_per_minute: int = 60
    default_max_attempts: int = 3
    default_timeout_seconds: int = 120
    allow_participant_triggered_chat: bool = False
