"""Slack ``chat.postMessage`` delivery adapter.

Sends bot responses back to Slack using the Web API
``chat.postMessage`` method with ``SLACK_BOT_TOKEN``.

Uses ``httpx`` (already a project dependency) for HTTP calls.
All network calls are injectable via ``http_post`` for testing.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings

if TYPE_CHECKING:
    import httpx

from .exceptions import SlackDeliveryError

logger = logging.getLogger(__name__)

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackDeliveryResult:
    """Outcome of a Slack message delivery attempt."""

    ok: bool
    channel_id: str
    response_ts: str = ""
    error: str = ""
    raw_response: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------

def get_slack_bot_token() -> str:
    """Return the Slack bot token from settings or environment.

    Prefers ``settings.SLACK_BOT_TOKEN``, falls back to
    ``os.environ.get("SLACK_BOT_TOKEN", "")``.
    """
    return getattr(
        settings, "SLACK_BOT_TOKEN", os.environ.get("SLACK_BOT_TOKEN", "")
    )


# ---------------------------------------------------------------------------
# Default HTTP post function (httpx)
# ---------------------------------------------------------------------------

def _default_http_post(url: str, *, json_body: dict, headers: dict) -> httpx.Response:
    """Default HTTP POST using httpx.  Imported lazily to avoid import-time issues in tests."""
    import httpx

    return httpx.post(url, json=json_body, headers=headers, timeout=30)


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def send_slack_message(
    channel_id: str,
    text: str,
    thread_ts: str = "",
    token: str | None = None,
    http_post: Callable | None = None,
) -> SlackDeliveryResult:
    """Send a message to a Slack channel via ``chat.postMessage``.

    Args:
        channel_id: Target Slack channel ID.
        text: Message text to send.
        thread_ts: Thread timestamp for threaded replies (optional).
        token: Slack bot token.  If ``None``, reads from settings/env.
        http_post: Injectable HTTP POST callable for testing.
            Signature: ``http_post(url, json_body=..., headers=...) -> response``
            The response must have ``.status_code`` (int) and ``.text`` (str).

    Returns:
        ``SlackDeliveryResult`` with ``ok=True`` on success.

    Raises:
        ``SlackDeliveryError`` only from ``deliver_slack_response``.
        This function returns controlled failures instead of raising.
    """
    # --- Validate inputs ---
    if not channel_id:
        return SlackDeliveryResult(
            ok=False, channel_id="", error="Missing channel_id"
        )

    if not text:
        return SlackDeliveryResult(
            ok=False, channel_id=channel_id, error="Missing text"
        )

    # --- Get token ---
    bot_token = token if token is not None else get_slack_bot_token()
    if not bot_token:
        return SlackDeliveryResult(
            ok=False, channel_id=channel_id, error="Missing SLACK_BOT_TOKEN"
        )

    # --- Build request ---
    body: dict[str, Any] = {
        "channel": channel_id,
        "text": text,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts

    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    post_fn = http_post if http_post is not None else _default_http_post

    # --- Make HTTP call ---
    try:
        response = post_fn(
            SLACK_POST_MESSAGE_URL,
            json_body=body,
            headers=headers,
        )
    except Exception as exc:
        logger.error("Slack HTTP request failed: %s", exc)
        return SlackDeliveryResult(
            ok=False, channel_id=channel_id, error=f"HTTP error: {exc}"
        )

    # --- Parse response ---
    try:
        raw = json.loads(response.text)
    except (ValueError, AttributeError) as exc:
        return SlackDeliveryResult(
            ok=False,
            channel_id=channel_id,
            error=f"Invalid JSON response: {exc}",
        )

    if not isinstance(raw, dict):
        return SlackDeliveryResult(
            ok=False,
            channel_id=channel_id,
            error="Slack response is not a JSON object",
        )

    if raw.get("ok"):
        return SlackDeliveryResult(
            ok=True,
            channel_id=channel_id,
            response_ts=str(raw.get("ts", "")),
            raw_response=raw,
        )

    return SlackDeliveryResult(
        ok=False,
        channel_id=channel_id,
        error=str(raw.get("error", "unknown_slack_error")),
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Delivery callback for Phase 5 processing pipeline
# ---------------------------------------------------------------------------

def deliver_slack_response(
    channel_id: str,
    text: str,
    thread_ts: str = "",
    event=None,
    response=None,
) -> str:
    """Delivery callback matching the signature expected by ``process_inbound_event``.

    Calls ``send_slack_message`` and returns ``response_ts`` on success.
    Raises ``SlackDeliveryError`` on failure so the processing pipeline
    can mark the event as ``FAILED``.
    """
    result = send_slack_message(
        channel_id=channel_id,
        text=text,
        thread_ts=thread_ts,
    )

    if not result.ok:
        raise SlackDeliveryError(result.error)

    return result.response_ts
