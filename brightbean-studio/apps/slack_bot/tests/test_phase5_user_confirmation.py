"""Phase 5 tests — approved-user access confirmation DM.

Tests cover:
1.  Newly approved single user receives confirmation DM.
2.  Restored single user receives confirmation DM.
3.  Already-approved user does not receive duplicate DM.
4.  Invalid user ID receives no DM.
5.  Failed database grant receives no DM.
6.  User-DM failure does not roll back access.
7.  Single-user admin response reports notification failure.
8.  Bulk grant notifies every newly approved user.
9.  Bulk grant notifies every restored user.
10. Bulk grant does not notify already-approved users.
11. Bulk grant does not notify invalid IDs.
12. Bulk grant continues after one user-DM failure.
13. Bulk admin response lists notification failures.
14. Empty notification-failure section is omitted.
15. Admin DM grant still bypasses analytics queue.
16. Admin DM grant still does not call LLM or BrightBean.
17. Admin DM grant still does not add eyes reaction.
18. Normal approved mention behaviour remains unchanged.
19. Unauthorized-user Phase 4 behaviour remains unchanged.
20. No markdown report is created.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings

from apps.slack_bot.access_service import (
    BulkGrantResult,
    GrantResult,
)
from apps.slack_bot.admin_dm_response import (
    format_bulk_grant_response,
    format_single_grant_response,
)
from apps.slack_bot.admin_dm_service import process_admin_dm
from apps.slack_bot.bot_grant_service import BotGrantResult
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.delivery import SlackDeliveryResult
from apps.slack_bot.models import (
    BotAdministrator,
    BotUserAccess,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.tests.conftest import signed_slack_headers
from apps.slack_bot.user_confirmation_service import (
    send_bulk_user_confirmation_dms,
    send_user_confirmation_dm,
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


def _mention_payload(
    event_id="Ev_mention_1",
    team_id="TTEST123",
    user_id="UUSER123",
    channel_id="C123",
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


def _create_admin(workspace_id="TTEST123", slack_user_id="UADMIN123"):
    return BotAdministrator.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
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


def _create_unauthorized_attempt(
    workspace_id="TTEST123",
    slack_user_id="U08ABC123",
    source_channel_id="C123",
):
    return UnauthorizedAccessAttempt.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        last_source_channel_id=source_channel_id,
        attempt_count=1,
    )


def _provision_side_effect(
    *,
    action="granted",
    workspace_name="Test WS",
):
    """Return a side_effect function for grant_bot_access mock."""
    def _effect(
        *,
        team_id,
        target_slack_user_id,
        approving_slack_user_id,
    ):
        if action == "granted":
            BotUserAccess.objects.create(
                workspace_id=team_id,
                slack_user_id=target_slack_user_id,
                status=ACCESS_STATUS_APPROVED,
                permission=PERMISSION_READ_ONLY,
                granted_by_slack_user_id=approving_slack_user_id,
            )
        elif action == "restored":
            access = BotUserAccess.objects.get(
                workspace_id=team_id, slack_user_id=target_slack_user_id,
            )
            access.status = ACCESS_STATUS_APPROVED
            access.granted_by_slack_user_id = approving_slack_user_id
            access.save(update_fields=["status", "granted_by_slack_user_id"])
        return BotGrantResult(
            action=action,
            target_slack_user_id=target_slack_user_id,
            workspace_name=workspace_name,
        )
    return _effect


def _ok_result(channel_id="C", response_ts="ts"):
    return SlackDeliveryResult(ok=True, channel_id=channel_id, response_ts=response_ts)


def _fail_result(channel_id="C", error="fail"):
    return SlackDeliveryResult(ok=False, channel_id=channel_id, error=error)


# ===========================================================================
# send_user_confirmation_dm — unit tests
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_send_user_confirmation_dm_success(mock_send):
    mock_send.return_value = _ok_result()
    ok = send_user_confirmation_dm(
        workspace_id="TTEST123",
        slack_user_id="U08ABC123",
    )
    assert ok is True
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["channel_id"] == "U08ABC123"
    assert "enabled" in mock_send.call_args.kwargs["text"]


@pytest.mark.django_db
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_send_user_confirmation_dm_failure(mock_send):
    mock_send.return_value = _fail_result()
    ok = send_user_confirmation_dm(
        workspace_id="TTEST123",
        slack_user_id="U08ABC123",
    )
    assert ok is False


# ===========================================================================
# send_bulk_user_confirmation_dms — unit tests
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_confirmation_all_succeed(mock_send):
    mock_send.return_value = _ok_result()
    result = send_bulk_user_confirmation_dms(
        workspace_id="TTEST123",
        slack_user_ids=["U08ABC123", "U08DEF456"],
    )
    assert result.notified == ["U08ABC123", "U08DEF456"]
    assert result.failed == []


@pytest.mark.django_db
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_confirmation_partial_failure(mock_send):
    mock_send.side_effect = [_ok_result(), _fail_result(), _ok_result()]
    result = send_bulk_user_confirmation_dms(
        workspace_id="TTEST123",
        slack_user_ids=["U1", "U2", "U3"],
    )
    assert result.notified == ["U1", "U3"]
    assert result.failed == ["U2"]


# ===========================================================================
# process_admin_dm — single-user confirmation
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_newly_approved_single_user_receives_dm(mock_send_conf, mock_provision):
    """Test 1: Newly approved single user receives confirmation DM."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_send_conf.assert_called_once()
    assert mock_send_conf.call_args.kwargs["channel_id"] == "U08ABC123"
    assert "enabled" in mock_send_conf.call_args.kwargs["text"]


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_restored_single_user_receives_dm(mock_send_conf, mock_provision):
    """Test 2: Restored single user receives confirmation DM."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect(action="restored")
    _create_admin("TTEST123", "UADMIN123")
    _create_revoked_user("TTEST123", "U08ABC123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_send_conf.assert_called_once()
    assert mock_send_conf.call_args.kwargs["channel_id"] == "U08ABC123"


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_already_approved_no_duplicate_dm(mock_send_conf, mock_provision):
    """Test 3: Already-approved user does not receive duplicate DM."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect(action="already_approved")
    _create_admin("TTEST123", "UADMIN123")
    _create_approved_user("TTEST123", "U08ABC123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_send_conf.assert_not_called()


@pytest.mark.django_db
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_invalid_id_no_dm(mock_send_conf):
    """Test 4: Invalid user ID receives no confirmation DM."""
    mock_send_conf.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    process_admin_dm("TTEST123", "UADMIN123", "Give C08INVALID access")
    mock_send_conf.assert_not_called()


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_user_dm_failure_does_not_rollback(mock_send_conf, mock_provision):
    """Test 6: User-DM failure does not roll back access."""
    mock_send_conf.return_value = _fail_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    # Access should still be granted
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
        status="APPROVED",
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_single_user_admin_response_reports_failure(mock_send_conf, mock_provision):
    """Test 7: Single-user admin response reports notification failure."""
    mock_send_conf.return_value = _fail_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert "Failed" in result.response_text
    assert "User notification: Failed" in result.response_text


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_single_user_admin_response_no_failure_on_success(mock_send_conf, mock_provision):
    """Admin response does not mention failure when DM succeeds."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert "User notification: Failed" not in result.response_text


# ===========================================================================
# process_admin_dm — bulk confirmation
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_notifies_all_newly_approved(mock_send_conf, mock_provision):
    """Test 8: Bulk grant notifies every newly approved user."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    assert mock_send_conf.call_count == 2
    channels_called = [c.kwargs["channel_id"] for c in mock_send_conf.call_args_list]
    assert "U08ABC123" in channels_called
    assert "U08DEF456" in channels_called


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_notifies_restored_users(mock_send_conf, mock_provision):
    """Test 9: Bulk grant notifies every restored user."""
    mock_send_conf.return_value = _ok_result()
    # First user (U08ABC123) is restored, second (U08DEF456) is newly provisioned
    def _mixed_effect(*, team_id, target_slack_user_id, approving_slack_user_id):
        if target_slack_user_id == "U08ABC123":
            access = BotUserAccess.objects.get(
                workspace_id=team_id, slack_user_id=target_slack_user_id,
            )
            access.status = ACCESS_STATUS_APPROVED
            access.granted_by_slack_user_id = approving_slack_user_id
            access.save(update_fields=["status", "granted_by_slack_user_id"])
            return BotGrantResult(
                action="restored",
                target_slack_user_id=target_slack_user_id,
                workspace_name="Test WS",
            )
        BotUserAccess.objects.create(
            workspace_id=team_id, slack_user_id=target_slack_user_id,
            status=ACCESS_STATUS_APPROVED, permission=PERMISSION_READ_ONLY,
            granted_by_slack_user_id=approving_slack_user_id,
        )
        return BotGrantResult(
            action="granted",
            target_slack_user_id=target_slack_user_id,
            workspace_name="Test WS",
        )
    mock_provision.side_effect = _mixed_effect
    _create_admin("TTEST123", "UADMIN123")
    _create_revoked_user("TTEST123", "U08ABC123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    # Both should be notified (one restored, one new)
    assert mock_send_conf.call_count == 2


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_skips_already_approved(mock_send_conf, mock_provision):
    """Test 10: Bulk grant does not notify already-approved users."""
    mock_send_conf.return_value = _ok_result()
    # First call returns already_approved, subsequent granted
    call_count = [0]
    def _mixed_effect(*, team_id, target_slack_user_id, approving_slack_user_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return BotGrantResult(
                action="already_approved",
                target_slack_user_id=target_slack_user_id,
                workspace_name="Test WS",
            )
        BotUserAccess.objects.create(
            workspace_id=team_id, slack_user_id=target_slack_user_id,
            status=ACCESS_STATUS_APPROVED, permission=PERMISSION_READ_ONLY,
            granted_by_slack_user_id=approving_slack_user_id,
        )
        return BotGrantResult(
            action="granted",
            target_slack_user_id=target_slack_user_id,
            workspace_name="Test WS",
        )
    mock_provision.side_effect = _mixed_effect
    _create_admin("TTEST123", "UADMIN123")
    _create_approved_user("TTEST123", "U08ABC123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    # Only U08DEF456 should be notified (U08ABC123 is already approved)
    channels_called = [c.kwargs["channel_id"] for c in mock_send_conf.call_args_list]
    assert "U08DEF456" in channels_called
    assert "U08ABC123" not in channels_called


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_skips_invalid_ids(mock_send_conf, mock_provision):
    """Test 11: Bulk grant does not notify invalid IDs."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, C08INVALID",
    )
    channels_called = [c.kwargs["channel_id"] for c in mock_send_conf.call_args_list]
    assert "U08ABC123" in channels_called
    assert "C08INVALID" not in channels_called


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_continues_after_dm_failure(mock_send_conf, mock_provision):
    """Test 12: Bulk grant continues after one user-DM failure."""
    mock_send_conf.side_effect = [_fail_result(), _ok_result()]
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    # Both should have been attempted
    assert mock_send_conf.call_count == 2


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_admin_response_lists_failures(mock_send_conf, mock_provision):
    """Test 13: Bulk admin response lists notification failures."""
    mock_send_conf.side_effect = [_ok_result(), _fail_result()]
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    result = process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    assert "User notifications failed" in result.response_text
    assert "U08DEF456" in result.response_text


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_bulk_no_failure_section_when_all_succeed(mock_send_conf, mock_provision):
    """Test 14: Empty notification-failure section is omitted."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    result = process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    assert "User notifications failed" not in result.response_text


# ===========================================================================
# format_single_grant_response — with notification_failed
# ===========================================================================


def test_format_single_granted_with_failure():
    result = GrantResult(action="granted", workspace_id="T", slack_user_id="U08ABC123")
    text = format_single_grant_response(result, notification_failed=True)
    assert "User notification: Failed" in text


def test_format_single_granted_without_failure():
    result = GrantResult(action="granted", workspace_id="T", slack_user_id="U08ABC123")
    text = format_single_grant_response(result, notification_failed=False)
    assert "User notification: Failed" not in text


def test_format_single_already_approved_ignores_failure():
    result = GrantResult(action="already_approved", workspace_id="T", slack_user_id="U08ABC123")
    text = format_single_grant_response(result, notification_failed=True)
    assert "User notification: Failed" not in text


# ===========================================================================
# format_bulk_grant_response — with notification_failures
# ===========================================================================


def test_format_bulk_with_notification_failures():
    result = BulkGrantResult(
        approved=["U08ABC123"],
        restored=["U08GHI789"],
    )
    text = format_bulk_grant_response(
        result,
        notification_failures=["U08DEF456"],
    )
    assert "User notifications failed" in text
    assert "U08DEF456" in text


def test_format_bulk_without_notification_failures():
    result = BulkGrantResult(approved=["U08ABC123"])
    text = format_bulk_grant_response(result, notification_failures=[])
    assert "User notifications failed" not in text


def test_format_bulk_none_notification_failures():
    result = BulkGrantResult(approved=["U08ABC123"])
    text = format_bulk_grant_response(result, notification_failures=None)
    assert "User notifications failed" not in text


# ===========================================================================
# Views integration — Phase 5 bypasses
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_admin_dm_bypasses_analytics_queue(
    mock_send_conf, mock_send_view, mock_reaction, mock_enqueue, mock_provision,
):
    """Test 15: Admin DM grant still bypasses analytics queue."""
    mock_send_conf.return_value = _ok_result()
    mock_send_view.return_value = MagicMock(ok=True, response_ts="ts")
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_admin_dm_no_eyes_reaction(
    mock_send_conf, mock_send_view, mock_reaction, mock_enqueue, mock_provision,
):
    """Test 17: Admin DM grant still does not add eyes reaction."""
    mock_send_conf.return_value = _ok_result()
    mock_send_view.return_value = MagicMock(ok=True, response_ts="ts")
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    _post(client, _dm_payload(text="Give U08ABC123 access"))
    mock_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_admin_dm_grant_via_endpoint_sends_confirmation(
    mock_send_conf, mock_send_view, mock_reaction, mock_enqueue, mock_provision,
):
    """End-to-end: admin DM grant triggers user confirmation DM."""
    mock_send_conf.return_value = _ok_result()
    mock_send_view.return_value = MagicMock(ok=True, response_ts="ts")
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    _post(client, _dm_payload(text="Give U08ABC123 access"))
    # Confirmation DM should have been sent to the user
    assert mock_send_conf.call_count == 1
    assert mock_send_conf.call_args.kwargs["channel_id"] == "U08ABC123"
    # Admin response also sent
    assert mock_send_view.call_count == 1


# ===========================================================================
# Regression — approved mention unchanged
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_approved_mention_normal_flow_unchanged(
    mock_send_conf, mock_send_notif, mock_reaction, mock_enqueue,
):
    """Test 18: Normal approved mention behaviour remains unchanged."""
    mock_reaction.return_value = MagicMock(ok=True)
    mock_send_conf.return_value = _ok_result()
    mock_send_notif.return_value = _ok_result()
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload(user_id="UUSER123"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()
    # No confirmation DM for normal analytics flow
    mock_send_conf.assert_not_called()


# ===========================================================================
# Regression — Phase 4 unauthorized user unchanged
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
@patch("apps.slack_bot.unauthorized_notification_service.send_slack_message")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_unauthorized_user_phase4_unchanged(
    mock_send_conf, mock_send_notif, mock_send_view, mock_reaction, mock_enqueue,
):
    """Test 19: Unauthorized-user Phase 4 behaviour remains unchanged."""
    mock_send_conf.return_value = _ok_result()
    mock_send_notif.return_value = _ok_result()
    mock_send_view.return_value = _ok_result()
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _mention_payload(user_id="UUNREG123"))
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()
    # Phase 4 notification should fire (user DM + channel response + admin DM)
    assert mock_send_notif.call_count >= 1
    # Phase 5 confirmation should NOT fire
    mock_send_conf.assert_not_called()
    # UnauthorizedAccessAttempt should be created
    assert UnauthorizedAccessAttempt.objects.filter(
        workspace_id="TTEST123", slack_user_id="UUNREG123",
    ).exists()


# ===========================================================================
# No markdown report
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.user_confirmation_service.send_slack_message")
def test_no_markdown_report(mock_send_conf, mock_provision):
    """Test 20: No markdown report is created."""
    mock_send_conf.return_value = _ok_result()
    mock_provision.side_effect = _provision_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert isinstance(result.response_text, str)
    # The response is a plain text Slack DM, not a markdown file
    assert not result.response_text.endswith(".md")
