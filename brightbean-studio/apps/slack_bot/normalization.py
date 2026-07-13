"""Slack message normalization.

Converts a persisted ``SlackInboundEvent`` into a clean
``SlackAnalyticsRequest`` (defined in :mod:`apps.slack_bot.contracts`)
suitable for downstream routing, background processing, and
LLM/analytics integration.
"""

from __future__ import annotations

import re

from .contracts import SlackAnalyticsRequest
from .exceptions import SlackNormalizationError

# Regex for Slack user mentions: <@U123> or <@U123|displayname>
_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")

# Regex for collapsing repeated whitespace
_MULTI_WS_RE = re.compile(r"\s+")


def remove_bot_mentions(text: str) -> str:
    """Remove Slack user mention patterns from text.

    Handles both ``<@USERID>`` and ``<@USERID|name>`` forms.
    Does not remove normal words that merely contain ``@``.
    """
    return _MENTION_RE.sub("", text)


def clean_slack_text(text: str) -> str:
    """Clean Slack message text for downstream processing.

    - Removes bot mentions
    - Strips leading/trailing whitespace
    - Collapses repeated whitespace into single spaces
    - Preserves meaningful punctuation inside text
    """
    without_mentions = remove_bot_mentions(text)
    collapsed = _MULTI_WS_RE.sub(" ", without_mentions)
    return collapsed.strip()


def is_meaningful_message(text: str) -> bool:
    """Return ``True`` if *text* has content beyond whitespace/punctuation.

    Returns ``False`` for:
    - empty strings
    - whitespace-only strings
    - punctuation-only strings

    Returns ``True`` for strings containing at least one
    alphanumeric character.
    """
    return bool(re.search(r"[A-Za-z0-9]", text))


def normalize_inbound_event(event) -> SlackAnalyticsRequest:
    """Convert a ``SlackInboundEvent`` into a ``SlackAnalyticsRequest``.

    Steps:
    1. Clean the message text (remove mentions, collapse whitespace).
    2. Reject empty or punctuation-only messages.
    3. Fall back to ``event_ts`` when ``thread_ts`` is empty.

    Raises ``SlackNormalizationError`` for messages that are empty
    or punctuation-only after cleaning.
    """
    cleaned = clean_slack_text(event.message_text)

    if not is_meaningful_message(cleaned):
        raise SlackNormalizationError(
            f"Message text is empty or punctuation-only after normalization "
            f"(event_id={event.event_id})"
        )

    thread_ts = event.thread_ts if event.thread_ts else event.event_ts

    return SlackAnalyticsRequest(
        correlation_id=event.correlation_id,
        event_id=event.event_id,
        team_id=event.team_id,
        channel_id=event.channel_id,
        user_id=event.user_id,
        thread_ts=thread_ts,
        text=cleaned,
    )
