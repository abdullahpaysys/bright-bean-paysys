"""Tests for Slack message delivery adapter."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from django.test import override_settings

from apps.slack_bot.delivery import (
    SlackDeliveryResult,
    deliver_slack_response,
    get_slack_bot_token,
    send_slack_message,
)
from apps.slack_bot.exceptions import SlackDeliveryError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, body_dict=None, text=None):
    """Create a mock HTTP response object."""
    mock = MagicMock()
    mock.status_code = status_code
    if text is not None:
        mock.text = text
    elif body_dict is not None:
        mock.text = json.dumps(body_dict)
    else:
        mock.text = '{"ok": true, "ts": "1720000001.000100"}'
    return mock


def _mock_http_post(response=None):
    """Create a mock http_post callable that records calls."""
    if response is None:
        response = _mock_response()

    def _post(url, *, json_body, headers):
        _post.last_url = url
        _post.last_json_body = json_body
        _post.last_headers = headers
        return response

    _post.last_url = None
    _post.last_json_body = None
    _post.last_headers = None
    return _post


# ===========================================================================
# 1. Success without thread
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_send_success_without_thread():
    resp = _mock_response(body_dict={"ok": True, "channel": "C123", "ts": "1720000001.000100"})
    post = _mock_http_post(resp)

    result = send_slack_message(
        channel_id="C123",
        text="Hello from bot",
        thread_ts="",
        http_post=post,
    )

    assert result.ok is True
    assert result.channel_id == "C123"
    assert result.response_ts == "1720000001.000100"
    assert post.last_json_body["channel"] == "C123"
    assert post.last_json_body["text"] == "Hello from bot"
    assert "thread_ts" not in post.last_json_body


# ===========================================================================
# 2. Success with thread
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_send_success_with_thread():
    resp = _mock_response(body_dict={"ok": True, "ts": "1720000001.000200"})
    post = _mock_http_post(resp)

    result = send_slack_message(
        channel_id="C123",
        text="Threaded reply",
        thread_ts="1720000000.000100",
        http_post=post,
    )

    assert result.ok is True
    assert post.last_json_body["thread_ts"] == "1720000000.000100"


# ===========================================================================
# 3. Missing token
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="")
def test_missing_token_returns_failure():
    result = send_slack_message(
        channel_id="C123",
        text="hello",
        token="",
    )
    assert result.ok is False
    assert "token" in result.error.lower()


# ===========================================================================
# 4. Empty channel
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test")
def test_empty_channel_returns_failure():
    result = send_slack_message(
        channel_id="",
        text="hello",
    )
    assert result.ok is False
    assert "channel" in result.error.lower()


# ===========================================================================
# 5. Empty text
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test")
def test_empty_text_returns_failure():
    result = send_slack_message(
        channel_id="C123",
        text="",
    )
    assert result.ok is False
    assert "text" in result.error.lower()


# ===========================================================================
# 6. Slack API error
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_slack_api_error_returns_failure():
    resp = _mock_response(body_dict={"ok": False, "error": "channel_not_found"})
    post = _mock_http_post(resp)

    result = send_slack_message(
        channel_id="C123",
        text="hello",
        http_post=post,
    )

    assert result.ok is False
    assert result.error == "channel_not_found"


# ===========================================================================
# 7. Network/HTTP exception
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_http_exception_returns_failure():
    def exploding_post(url, *, json_body, headers):
        raise ConnectionError("Network down")

    result = send_slack_message(
        channel_id="C123",
        text="hello",
        http_post=exploding_post,
    )

    assert result.ok is False
    assert "HTTP error" in result.error


# ===========================================================================
# 8. Invalid JSON response
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_invalid_json_response_returns_failure():
    resp = _mock_response(text="not json {{{")
    post = _mock_http_post(resp)

    result = send_slack_message(
        channel_id="C123",
        text="hello",
        http_post=post,
    )

    assert result.ok is False
    assert "json" in result.error.lower()


# ===========================================================================
# 9. deliver_slack_response success
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-test-token")
def test_deliver_slack_response_success():
    resp = _mock_response(body_dict={"ok": True, "ts": "1720000001.000300"})
    post = _mock_http_post(resp)

    # Monkey-patch send_slack_message to use our mock http_post
    import apps.slack_bot.delivery as delivery_mod

    original_send = delivery_mod.send_slack_message

    def _patched_send(channel_id, text, thread_ts="", token=None, http_post=None):
        return original_send(
            channel_id=channel_id,
            text=text,
            thread_ts=thread_ts,
            token=token,
            http_post=post,
        )

    delivery_mod.send_slack_message = _patched_send
    try:
        ts = deliver_slack_response(
            channel_id="C123",
            text="Response text",
            thread_ts="1720000000.000100",
        )
        assert ts == "1720000001.000300"
    finally:
        delivery_mod.send_slack_message = original_send


# ===========================================================================
# 10. deliver_slack_response failure
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="")
def test_deliver_slack_response_failure_raises():
    with pytest.raises(SlackDeliveryError):
        deliver_slack_response(
            channel_id="C123",
            text="hello",
            thread_ts="",
        )


# ===========================================================================
# 11. Authorization header
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-my-token")
def test_authorization_header_set():
    resp = _mock_response(body_dict={"ok": True, "ts": "1720000001.000400"})
    post = _mock_http_post(resp)

    send_slack_message(
        channel_id="C123",
        text="hello",
        http_post=post,
    )

    assert post.last_headers["Authorization"] == "Bearer xoxb-my-token"


# ===========================================================================
# 12. get_slack_bot_token
# ===========================================================================

@override_settings(SLACK_BOT_TOKEN="xoxb-from-settings")
def test_get_token_from_settings():
    assert get_slack_bot_token() == "xoxb-from-settings"


# ===========================================================================
# 13. SlackDeliveryResult dataclass
# ===========================================================================

def test_delivery_result_is_frozen():
    result = SlackDeliveryResult(ok=True, channel_id="C123")
    with pytest.raises(AttributeError):
        result.ok = False


def test_delivery_result_defaults():
    result = SlackDeliveryResult(ok=True, channel_id="C123")
    assert result.response_ts == ""
    assert result.error == ""
    assert result.raw_response is None


# ===========================================================================
# 14. Safety — no external service imports
# ===========================================================================

def test_delivery_module_does_not_import_llm_clients():
    import apps.slack_bot.delivery as delivery_mod
    with open(delivery_mod.__file__) as f:
        source = f.read()
    assert "anthropic" not in source
    assert "openai" not in source
    assert "zhipuai" not in source


def test_delivery_module_does_not_import_brightbean_analytics():
    import apps.slack_bot.delivery as delivery_mod
    with open(delivery_mod.__file__) as f:
        source = f.read()
    assert "apps.analytics" not in source
    assert "AnalyticsService" not in source
