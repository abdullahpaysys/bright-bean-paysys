"""Simple deterministic command routing for Slack bot messages.

Receives a normalized ``SlackAnalyticsRequest`` and returns a
``SimpleBotResponse`` with a response type and text.

Supported routes:
- greeting (hi, hello, hey, salam, …)
- help (help, what can you do, commands, examples)
- status (status, connected accounts, connections, account status)
- analytics_placeholder (everything else with meaningful text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .constants import (
    RESPONSE_TYPE_ANALYTICS_PLACEHOLDER,
    RESPONSE_TYPE_GREETING,
    RESPONSE_TYPE_HELP,
    RESPONSE_TYPE_STATUS,
)
from .normalization import SlackAnalyticsRequest

# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpleBotResponse:
    """Structured response from the routing layer."""

    response_type: str
    text: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Command keyword sets
# ---------------------------------------------------------------------------

_GREETING_KEYWORDS = frozenset({
    "hi", "hello", "hey", "salam", "assalam o alaikum",
})

_HELP_KEYWORDS = frozenset({
    "help", "what can you do", "commands", "examples",
})

_STATUS_KEYWORDS = frozenset({
    "status", "connected accounts", "connections", "account status",
})

# ---------------------------------------------------------------------------
# Response texts
# ---------------------------------------------------------------------------

_GREETING_TEXT = "Hi. Ask me about Instagram, Facebook, or LinkedIn analytics."

_HELP_TEXT = (
    "I can help with Instagram, Facebook, and LinkedIn analytics.\n\n"
    "Examples:\n"
    "- top Instagram post this week\n"
    "- compare Facebook and Instagram engagement last 30 days\n"
    "- LinkedIn follower growth this month\n"
    "- Facebook reach last 7 days"
)

_STATUS_TEXT = (
    "Status check placeholder. "
    "BrightBean account-status integration will be added later."
)

_ANALYTICS_PLACEHOLDER_TEXT = (
    "Analytics pipeline placeholder. "
    "LLM and BrightBean analytics integration will be added by the teammate branch."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_command_text(text: str) -> str:
    """Lowercase and strip trailing punctuation for command matching."""
    lowered = text.lower().strip()
    # Strip trailing punctuation for exact-match commands
    return re.sub(r"[!?.,;:]+$", "", lowered).strip()


def is_greeting(text: str) -> bool:
    """Return ``True`` if *text* matches a greeting command."""
    return normalize_command_text(text) in _GREETING_KEYWORDS


def is_help_command(text: str) -> bool:
    """Return ``True`` if *text* matches a help command."""
    return normalize_command_text(text) in _HELP_KEYWORDS


def is_status_command(text: str) -> bool:
    """Return ``True`` if *text* matches a status command."""
    return normalize_command_text(text) in _STATUS_KEYWORDS


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def route_simple_command(request: SlackAnalyticsRequest) -> SimpleBotResponse:
    """Route a normalized Slack analytics request to a simple response.

    Matching is deterministic and case-insensitive:
    1. greeting keywords → greeting response
    2. help keywords → help response
    3. status keywords → status placeholder
    4. anything else → analytics placeholder
    """
    text = request.text

    if is_greeting(text):
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_GREETING,
            text=_GREETING_TEXT,
        )

    if is_help_command(text):
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_HELP,
            text=_HELP_TEXT,
        )

    if is_status_command(text):
        return SimpleBotResponse(
            response_type=RESPONSE_TYPE_STATUS,
            text=_STATUS_TEXT,
        )

    return SimpleBotResponse(
        response_type=RESPONSE_TYPE_ANALYTICS_PLACEHOLDER,
        text=_ANALYTICS_PLACEHOLDER_TEXT,
    )
