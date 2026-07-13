"""Slack Events API endpoint.

Phase 9: signature verification, URL verification, event parsing,
deduplication, persistence, and background-task enqueue for new events.
"""

from __future__ import annotations

import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .constants import (
    ERROR_INVALID_JSON,
    ERROR_INVALID_SIGNATURE,
    RESPONSE_DUPLICATE,
    RESPONSE_IGNORED,
    RESPONSE_RECEIVED,
)
from .events import (
    extract_persistence_fields,
    get_url_verification_challenge,
    is_url_verification,
    parse_slack_payload,
    should_accept_event,
)
from .models import SlackInboundEvent
from .signing import verify_slack_request
from .tasks import enqueue_inbound_event

logger = logging.getLogger(__name__)


@csrf_exempt
def slack_events(request):
    """Handle incoming Slack Events API requests.

    Flow:
    1. Read raw body.
    2. Verify Slack signature + timestamp → 401 if invalid.
    3. Parse JSON → 400 if invalid.
    4. URL verification → return challenge.
    5. Unsupported/ignored event → 200 with status=ignored.
    6. Accepted event → persist via dedup helper → enqueue if new → 200.
    """
    raw_body = request.body

    # --- 1. Signature verification ---
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not verify_slack_request(raw_body, timestamp, signature):
        logger.warning("Slack request rejected: invalid signature")
        return JsonResponse(
            {"ok": False, "error": ERROR_INVALID_SIGNATURE},
            status=401,
        )

    # --- 2. Parse JSON ---
    try:
        payload = parse_slack_payload(raw_body)
    except Exception:
        logger.warning("Slack request rejected: invalid JSON body")
        return JsonResponse(
            {"ok": False, "error": ERROR_INVALID_JSON},
            status=400,
        )

    # --- 3. URL verification ---
    if is_url_verification(payload):
        try:
            challenge = get_url_verification_challenge(payload)
        except Exception:
            return JsonResponse(
                {"ok": False, "error": ERROR_INVALID_JSON},
                status=400,
            )
        return JsonResponse({"challenge": challenge})

    # --- 4. Event filtering ---
    accepted, reason = should_accept_event(payload)
    if not accepted:
        logger.info("Slack event ignored: reason=%s", reason)
        return JsonResponse(
            {"ok": True, "status": RESPONSE_IGNORED, "reason": reason},
            status=200,
        )

    # --- 5. Persist accepted event ---
    fields = extract_persistence_fields(payload)
    if fields is None:
        logger.info("Slack event ignored: missing required fields")
        return JsonResponse(
            {"ok": True, "status": RESPONSE_IGNORED, "reason": "missing_required_fields"},
            status=200,
        )

    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id=fields["event_id"],
        team_id=fields["team_id"],
        channel_id=fields["channel_id"],
        user_id=fields["user_id"],
        event_ts=fields["event_ts"],
        message_text=fields["message_text"],
        thread_ts=fields["thread_ts"],
    )

    if created:
        logger.info(
            "Slack event received: event_id=%s team_id=%s channel_id=%s",
            event.event_id, event.team_id, event.channel_id,
        )
        enqueue_inbound_event(event.event_id)
        logger.info("Slack event enqueued: event_id=%s", event.event_id)
        return JsonResponse(
            {"ok": True, "status": RESPONSE_RECEIVED},
            status=200,
        )

    logger.info("Slack event duplicate: event_id=%s", event.event_id)
    return JsonResponse(
        {"ok": True, "status": RESPONSE_DUPLICATE},
        status=200,
    )
