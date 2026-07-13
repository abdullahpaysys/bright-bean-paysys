"""Tests for simple deterministic command routing."""

from __future__ import annotations

import pytest

from apps.slack_bot.normalization import SlackAnalyticsRequest
from apps.slack_bot.routing import (
    SimpleBotResponse,
    is_greeting,
    is_help_command,
    is_status_command,
    normalize_command_text,
    route_simple_command,
)


def _make_request(text: str) -> SlackAnalyticsRequest:
    return SlackAnalyticsRequest(
        correlation_id="corr-1",
        event_id="Ev1",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        thread_ts="1720000000.000100",
        text=text,
    )


# ===========================================================================
# normalize_command_text
# ===========================================================================

def test_normalize_command_text_lowercases():
    assert normalize_command_text("HELLO") == "hello"


def test_normalize_command_text_strips_trailing_punctuation():
    assert normalize_command_text("hello!") == "hello"
    assert normalize_command_text("help?") == "help"
    assert normalize_command_text("status.") == "status"


def test_normalize_command_text_strips_whitespace():
    assert normalize_command_text("  hi  ") == "hi"


# ===========================================================================
# Greeting tests
# ===========================================================================

@pytest.mark.parametrize("text", ["hi", "hello", "hey", "salam", "assalam o alaikum"])
def test_greeting_variants(text):
    result = route_simple_command(_make_request(text))
    assert result.response_type == "greeting"
    assert len(result.text) > 0


def test_greeting_with_punctuation():
    result = route_simple_command(_make_request("hello!"))
    assert result.response_type == "greeting"


def test_greeting_uppercase():
    result = route_simple_command(_make_request("HI"))
    assert result.response_type == "greeting"


def test_is_greeting_helper():
    assert is_greeting("hi") is True
    assert is_greeting("hello!") is True
    assert is_greeting("top instagram") is False


# ===========================================================================
# Help tests
# ===========================================================================

@pytest.mark.parametrize("text", ["help", "what can you do", "commands", "examples"])
def test_help_variants(text):
    result = route_simple_command(_make_request(text))
    assert result.response_type == "help"
    assert "Examples:" in result.text


def test_help_with_punctuation():
    result = route_simple_command(_make_request("what can you do?"))
    assert result.response_type == "help"


def test_is_help_command_helper():
    assert is_help_command("help") is True
    assert is_help_command("commands") is True
    assert is_help_command("hello") is False


# ===========================================================================
# Status tests
# ===========================================================================

@pytest.mark.parametrize("text", ["status", "connected accounts", "connections", "account status"])
def test_status_variants(text):
    result = route_simple_command(_make_request(text))
    assert result.response_type == "status"
    assert "placeholder" in result.text.lower()


def test_is_status_command_helper():
    assert is_status_command("status") is True
    assert is_status_command("connected accounts") is True
    assert is_status_command("help") is False


# ===========================================================================
# Analytics placeholder tests
# ===========================================================================

@pytest.mark.parametrize(
    "text",
    [
        "top instagram post this week",
        "compare facebook vs linkedin last 30 days",
        "linkedin follower growth this month",
        "facebook reach last 7 days",
    ],
)
def test_analytics_placeholder_variants(text):
    result = route_simple_command(_make_request(text))
    assert result.response_type == "analytics_placeholder"
    assert "placeholder" in result.text.lower()


# ===========================================================================
# Response object tests
# ===========================================================================

def test_response_has_response_type():
    result = route_simple_command(_make_request("hi"))
    assert hasattr(result, "response_type")


def test_response_has_nonempty_text():
    result = route_simple_command(_make_request("hi"))
    assert len(result.text) > 0


def test_response_metadata_defaults_to_dict():
    result = route_simple_command(_make_request("hi"))
    assert result.metadata == {}


def test_response_is_frozen():
    result = route_simple_command(_make_request("hi"))
    with pytest.raises(AttributeError):
        result.text = "mutated"


def test_response_is_simple_bot_response():
    result = route_simple_command(_make_request("hi"))
    assert isinstance(result, SimpleBotResponse)


# ===========================================================================
# Edge cases
# ===========================================================================

def test_empty_string_falls_to_analytics_placeholder():
    """Empty text is not greeting/help/status → analytics_placeholder."""
    result = route_simple_command(_make_request(""))
    assert result.response_type == "analytics_placeholder"


def test_unknown_command_falls_to_analytics_placeholder():
    result = route_simple_command(_make_request("some random question about twitter"))
    assert result.response_type == "analytics_placeholder"


def test_greeting_with_leading_trailing_whitespace():
    result = route_simple_command(_make_request("  hey  "))
    assert result.response_type == "greeting"


# ===========================================================================
# Safety — no external service imports
# ===========================================================================

def test_routing_module_does_not_import_slack_sdk():
    """routing.py should not import any Slack Web API client."""
    import apps.slack_bot.routing as routing_mod
    source = open(routing_mod.__file__).read()
    assert "slack_sdk" not in source
    assert "WebClient" not in source
    assert "from slack" not in source


def test_routing_module_does_not_import_llm_clients():
    """routing.py should not import Claude/Z.AI/GLM clients."""
    import apps.slack_bot.routing as routing_mod
    source = open(routing_mod.__file__).read()
    assert "anthropic" not in source
    assert "openai" not in source
    assert "zhipuai" not in source
    assert "claude" not in source.lower() or "claude" not in source


def test_routing_module_does_not_import_brightbean_analytics():
    """routing.py should not import BrightBean analytics services."""
    import apps.slack_bot.routing as routing_mod
    source = open(routing_mod.__file__).read()
    assert "apps.analytics" not in source
    assert "fetch_analytics" not in source
    assert "AnalyticsService" not in source
