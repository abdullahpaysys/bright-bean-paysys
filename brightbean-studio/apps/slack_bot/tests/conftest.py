"""Shared test helpers for Slack bot tests."""

from __future__ import annotations

import time

from apps.slack_bot.signing import build_slack_signature


def signed_slack_headers(
    body: bytes,
    secret: str = "test_secret",
    timestamp: str | None = None,
) -> dict:
    """Build Django test-client headers for a signed Slack request.

    Returns a dict suitable for ``client.post(..., **headers)``.
    """
    if timestamp is None:
        timestamp = str(int(time.time()))
    signature = build_slack_signature(secret, timestamp, body)
    return {
        "HTTP_X_SLACK_REQUEST_TIMESTAMP": timestamp,
        "HTTP_X_SLACK_SIGNATURE": signature,
    }
