"""Slack request signature and timestamp verification.

Implements HMAC-SHA256 signature verification using ``SLACK_SIGNING_SECRET``
and rejects requests outside the replay window.

Slack signs requests as::

    v0:{timestamp}:{raw_body}

and sends the result as ``X-Slack-Signature`` header (``v0=<hex>``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from django.conf import settings

from .constants import DEFAULT_REPLAY_WINDOW_SECONDS


def get_slack_signing_secret() -> str:
    """Return the Slack signing secret from settings or environment."""
    return getattr(
        settings, "SLACK_SIGNING_SECRET", os.environ.get("SLACK_SIGNING_SECRET", "")
    )


def get_replay_window_seconds() -> int:
    """Return the replay window in seconds from settings or environment."""
    return getattr(
        settings,
        "SLACK_EVENT_REPLAY_WINDOW_SECONDS",
        DEFAULT_REPLAY_WINDOW_SECONDS,
    )


def build_slack_signature(
    signing_secret: str, timestamp: str, raw_body: bytes
) -> str:
    """Build the Slack signature string ``v0=<hex_hmac_sha256>``.

    The basestring is ``v0:{timestamp}:{raw_body}`` encoded as UTF-8.
    """
    base_string = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}"
    computed = hmac.new(
        key=signing_secret.encode("utf-8"),
        msg=base_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"v0={computed}"


def verify_slack_request(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    signing_secret: str | None = None,
    replay_window_seconds: int | None = None,
    now: int | None = None,
) -> bool:
    """Verify a Slack request signature and timestamp freshness.

    Returns ``True`` only if:
    - ``signature`` and ``timestamp`` are both present
    - ``timestamp`` is within the replay window
    - The HMAC-SHA256 signature matches (constant-time comparison)

    Returns ``False`` for any failure (missing headers, stale timestamp,
    bad signature format, mismatched signature).
    """
    if not signature or not timestamp:
        return False

    # Signature must start with "v0="
    if not signature.startswith("v0="):
        return False

    # Timestamp must be an integer
    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        return False

    # Replay window check
    secret = signing_secret if signing_secret is not None else get_slack_signing_secret()
    if not secret:
        return False

    window = (
        replay_window_seconds
        if replay_window_seconds is not None
        else get_replay_window_seconds()
    )
    current_time = now if now is not None else int(time.time())

    if abs(current_time - ts_int) > window:
        return False

    # Build expected signature and compare in constant time
    expected = build_slack_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature)
