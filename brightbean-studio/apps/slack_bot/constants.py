"""Shared constants for the Slack analytics bot."""

import os

# ---------------------------------------------------------------------------
# SlackInboundEvent status values (Phase 4)
# ---------------------------------------------------------------------------

STATUS_RECEIVED = "RECEIVED"
STATUS_PROCESSING = "PROCESSING"
STATUS_RESPONDED = "RESPONDED"
STATUS_FAILED = "FAILED"
STATUS_IGNORED = "IGNORED"

STATUS_CHOICES = (
    (STATUS_RECEIVED, "Received"),
    (STATUS_PROCESSING, "Processing"),
    (STATUS_RESPONDED, "Responded"),
    (STATUS_FAILED, "Failed"),
    (STATUS_IGNORED, "Ignored"),
)

# ---------------------------------------------------------------------------
# Slack event type constants (Phase 3)
# ---------------------------------------------------------------------------

SLACK_EVENT_TYPE_URL_VERIFICATION = "url_verification"
SLACK_EVENT_TYPE_EVENT_CALLBACK = "event_callback"
SLACK_EVENT_APP_MENTION = "app_mention"
SLACK_EVENT_MESSAGE = "message"

# ---------------------------------------------------------------------------
# Error / response constants (Phase 3)
# ---------------------------------------------------------------------------

ERROR_INVALID_SIGNATURE = "invalid_signature"
ERROR_INVALID_JSON = "invalid_json"

# Response statuses (lowercase — distinct from model STATUS_* uppercase values)
RESPONSE_RECEIVED = "received"
RESPONSE_DUPLICATE = "duplicate"
RESPONSE_IGNORED = "ignored"

# ---------------------------------------------------------------------------
# Event accept/reject reason strings (Phase 3)
# ---------------------------------------------------------------------------

REASON_ACCEPTED = "accepted"
REASON_URL_VERIFICATION = "url_verification"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_BOT_MESSAGE = "bot_message"
REASON_MISSING_EVENT_ID = "missing_event_id"
REASON_MISSING_REQUIRED_FIELDS = "missing_required_fields"
REASON_MESSAGE_WITHOUT_THREAD = "message_without_thread"
REASON_IGNORED_SUBTYPE = "ignored_subtype"
REASON_NOT_BOT_THREAD = "not_bot_thread"

# Supported event types for trigger filtering
SUPPORTED_EVENT_TYPES = frozenset({
    SLACK_EVENT_APP_MENTION,
    SLACK_EVENT_MESSAGE,
})

# ---------------------------------------------------------------------------
# Replay window default (seconds)
# ---------------------------------------------------------------------------

DEFAULT_REPLAY_WINDOW_SECONDS = int(
    os.environ.get("SLACK_EVENT_REPLAY_WINDOW_SECONDS", 300)
)

# ---------------------------------------------------------------------------
# Routing response types (Phase 7)
# ---------------------------------------------------------------------------

RESPONSE_TYPE_GREETING = "greeting"
RESPONSE_TYPE_HELP = "help"
RESPONSE_TYPE_STATUS = "status"
RESPONSE_TYPE_ANALYTICS_PLACEHOLDER = "analytics_placeholder"
RESPONSE_TYPE_UNSUPPORTED = "unsupported"
RESPONSE_TYPE_ERROR = "error"
RESPONSE_TYPE_NO_RESPONSE = "no_response"
