"""Simple deterministic command routing for Slack bot messages.

Receives a normalized ``SlackAnalyticsRequest`` and returns a
``SimpleBotResponse`` with a response type and text.

All current routes return ``no_response`` with empty text.
User-facing responses will be implemented during LLM + BrightBean
analytics integration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .constants import RESPONSE_TYPE_NO_RESPONSE
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
# Command keyword sets (retained for future LLM routing context)
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

    Currently all routes return ``no_response`` with empty text.
    Keyword classification helpers are retained for future LLM routing.
    """
    return SimpleBotResponse(
        response_type=RESPONSE_TYPE_NO_RESPONSE,
        text="",
    )
