"""Phase 4 tests — unauthorized access notifications.

Tests cover:
1.  Unregistered direct mention is blocked.
2.  Unregistered valid thread reply is blocked.
3.  Unregistered mention sends the user DM.
4.  Unregistered thread reply sends the user DM.
5.  Generic originating-channel/thread response is sent.
6.  No eyes reaction is added.
7.  No analytics task is enqueued.
8.  LLM, BrightBean and analytics tools are not called.
9.  UnauthorizedAccessAttempt row is created.
10. attempt_count increments on repeated attempts.
11. first_attempt_at is preserved.
12. last_attempt_at is updated.
13. source channel ID and message timestamp are stored.
14. First attempt sends administrator DM.
15. Administrator DM contains the unregistered Member ID.
16. Administrator DM contains the source channel ID.
17. Second attempt within 24 hours suppresses administrator DM.
18. Attempt after 24 hours sends administrator DM again.
19. Different users have separate 24-hour cooldowns.
20. Same user across different channels shares one cooldown.
21. Successful administrator DM updates last_admin_notification_at.
22. Failed administrator DM does not update last_admin_notification_at.
23. Missing active administrator does not crash.
24. User DM failure does not allow access.
25. Duplicate Slack event does not duplicate attempt processing.
26. Revoked user remains blocked but does not enter the new unregistered notification flow.
27. Approved-user mention still follows the existing normal flow.
28. Approved thread reply still follows the existing normal flow.
29. Admin DM grant handling from Phase 3 remains unchanged.
30. No markdown report is created.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ADMIN_NOTIFICATION_SENT,
    AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
    AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.delivery import SlackDeliveryResult
from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    SlackInboundEvent,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.tests.conftest import signed_slack_headers
from apps.slack_bot.unauthorized_notification_service import (
    GENERIC_CHANNEL_RESPONSE,
    UnauthorizedNotificationResult,
    classify_user,
    format_admin_notification_dm,
    handle_unauthorized_access,
    record_unauthorized_attempt,
)

SECRET = "test_secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(client, body_dict, secret=SECRET, timestamp=None):
    raw = json.dumps(body_dict).encode("utf-8")
    headers = signed_slack_headers(raw, secret=secret, timestamp=timestamp)
    from django.urls import reverse
    url = reverse("slack_bot:events")
    return client.post(url, data=raw, content_type="application/json", **headers)


def _mention_payload(
    event_id="Ev_mention_1",
    team_id="TTEST123",
    user_id="UUNREG123",
    channel_id="C08XYZ456",
    text="<@B123> show analytics",
    ts="1720000000.000100",
):
    return {
        "team_id": team_id,
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": user_id,
            "text": text,
            "ts": ts,
            "channel": channel_id,
        },
    }


def _thread_reply_payload(
    event_id="Ev_thread_1",
    team_id="TTEST123",
    user_id="UUNREG123",
    channel_id="C08XYZ456",
    thread_ts="1720000000.000050",
    ts="1720000000.000200",
    text="follow up question",
):
    return {
        "team_id": team_id,
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": user_id,
            "text": text,
            "ts": ts,
            "channel": channel_id,
            "thread_ts": thread_ts,
        },
    }


def _dm_payload(
    event_id="Ev_dm_1",
    team_id="TTEST123",
    user_id="UADMIN123",
    channel_id="D123",
    text="Give U08ABC123 access",
    ts="1720000000.000100",
):
    return {
        "team_id": team_id,
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": user_id,
            "text": text,
            "ts": ts,
            "channel": channel_id,
        },
    }


def _create_admin(workspace_id="TTEST123", slack_user_id="UADMIN123", status=ADMIN_STATUS_ACTIVE):
    return BotAdministrator.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=status,
    )


def _create_approved_user(workspace_id="TTEST123", slack_user_id="UUSER123"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    )


def _create_revoked_user(workspace_id="TTEST123", slack_user_id="UREVOKED123"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )


def _ok_result(channel_id="C", response_ts="ts"):
    return SlackDeliveryResult(ok=True, channel_id=channel_id, response_ts=response_ts)


def _fail_result(channel_id="C", error="fail"):
    return SlackDeliveryResult(ok=False, channel_id=channel_id, error=error)


# ===========================================================================
# classify_user — unit tests
# ===========================================================================


@pytest.mark.django_db
def test_classify_user_unregistered():
    is_unreg, is_rev = classify_user("TTEST123", "UUNREG123")
    assert is_unreg is True
    assert is_rev is False


@pytest.mark.django_db
def test_classify_user_revoked():
    _create_revoked_user("TTEST123", "UREVOKED123")
    is_unreg, is_rev = classify_user("TTEST123", "UREVOKED123")
    assert is_unreg is False
    assert is_rev is True


@pytest.mark.django_db
def test_classify_user_approved():
    _create_approved_user("TTEST123", "UUSER123")
    is_unreg, is_rev = classify_user("TTEST123", "UUSER123")
    assert is_unreg is False
    assert is_rev is False


# ===========================================================================
# record_unauthorized_attempt — unit tests
# ===========================================================================


@pytest.mark.django_db
def test_record_attempt_creates_row():
    attempt = record_unauthorized_attempt(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    assert attempt.attempt_count == 1
    assert attempt.first_attempt_at is not None
    assert attempt.last_attempt_at is not None
    assert attempt.last_source_channel_id == "C08XYZ456"
    assert attempt.last_message_ts == "1720000000.000100"


@pytest.mark.django_db
def test_record_attempt_increments_count():
    record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C1", message_ts="ts1",
    )
    attempt = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C2", message_ts="ts2",
    )
    assert attempt.attempt_count == 2


@pytest.mark.django_db
def test_record_attempt_preserves_first_attempt_at():
    a1 = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C1", message_ts="ts1",
    )
    first = a1.first_attempt_at
    a2 = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C2", message_ts="ts2",
    )
    assert a2.first_attempt_at == first


@pytest.mark.django_db
def test_record_attempt_updates_last_attempt_at():
    a1 = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C1", message_ts="ts1",
    )
    import time
    time.sleep(0.01)
    a2 = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C2", message_ts="ts2",
    )
    assert a2.last_attempt_at > a1.last_attempt_at


@pytest.mark.django_db
def test_record_attempt_stores_channel_and_ts():
    record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456", message_ts="123.456",
    )
    attempt = record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C08NEW789", message_ts="789.012",
    )
    assert attempt.last_source_channel_id == "C08NEW789"
    assert attempt.last_message_ts == "789.012"


@pytest.mark.django_db
def test_record_attempt_one_row_per_user():
    record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C1", message_ts="ts1",
    )
    record_unauthorized_attempt(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
        source_channel_id="C2", message_ts="ts2",
    )
    assert UnauthorizedAccessAttempt.objects.filter(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    ).count() == 1


# ===========================================================================
# handle_unauthorized_access — service tests
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_handle_unauthorized_sends_user_dm(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    assert result.user_dm_sent is True
    # First call should be the user DM (channel = user_id)
    call_args = mock_send.call_args_list[0]
    assert call_args.kwargs["channel_id"] == "UUNREG123"
    assert "do not currently have access" in call_args.kwargs["text"]


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_handle_unauthorized_sends_channel_response(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
        thread_ts="1720000000.000050",
    )
    assert result.channel_response_sent is True
    # Second call should be the channel response
    call_args = mock_send.call_args_list[1]
    assert call_args.kwargs["channel_id"] == "C08XYZ456"
    assert call_args.kwargs["text"] == GENERIC_CHANNEL_RESPONSE
    assert call_args.kwargs["thread_ts"] == "1720000000.000050"


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_handle_unauthorized_creates_attempt_row(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    assert UnauthorizedAccessAttempt.objects.filter(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_handle_unauthorized_writes_audit(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    assert BotAccessAuditLog.objects.filter(
        workspace_id="TTEST123",
        target_slack_user_id="UUNREG123",
        action=AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_first_attempt_sends_admin_dm(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    assert result.admin_dm_sent is True
    assert result.admin_dm_suppressed is False
    # Third call should be the admin DM (channel = admin user_id)
    call_args = mock_send.call_args_list[2]
    assert call_args.kwargs["channel_id"] == "UADMIN123"


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_admin_dm_contains_member_id(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="U08ABC123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    admin_call = mock_send.call_args_list[2]
    assert "U08ABC123" in admin_call.kwargs["text"]


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_admin_dm_contains_source_channel_id(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="U08ABC123",
        source_channel_id="C08XYZ456",
        message_ts="1720000000.000100",
    )
    admin_call = mock_send.call_args_list[2]
    assert "C08XYZ456" in admin_call.kwargs["text"]


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_second_attempt_within_24h_suppresses_admin_dm(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # First attempt
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="ts1",
    )
    # Second attempt — should suppress admin DM
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="ts2",
    )
    assert result.admin_dm_sent is False
    assert result.admin_dm_suppressed is True
    assert result.attempt_count == 2


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_attempt_after_24h_sends_admin_dm_again(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # First attempt
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="ts1",
    )
    # Manually push last_admin_notification_at back 25 hours
    attempt = UnauthorizedAccessAttempt.objects.get(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    )
    attempt.last_admin_notification_at = timezone.now() - timedelta(hours=25)
    attempt.save(update_fields=["last_admin_notification_at"])

    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C08XYZ456",
        message_ts="ts2",
    )
    assert result.admin_dm_sent is True
    assert result.attempt_count == 2


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_different_users_separate_cooldowns(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # User 1
    r1 = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG001",
        source_channel_id="C1",
        message_ts="ts1",
    )
    # User 2 — should also get admin DM (independent cooldown)
    r2 = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG002",
        source_channel_id="C1",
        message_ts="ts2",
    )
    assert r1.admin_dm_sent is True
    assert r2.admin_dm_sent is True


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_same_user_different_channels_shares_cooldown(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # First attempt from channel C1
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    # Second attempt from channel C2 — same user, should suppress
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C2",
        message_ts="ts2",
    )
    assert result.admin_dm_suppressed is True


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_successful_admin_dm_updates_timestamp(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    attempt = UnauthorizedAccessAttempt.objects.get(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    )
    assert attempt.last_admin_notification_at is not None


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_failed_admin_dm_does_not_update_timestamp(mock_send):
    """Admin DM fails (3rd call), user/channel DMs succeed (1st/2nd calls)."""
    mock_send.side_effect = [
        _ok_result(),    # user DM
        _ok_result(),    # channel response
        _fail_result(),  # admin DM fails
    ]
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    assert result.admin_dm_sent is False
    attempt = UnauthorizedAccessAttempt.objects.get(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    )
    assert attempt.last_admin_notification_at is None


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_missing_admin_does_not_crash(mock_send):
    mock_send.return_value = _ok_result()
    # No admin created
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    assert result.admin_not_configured is True
    assert result.handled is True


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_user_dm_failure_does_not_grant_access(mock_send):
    """User DM fails, but admin notification still proceeds."""
    mock_send.side_effect = [
        _fail_result(),   # user DM fails
        _ok_result(),     # channel response
        _ok_result(),     # admin DM
    ]
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    assert result.user_dm_sent is False
    assert result.admin_dm_sent is True
    # No access granted
    assert not BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_suppressed_writes_audit(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # First attempt
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    # Second attempt — suppressed
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts2",
    )
    assert BotAccessAuditLog.objects.filter(
        workspace_id="TTEST123",
        target_slack_user_id="UUNREG123",
        action=AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_sent_writes_audit(mock_send):
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    assert BotAccessAuditLog.objects.filter(
        workspace_id="TTEST123",
        target_slack_user_id="UUNREG123",
        action=AUDIT_ADMIN_NOTIFICATION_SENT,
    ).exists()


# ===========================================================================
# format_admin_notification_dm
# ===========================================================================


def test_format_admin_dm_contains_member_id():
    text = format_admin_notification_dm(
        slack_user_id="U08ABC123",
        source_channel_id="C08XYZ456",
        attempted_at=timezone.now(),
    )
    assert "U08ABC123" in text
    assert "C08XYZ456" in text
    assert "Give U08ABC123 access" in text


# ===========================================================================
# Views integration — unregistered mention
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_blocked(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_no_eyes_reaction(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload())
    mock_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_no_enqueue(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload())
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_sends_user_dm(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload(user_id="UUNREG123"))
    # First call to notification service send_slack_message is the user DM
    user_dm_call = mock_send_notif.call_args_list[0]
    assert user_dm_call.kwargs["channel_id"] == "UUNREG123"
    assert "do not currently have access" in user_dm_call.kwargs["text"]


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_sends_channel_response(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload(channel_id="C08XYZ456"))
    # Second call is the channel response
    channel_call = mock_send_notif.call_args_list[1]
    assert channel_call.kwargs["channel_id"] == "C08XYZ456"
    assert channel_call.kwargs["text"] == GENERIC_CHANNEL_RESPONSE


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_creates_attempt(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload(user_id="UUNREG123", channel_id="C08XYZ456"))
    attempt = UnauthorizedAccessAttempt.objects.get(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    )
    assert attempt.attempt_count == 1
    assert attempt.last_source_channel_id == "C08XYZ456"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_mention_sends_admin_dm(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _mention_payload(user_id="U08ABC123", channel_id="C08XYZ456"))
    # Third call is the admin DM
    admin_call = mock_send_notif.call_args_list[2]
    assert admin_call.kwargs["channel_id"] == "UADMIN123"
    assert "U08ABC123" in admin_call.kwargs["text"]
    assert "C08XYZ456" in admin_call.kwargs["text"]


# ===========================================================================
# Views integration — unregistered thread reply
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_thread_reply_blocked(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    # Create a known bot thread
    SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id="C08XYZ456",
        user_id="UBOT",
        event_ts="1720000000.000050",
        message_text="bot response",
        thread_ts="",
        status="RESPONDED",
        response_ts="1720000000.000050",
    )
    client = Client()
    response = _post(client, _thread_reply_payload())
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_thread_reply_sends_user_dm(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id="C08XYZ456",
        user_id="UBOT",
        event_ts="1720000000.000050",
        message_text="bot response",
        thread_ts="",
        status="RESPONDED",
        response_ts="1720000000.000050",
    )
    client = Client()
    _post(client, _thread_reply_payload(user_id="UUNREG123"))
    user_dm_call = mock_send_notif.call_args_list[0]
    assert user_dm_call.kwargs["channel_id"] == "UUNREG123"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_unregistered_thread_reply_channel_response_in_thread(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id="C08XYZ456",
        user_id="UBOT",
        event_ts="1720000000.000050",
        message_text="bot response",
        thread_ts="",
        status="RESPONDED",
        response_ts="1720000000.000050",
    )
    client = Client()
    _post(client, _thread_reply_payload())
    channel_call = mock_send_notif.call_args_list[1]
    assert channel_call.kwargs["channel_id"] == "C08XYZ456"
    assert channel_call.kwargs["thread_ts"] == "1720000000.000050"


# ===========================================================================
# Views integration — duplicate event
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_duplicate_event_does_not_duplicate_attempt(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    payload = _mention_payload(user_id="UUNREG123")
    _post(client, payload)
    _post(client, payload)  # duplicate event_id
    assert UnauthorizedAccessAttempt.objects.filter(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    ).count() == 1
    attempt = UnauthorizedAccessAttempt.objects.get(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    )
    assert attempt.attempt_count == 1


# ===========================================================================
# Views integration — revoked user
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_revoked_user_blocked_no_notification_flow(mock_send_notif, mock_send_view, mock_reaction, mock_enqueue):
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    _create_revoked_user("TTEST123", "UREVOKED123")
    client = Client()
    response = _post(client, _mention_payload(user_id="UREVOKED123"))
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()
    # No notification calls — revoked users don't enter Phase 4
    mock_send_notif.assert_not_called()
    # No attempt record
    assert not UnauthorizedAccessAttempt.objects.filter(
        workspace_id="TTEST123", slack_user_id="UREVOKED123",
    ).exists()


# ===========================================================================
# Views integration — approved user (unchanged)
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_approved_mention_normal_flow(mock_send_notif, mock_reaction, mock_enqueue):
    mock_add_reaction_result = MagicMock(ok=True)
    mock_reaction.return_value = mock_add_reaction_result
    mock_send_notif.return_value = _ok_result()
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload(user_id="UUSER123"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()
    mock_send_notif.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_approved_thread_reply_normal_flow(mock_send_notif, mock_reaction, mock_enqueue):
    mock_reaction.return_value = MagicMock(ok=True)
    mock_send_notif.return_value = _ok_result()
    _create_approved_user("TTEST123", "UUSER123")
    SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id="C08XYZ456",
        user_id="UBOT",
        event_ts="1720000000.000050",
        message_text="bot response",
        thread_ts="",
        status="RESPONDED",
        response_ts="1720000000.000050",
    )
    client = Client()
    response = _post(client, _thread_reply_payload(user_id="UUSER123"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()
    mock_send_notif.assert_not_called()


# ===========================================================================
# Views integration — Phase 3 admin DM unchanged
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_admin_dm_grant_still_works(mock_send_notif, mock_send_view, mock_conf, mock_grant):
    from apps.slack_bot.bot_grant_service import BotGrantResult
    mock_send_view.return_value = MagicMock(ok=True, response_ts="ts")
    mock_send_notif.return_value = _ok_result()
    mock_conf.return_value = True

    def _grant_effect(*, team_id, target_slack_user_id, approving_slack_user_id):
        BotUserAccess.objects.create(
            workspace_id=team_id, slack_user_id=target_slack_user_id,
            status="APPROVED", permission="READ_ONLY",
            granted_by_slack_user_id=approving_slack_user_id,
        )
        return BotGrantResult(
            action="granted",
            target_slack_user_id=target_slack_user_id,
            workspace_name="Test WS",
        )
    mock_grant.side_effect = _grant_effect

    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
        status="APPROVED",
    ).exists()
    # Phase 4 notification service should not be called
    mock_send_notif.assert_not_called()


# ===========================================================================
# No markdown report
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
def test_no_markdown_report_created(mock_send):
    """Verify the service returns a result dataclass, not a markdown report."""
    mock_send.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    result = handle_unauthorized_access(
        workspace_id="TTEST123",
        slack_user_id="UUNREG123",
        source_channel_id="C1",
        message_ts="ts1",
    )
    assert isinstance(result, UnauthorizedNotificationResult)
    assert not isinstance(result, str)
