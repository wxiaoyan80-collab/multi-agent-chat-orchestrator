import pytest

from multica_chat.config import ServiceConfig
from multica_chat.errors import BudgetExceeded, LoopDetected
from multica_chat.security import (
    contains_trigger_instruction,
    enforce_invocation_limits,
    enforce_participant_budget,
    sanitize_for_agent,
    summarize,
)


def test_sanitize_drops_whitelist_and_nested_secrets():
    payload = {
        "user_prompt": "hello",
        "custom_env": {"AWS_SECRET": "x"},
        "mcp_config": {"token": "y"},
        "context": {
            "safe": "ok",
            "api_key": "should-drop",
            "nested": {"password": "drop", "keep": "yes"},
        },
        "totally_unknown": "drop",
    }
    clean = sanitize_for_agent(payload)
    assert clean == {
        "user_prompt": "hello",
        "context": {"safe": "ok", "nested": {"keep": "yes"}},
    }


def test_participant_budget_cap():
    config = ServiceConfig(max_participants=5, max_concurrent_runs=10)
    enforce_participant_budget(["a1", "a2", "a3", "a4", "a5"], config)
    with pytest.raises(BudgetExceeded):
        enforce_participant_budget([f"a{i}" for i in range(6)], config)


def test_loop_detection_depth_and_cycle_and_override():
    config = ServiceConfig(max_depth=3, max_messages_per_request=30)
    with pytest.raises(LoopDetected):
        enforce_invocation_limits(["a1"], None, 3, [], False, config)
    with pytest.raises(LoopDetected):
        enforce_invocation_limits(["a1"], None, 0, ["a1"], False, config)
    # Explicit human override permits a seen-agent cycle.
    enforce_invocation_limits(["a1"], None, 0, ["a1"], True, config)


def test_trigger_instruction_detection_and_summary():
    assert contains_trigger_instruction("Please start a multi-trigger to other agents")
    assert contains_trigger_instruction("请发起群聊")
    assert not contains_trigger_instruction("The deployment succeeded.")
    assert summarize("line one\nline two\nline three") == "line one line two"
    assert "..." in summarize("x" * 500)
