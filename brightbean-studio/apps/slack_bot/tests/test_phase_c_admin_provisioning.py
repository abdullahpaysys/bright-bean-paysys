"""Phase C tests — admin DM integration with BrightBean provisioning.

Tests cover:
 1.  Parser: ID + email syntax.
 2.  Parser: ID without email.
 3.  Parser: bulk mixed email/no-email.
 4.  Parser: email conflict detection.
 5.  Parser: invalid IDs still reported.
 6.  Service: single provisioning success.
 7.  Service: source channel from UnauthorizedAccessAttempt.
 8.  Service: missing source channel → FAILED.
 9.  Service: email passed through to provisioning.
 10. Service: bulk provisioning — one failure does not block others.
 11. Service: confirmation DM only for NEWLY_PROVISIONED.
 12. Service: confirmation DM for RESTORED.
 13. Service: confirmation DM for REPAIRED.
 14. Service: no confirmation DM for ALREADY_PROVISIONED.
 15. Service: no confirmation DM for FAILED.
 16. Service: email conflicts reported in response.
 17. Response: NEWLY_PROVISIONED format.
 18. Response: ALREADY_PROVISIONED format.
 19. Response: RESTORED format.
 20. Response: REPAIRED format.
 21. Response: FAILED format.
 22. Response: bulk with mixed statuses.
 23. Notification: instruction text includes email syntax.
 24. Notification: instruction text includes "analytics read-only".
 25. User confirmation: text includes "Analytics read-only".
 26. Service: non-admin blocked.
 27. Service: no grant intent → not handled.
 28. Service: usage message for no IDs.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.slack_bot.admin_dm_parser import parse_grant_command
from apps.slack_bot.admin_dm_response import (
    format_bot_grant_response,
    format_bulk_bot_grant_response,
)
from apps.slack_bot.admin_dm_service import process_admin_dm
from apps.slack_bot.bot_grant_service import BotGrantResult
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ADMIN_STATUS_ACTIVE,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.models import (
    BotAdministrator,
    BotUserAccess,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.unauthorized_notification_service import (
    format_admin_notification_dm,
)
from apps.slack_bot.user_confirmation_service import (
    USER_CONFIRMATION_DM_TEXT,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_admin(workspace_id="TTEST123", slack_user_id="UADMIN123"):
    return BotAdministrator.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
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


def _grant_side_effect(
    *,
    action="granted",
    workspace_name="Test WS",
    failure_reason="",
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
        return BotGrantResult(
            action=action,
            target_slack_user_id=target_slack_user_id,
            workspace_name=workspace_name,
            failure_reason=failure_reason,
        )
    return _effect


# ===========================================================================
# Parser tests
# ===========================================================================


def test_parser_id_with_email():
    """Test 1: Parser extracts ID + email syntax."""
    result = parse_grant_command("Give U08ABC123 access as user@company.com")
    assert result.is_grant_intent is True
    assert len(result.entries) == 1
    assert result.entries[0].member_id == "U08ABC123"
    assert result.entries[0].email == "user@company.com"


def test_parser_id_without_email():
    """Test 2: Parser extracts ID without email."""
    result = parse_grant_command("Give U08ABC123 access")
    assert result.is_grant_intent is True
    assert len(result.entries) == 1
    assert result.entries[0].member_id == "U08ABC123"
    assert result.entries[0].email is None


def test_parser_bulk_mixed_email():
    """Test 3: Parser handles bulk with mixed email/no-email."""
    text = (
        "Give access to:\n"
        "U08ABC123 as user1@company.com\n"
        "U08DEF456"
    )
    result = parse_grant_command(text)
    assert result.is_grant_intent is True
    assert len(result.entries) == 2
    assert result.entries[0].member_id == "U08ABC123"
    assert result.entries[0].email == "user1@company.com"
    assert result.entries[1].member_id == "U08DEF456"
    assert result.entries[1].email is None


def test_parser_email_conflict():
    """Test 4: Parser detects conflicting emails for same ID."""
    text = (
        "Give access to:\n"
        "U08ABC123 as user1@company.com\n"
        "U08ABC123 as user2@company.com"
    )
    result = parse_grant_command(text)
    assert result.is_grant_intent is True
    assert "U08ABC123" in result.email_conflicts


def test_parser_invalid_ids_reported():
    """Test 5: Parser still reports invalid IDs."""
    result = parse_grant_command("Give C08INVALID access")
    assert result.is_grant_intent is True
    assert result.invalid_ids == ["C08INVALID"]
    assert len(result.entries) == 0


# ===========================================================================
# Service tests
# ===========================================================================


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_single_provisioning_success(mock_provision, mock_conf):
    """Test 6: Single provisioning success."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access as user@company.com")
    assert result.is_admin_dm is True
    assert result.handled is True
    assert "Access granted" in result.response_text


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_source_channel_from_attempt(mock_provision, mock_conf):
    """Test 7: grant_bot_access is called with correct team_id and target."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C456")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    call_kwargs = mock_provision.call_args.kwargs
    assert call_kwargs["team_id"] == "TTEST123"
    assert call_kwargs["target_slack_user_id"] == "U08ABC123"


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_missing_source_channel_failed(mock_provision, mock_conf):
    """Test 8: Grant succeeds even without a prior UnauthorizedAccessAttempt."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    # No UnauthorizedAccessAttempt created
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.handled is True
    assert "Access granted" in result.response_text
    mock_provision.assert_called_once()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_email_passed_through(mock_provision, mock_conf):
    """Test 9: grant_bot_access is called with correct target_slack_user_id."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access as user@company.com")
    call_kwargs = mock_provision.call_args.kwargs
    assert call_kwargs["target_slack_user_id"] == "U08ABC123"
    assert call_kwargs["team_id"] == "TTEST123"


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_bulk_one_failure_does_not_block(mock_provision, mock_conf):
    """Test 10: Bulk — one failure does not block others."""
    mock_conf.return_value = True
    call_count = [0]

    def _mixed_effect(*, team_id, target_slack_user_id, approving_slack_user_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return BotGrantResult(
                action="failed",
                target_slack_user_id=target_slack_user_id,
                failure_reason="User not found.",
            )
        BotUserAccess.objects.create(
            workspace_id=team_id, slack_user_id=target_slack_user_id,
            status=ACCESS_STATUS_APPROVED, permission=PERMISSION_READ_ONLY,
        )
        return BotGrantResult(
            action="granted",
            target_slack_user_id=target_slack_user_id,
            workspace_name="Test WS",
        )

    mock_provision.side_effect = _mixed_effect
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    result = process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456",
    )
    assert result.handled is True
    assert mock_provision.call_count == 2
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08DEF456",
    ).exists()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_confirmation_dm_only_newly_provisioned(mock_provision, mock_conf):
    """Test 11: Confirmation DM sent only for granted."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect(action="granted")
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_conf.assert_called_once()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_confirmation_dm_for_restored(mock_provision, mock_conf):
    """Test 12: Confirmation DM sent for restored."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect(action="restored")
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_conf.assert_called_once()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_confirmation_dm_for_repaired(mock_provision, mock_conf):
    """Test 13: Confirmation DM sent for granted (formerly REPAIRED)."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect(action="granted")
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_conf.assert_called_once()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_no_confirmation_dm_already_provisioned(mock_provision, mock_conf):
    """Test 14: No confirmation DM for already_approved."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect(action="already_approved")
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_conf.assert_not_called()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_no_confirmation_dm_failed(mock_provision, mock_conf):
    """Test 15: No confirmation DM for failed."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect(action="failed", failure_reason="Some failure.")
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    mock_conf.assert_not_called()


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_email_conflicts_reported(mock_provision, mock_conf):
    """Test 16: Email conflicts are reported in the response."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    text = (
        "Give access to:\n"
        "U08ABC123 as user1@company.com\n"
        "U08ABC123 as user2@company.com"
    )
    result = process_admin_dm("TTEST123", "UADMIN123", text)
    assert result.handled is True
    assert "Email conflict" in result.response_text or "conflict" in result.response_text.lower()


# ===========================================================================
# Response formatting tests
# ===========================================================================


def test_response_newly_provisioned():
    """Test 17: granted response format."""
    result = BotGrantResult(
        action="granted",
        target_slack_user_id="U08ABC123",
        workspace_name="Test WS",
    )
    text = format_bot_grant_response(result)
    assert "Access granted" in text
    assert "U08ABC123" in text
    assert "Test WS" in text
    assert "Analytics read-only" in text


def test_response_already_provisioned():
    """Test 18: already_approved response format."""
    result = BotGrantResult(
        action="already_approved",
        target_slack_user_id="U08ABC123",
        workspace_name="Test WS",
    )
    text = format_bot_grant_response(result)
    assert "No change" in text
    assert "Already approved" in text


def test_response_restored():
    """Test 19: restored response format."""
    result = BotGrantResult(
        action="restored",
        target_slack_user_id="U08ABC123",
        workspace_name="Test WS",
    )
    text = format_bot_grant_response(result)
    assert "restored" in text.lower()


def test_response_repaired():
    """Test 20: already_approved response format (formerly REPAIRED)."""
    result = BotGrantResult(
        action="already_approved",
        target_slack_user_id="U08ABC123",
        workspace_name="Test WS",
    )
    text = format_bot_grant_response(result)
    assert "No change" in text
    assert "Already approved" in text


def test_response_failed():
    """Test 21: failed response format."""
    result = BotGrantResult(
        action="failed",
        target_slack_user_id="U08ABC123",
        failure_reason="No user found.",
    )
    text = format_bot_grant_response(result)
    assert "not granted" in text.lower()
    assert "U08ABC123" in text


def test_response_bulk_mixed_statuses():
    """Test 22: Bulk response with mixed statuses."""
    results = [
        (
            BotGrantResult(
                action="granted",
                target_slack_user_id="U08ABC123",
                workspace_name="Test WS",
            ),
            False,
        ),
        (
            BotGrantResult(
                action="failed",
                target_slack_user_id="U08DEF456",
                failure_reason="Not found.",
            ),
            False,
        ),
        (
            BotGrantResult(
                action="already_approved",
                target_slack_user_id="U08GHI789",
                workspace_name="Test WS",
            ),
            False,
        ),
    ]
    text = format_bulk_bot_grant_response(results)
    assert "Bulk access update" in text
    assert "U08ABC123" in text
    assert "U08DEF456" in text
    assert "U08GHI789" in text
    assert "Analytics read-only" in text


# ===========================================================================
# Notification text tests
# ===========================================================================


def test_notification_includes_email_syntax():
    """Test 23: Notification instruction includes email syntax."""
    text = format_admin_notification_dm(
        slack_user_id="U08ABC123",
        source_channel_id="C08XYZ456",
        attempted_at=timezone.now(),
    )
    assert "as user@company.com" in text


def test_notification_includes_analytics_read_only():
    """Test 24: Notification instruction includes 'analytics read-only'."""
    text = format_admin_notification_dm(
        slack_user_id="U08ABC123",
        source_channel_id="C08XYZ456",
        attempted_at=timezone.now(),
    )
    assert "analytics read-only" in text.lower()


# ===========================================================================
# User confirmation text tests
# ===========================================================================


def test_user_confirmation_includes_analytics_read_only():
    """Test 25: User confirmation DM text includes 'Analytics read-only'."""
    assert "Analytics read-only" in USER_CONFIRMATION_DM_TEXT


# ===========================================================================
# Service edge case tests
# ===========================================================================


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_non_admin_blocked(mock_provision, mock_conf):
    """Test 26: Non-admin is blocked."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UOTHER456", "Give U08ABC123 access")
    assert result.is_admin_dm is False
    assert result.handled is False
    mock_provision.assert_not_called()


@override_settings(ADMIN_LLM_CHAT_ENABLED="false")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_no_grant_intent_not_handled(mock_provision, mock_conf):
    """Test 27: No grant intent → not handled (when LLM chat disabled)."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Hello there")
    assert result.is_admin_dm is True
    assert result.handled is False


@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
def test_service_usage_message_for_no_ids(mock_provision, mock_conf):
    """Test 28: Usage message returned when no IDs found."""
    mock_conf.return_value = True
    mock_provision.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give access to everyone")
    assert result.handled is True
    assert "could not find" in result.response_text
