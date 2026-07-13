"""Slack Events API parsing and filtering.

Parses incoming event payloads, filters by supported event types,
and extracts fields needed for persistence via the Phase 4 model.
"""

from __future__ import annotations

import json
from typing import Optional

from .constants import (
    REASON_ACCEPTED,
    REASON_BOT_MESSAGE,
    REASON_IGNORED_SUBTYPE,
    REASON_MESSAGE_WITHOUT_THREAD,
    REASON_MISSING_EVENT_ID,
    REASON_MISSING_REQUIRED_FIELDS,
    REASON_UNSUPPORTED_TYPE,
    REASON_URL_VERIFICATION,
    SLACK_EVENT_APP_MENTION,
    SLACK_EVENT_MESSAGE,
    SLACK_EVENT_TYPE_EVENT_CALLBACK,
    SLACK_EVENT_TYPE_URL_VERIFICATION,
    SUPPORTED_EVENT_TYPES,
)
from .exceptions import SlackEventParseError


def parse_slack_payload(raw_body: bytes) -> dict:
    """Parse raw request body bytes into a dict.

    Raises ``SlackEventParseError`` if the body is not valid JSON.
    """
    try:
        text = raw_body.decode("utf-8")
        return json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SlackEventParseError("Invalid JSON body") from exc


def is_url_verification(payload: dict) -> bool:
    """Return ``True`` if the payload is a Slack URL verification request."""
    return payload.get("type") == SLACK_EVENT_TYPE_URL_VERIFICATION


def get_url_verification_challenge(payload: dict) -> str:
    """Extract the ``challenge`` string from a URL verification payload.

    Raises ``SlackEventParseError`` if ``challenge`` is missing.
    """
    challenge = payload.get("challenge")
    if not challenge:
        raise SlackEventParseError("Missing challenge in url_verification payload")
    return str(challenge)


def extract_event_payload(payload: dict) -> Optional[dict]:
    """Return the inner ``event`` dict from an ``event_callback`` payload.

    Returns ``None`` if the payload is not an event callback or has no
    ``event`` key.
    """
    if payload.get("type") != SLACK_EVENT_TYPE_EVENT_CALLBACK:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    return event


def should_accept_event(payload: dict) -> tuple[bool, str]:
    """Determine whether an event callback payload should be accepted.

    Returns ``(True, REASON_ACCEPTED)`` for events that should be
    persisted, or ``(False, reason)`` for events that should be ignored.

    Acceptance rules:
    - Must be an ``event_callback`` with an ``event`` dict.
    - Must have a top-level ``event_id``.
    - Event ``type`` must be in ``SUPPORTED_EVENT_TYPES``.
    - ``app_mention`` events are always accepted.
    - ``message`` events are accepted only if they have ``thread_ts``,
      no ``bot_id``, and no ``subtype``.
    - Bot messages (``bot_id`` present) are rejected.
    - Events with ``subtype`` are rejected.
    """
    event = extract_event_payload(payload)
    if event is None:
        return False, REASON_UNSUPPORTED_TYPE

    # Must have event_id at the top level
    event_id = payload.get("event_id")
    if not event_id:
        return False, REASON_MISSING_EVENT_ID

    event_type = event.get("type", "")

    # Reject bot messages
    if event.get("bot_id"):
        return False, REASON_BOT_MESSAGE

    # Reject events with subtype (edits, deletions, joins, etc.)
    if event.get("subtype"):
        return False, REASON_IGNORED_SUBTYPE

    # Must be a supported event type
    if event_type not in SUPPORTED_EVENT_TYPES:
        return False, REASON_UNSUPPORTED_TYPE

    if event_type == SLACK_EVENT_APP_MENTION:
        # app_mention events are always accepted
        return True, REASON_ACCEPTED

    if event_type == SLACK_EVENT_MESSAGE:
        # Message events must have thread_ts for now
        if not event.get("thread_ts"):
            return False, REASON_MESSAGE_WITHOUT_THREAD
        return True, REASON_ACCEPTED

    return False, REASON_UNSUPPORTED_TYPE


def extract_persistence_fields(payload: dict) -> Optional[dict]:
    """Extract fields needed for ``SlackInboundEvent`` persistence.

    Returns a dict with keys: ``event_id``, ``team_id``, ``channel_id``,
    ``user_id``, ``event_ts``, ``message_text``, ``thread_ts``.

    Returns ``None`` if required fields are missing.
    """
    event = extract_event_payload(payload)
    if event is None:
        return None

    event_id = payload.get("event_id")
    team_id = payload.get("team_id")
    channel_id = event.get("channel")
    user_id = event.get("user")
    event_ts = event.get("ts") or event.get("event_ts")

    if not all([event_id, team_id, channel_id, user_id, event_ts]):
        return None

    return {
        "event_id": str(event_id),
        "team_id": str(team_id),
        "channel_id": str(channel_id),
        "user_id": str(user_id),
        "event_ts": str(event_ts),
        "message_text": event.get("text", ""),
        "thread_ts": event.get("thread_ts") or "",
    }
