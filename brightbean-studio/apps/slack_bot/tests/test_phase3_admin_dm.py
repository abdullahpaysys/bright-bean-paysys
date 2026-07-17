"""Phase 3 tests — administrator DM access grants.

Tests cover:
1. Parser: single ID, bulk IDs, comma/line/natural-language, dedup, invalid, channel IDs
2. Service: admin detection, active/inactive admin, workspace isolation, no admin
3. Views: admin DM routing, non-admin DM blocked, no enqueue/LLM/reaction, duplicate
4. Response formatting: single, bulk, already approved, restored, usage message
5. Slack delivery failure does not roll back committed access
6. Existing mention/thread behaviour unchanged
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
from apps.slack_bot.admin_dm_parser import (
    parse_grant_command,
)
from apps.slack_bot.admin_dm_response import (
    format_bulk_grant_response,
    format_single_grant_response,
)
from apps.slack_bot.admin_dm_service import (
    is_active_admin,
    is_direct_message_channel,
    process_admin_dm,
)
from apps.slack_bot.bot_grant_service import (
    BotGrantResult,
)
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    ADMIN_STATUS_INACTIVE,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.models import (
    BotAdministrator,
    BotUserAccess,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.tests.conftest import signed_slack_headers

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


# ===========================================================================
# Parser tests
# ===========================================================================


def test_parse_single_member_id():
    result = parse_grant_command("Give U08ABC123 access")
    assert result.is_grant_intent is True
    assert result.member_ids == ["U08ABC123"]


def test_parse_multiple_member_ids():
    result = parse_grant_command("Give access to U08ABC123 and U08DEF456")
    assert result.is_grant_intent is True
    assert result.member_ids == ["U08ABC123", "U08DEF456"]


def test_parse_comma_separated_ids():
    result = parse_grant_command("Whitelist U08ABC123, U08DEF456, U08GHI789")
    assert result.is_grant_intent is True
    assert result.member_ids == ["U08ABC123", "U08DEF456", "U08GHI789"]


def test_parse_line_separated_ids():
    text = "Whitelist these users:\nU08ABC123\nU08DEF456\nU08GHI789"
    result = parse_grant_command(text)
    assert result.is_grant_intent is True
    assert result.member_ids == ["U08ABC123", "U08DEF456", "U08GHI789"]


def test_parse_natural_language():
    result = parse_grant_command("Allow U08ABC123 to use the bot")
    assert result.is_grant_intent is True
    assert result.member_ids == ["U08ABC123"]


def test_parse_deduplication():
    result = parse_grant_command("Give U08ABC123 and U08ABC123 access")
    assert result.member_ids == ["U08ABC123"]


def test_parse_invalid_ids():
    result = parse_grant_command("Give C08INVALID access")
    assert result.is_grant_intent is True
    assert result.member_ids == []
    assert result.invalid_ids == ["C08INVALID"]


def test_parse_channel_id_rejected():
    result = parse_grant_command("Give C08INVALID and G08INVALID access")
    assert result.member_ids == []
    assert sorted(result.invalid_ids) == ["C08INVALID", "G08INVALID"]


def test_parse_mixed_valid_invalid():
    result = parse_grant_command("Give U08ABC123 and C08INVALID access")
    assert result.member_ids == ["U08ABC123"]
    assert result.invalid_ids == ["C08INVALID"]


def test_parse_no_grant_intent():
    result = parse_grant_command("Hello bot, how are you?")
    assert result.is_grant_intent is False


def test_parse_grant_intent_no_ids():
    result = parse_grant_command("Give access to everyone")
    assert result.is_grant_intent is True
    assert result.member_ids == []


def test_parse_empty_text():
    result = parse_grant_command("")
    assert result.is_grant_intent is False
    assert result.member_ids == []


def test_parse_w_guest_id():
    result = parse_grant_command("Allow W08GUEST1 to use the bot")
    assert result.member_ids == ["W08GUEST1"]


def test_parse_various_grant_terms():
    for term in ("give", "grant", "allow", "whitelist", "approve", "add access"):
        result = parse_grant_command(f"{term} U08ABC123")
        assert result.is_grant_intent is True, f"Failed for term: {term}"


# ===========================================================================
# DM detection helpers
# ===========================================================================


def test_is_dm_channel_true():
    assert is_direct_message_channel("D123456") is True


def test_is_dm_channel_false():
    assert is_direct_message_channel("C123456") is False


def test_is_dm_channel_empty():
    assert is_direct_message_channel("") is False


# ===========================================================================
# is_active_admin
# ===========================================================================


@pytest.mark.django_db
def test_is_active_admin_true():
    _create_admin("TTEST123", "UADMIN123")
    assert is_active_admin("TTEST123", "UADMIN123") is True


@pytest.mark.django_db
def test_is_active_admin_false_inactive():
    _create_admin("TTEST123", "UADMIN123", status=ADMIN_STATUS_INACTIVE)
    assert is_active_admin("TTEST123", "UADMIN123") is False


@pytest.mark.django_db
def test_is_active_admin_false_wrong_user():
    _create_admin("TTEST123", "UADMIN123")
    assert is_active_admin("TTEST123", "UOTHER456") is False


@pytest.mark.django_db
def test_is_active_admin_false_wrong_workspace():
    _create_admin("TTEST123", "UADMIN123")
    assert is_active_admin("TOTHER456", "UADMIN123") is False


@pytest.mark.django_db
def test_is_active_admin_false_no_admin():
    assert is_active_admin("TTEST123", "UADMIN123") is False


# ===========================================================================
# process_admin_dm — service tests
# ===========================================================================


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_single_grant(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.is_admin_dm is True
    assert result.handled is True
    assert "Access granted" in result.response_text
    assert "Member: U08ABC123" in result.response_text
    assert "Workspace: Test WS" in result.response_text
    assert "Analytics read-only" in result.response_text
    assert "Status: Approved" in result.response_text
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
        status="APPROVED",
    ).exists()


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_bulk_grant(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456", "U08GHI789"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    result = process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give access to U08ABC123, U08DEF456, U08GHI789",
    )
    assert result.handled is True
    assert "Bulk access update" in result.response_text
    assert BotUserAccess.objects.filter(workspace_id="TTEST123").count() == 3


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_already_approved(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect(action="already_approved")
    _create_admin("TTEST123", "UADMIN123")
    _create_approved_user("TTEST123", "U08ABC123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.handled is True
    assert "No change" in result.response_text


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_revoked_restored(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect(action="restored")
    _create_admin("TTEST123", "UADMIN123")
    BotUserAccess.objects.create(
        workspace_id="TTEST123",
        slack_user_id="U08ABC123",
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.handled is True
    assert "restored" in result.response_text.lower()
    access = BotUserAccess.objects.get(workspace_id="TTEST123", slack_user_id="U08ABC123")
    assert access.status == "APPROVED"


@pytest.mark.django_db
def test_admin_dm_invalid_ids_reported():
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give C08INVALID access")
    assert result.handled is True
    assert "Invalid" in result.response_text


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_mixed_valid_invalid(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    result = process_admin_dm(
        "TTEST123", "UADMIN123",
        "Give U08ABC123 and C08INVALID access",
    )
    assert result.handled is True
    assert "Bulk" in result.response_text
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
    ).exists()


@pytest.mark.django_db
def test_admin_dm_non_admin_blocked():
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UOTHER456", "Give U08ABC123 access")
    assert result.is_admin_dm is False
    assert result.handled is False
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
def test_admin_dm_workspace_isolation():
    _create_admin("TTEST123", "UADMIN123")
    _create_admin("TOTHER456", "UADMIN456")
    result = process_admin_dm("TOTHER456", "UADMIN123", "Give U08ABC123 access")
    assert result.is_admin_dm is False
    assert BotUserAccess.objects.filter(workspace_id="TOTHER456").count() == 0


@pytest.mark.django_db
def test_admin_dm_inactive_admin_blocked():
    _create_admin("TTEST123", "UADMIN123", status=ADMIN_STATUS_INACTIVE)
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.is_admin_dm is False
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
def test_admin_dm_no_admin_blocked():
    result = process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    assert result.is_admin_dm is False
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
@override_settings(ADMIN_LLM_CHAT_ENABLED="false")
def test_admin_dm_no_grant_intent():
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Hello bot")
    assert result.is_admin_dm is True
    assert result.handled is False


@pytest.mark.django_db
def test_admin_dm_grant_no_ids_returns_usage():
    _create_admin("TTEST123", "UADMIN123")
    result = process_admin_dm("TTEST123", "UADMIN123", "Give access to everyone")
    assert result.handled is True
    assert "could not find" in result.response_text


@pytest.mark.django_db
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
def test_admin_dm_grant_performer_is_admin(mock_conf, mock_grant):
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    process_admin_dm("TTEST123", "UADMIN123", "Give U08ABC123 access")
    access = BotUserAccess.objects.get(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
    )
    assert access.granted_by_slack_user_id == "UADMIN123"


# ===========================================================================
# Views integration — admin DM routing
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_single_grant_via_endpoint(mock_send, mock_grant, mock_conf):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_send.assert_called_once()
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
    ).exists()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_bulk_grant_via_endpoint(mock_send, mock_grant, mock_conf):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    for uid in ("U08ABC123", "U08DEF456"):
        _create_unauthorized_attempt("TTEST123", uid, "C123")
    client = Client()
    response = _post(client, _dm_payload(
        text="Give access to U08ABC123, U08DEF456",
    ))
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert BotUserAccess.objects.filter(workspace_id="TTEST123").count() == 2


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.send_slack_message")
def test_non_admin_dm_blocked(mock_send):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _dm_payload(
        user_id="UOTHER456", text="Give U08ABC123 access",
    ))
    assert response.status_code == 200
    assert response.json()["reason"] == "non_admin_dm"
    mock_send.assert_not_called()
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_does_not_enqueue(mock_send, mock_reaction, mock_enqueue, mock_grant, mock_conf):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    _post(client, _dm_payload(text="Give U08ABC123 access"))
    mock_enqueue.assert_not_called()
    mock_reaction.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_duplicate_does_not_grant_twice(mock_send, mock_grant, mock_conf):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    payload = _dm_payload(text="Give U08ABC123 access")
    r1 = _post(client, payload)
    assert r1.json()["status"] == "received"
    r2 = _post(client, payload)
    assert r2.json()["status"] == "duplicate"
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
    ).count() == 1


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_usage_response_no_ids(mock_send):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    _post(client, _dm_payload(text="Give access to everyone"))
    mock_send.assert_called_once()
    sent_text = mock_send.call_args.kwargs["text"]
    assert "could not find" in sent_text


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET, ADMIN_LLM_CHAT_ENABLED="false")
@patch("apps.slack_bot.views.send_slack_message")
def test_admin_dm_unrecognized_command(mock_send):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    _create_admin("TTEST123", "UADMIN123")
    client = Client()
    response = _post(client, _dm_payload(text="Hello bot"))
    assert response.json()["reason"] == "not_grant_command"
    mock_send.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm")
@patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access")
@patch("apps.slack_bot.views.send_slack_message")
def test_slack_response_failure_does_not_rollback(mock_send, mock_grant, mock_conf):
    """Access grant is committed even if Slack response delivery fails."""
    from apps.slack_bot.exceptions import SlackDeliveryError
    mock_send.side_effect = SlackDeliveryError("delivery failed")
    mock_conf.return_value = True
    mock_grant.side_effect = _grant_side_effect()
    _create_admin("TTEST123", "UADMIN123")
    _create_unauthorized_attempt("TTEST123", "U08ABC123", "C123")
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    # The endpoint should still return 200
    assert response.status_code == 200
    # Access should still be granted despite delivery failure
    assert BotUserAccess.objects.filter(
        workspace_id="TTEST123", slack_user_id="U08ABC123",
        status="APPROVED",
    ).exists()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.send_slack_message")
def test_no_admin_dm_ignored(mock_send):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    assert response.status_code == 200
    assert response.json()["reason"] == "non_admin_dm"
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.send_slack_message")
def test_inactive_admin_dm_blocked(mock_send):
    mock_send.return_value = MagicMock(ok=True, response_ts="ts")
    _create_admin("TTEST123", "UADMIN123", status=ADMIN_STATUS_INACTIVE)
    client = Client()
    response = _post(client, _dm_payload(text="Give U08ABC123 access"))
    assert response.json()["reason"] == "non_admin_dm"
    assert BotUserAccess.objects.count() == 0


# ===========================================================================
# Existing mention/thread behaviour unchanged
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_existing_mention_still_works(mock_reaction, mock_enqueue):
    mock_reaction.return_value = MagicMock(ok=True)
    _create_approved_user("TTEST123", "UUSER123")
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_unapproved_mention_still_blocked(mock_reaction, mock_enqueue):
    mock_reaction.return_value = MagicMock(ok=True)
    client = Client()
    response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["reason"] == "access_denied"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
@patch("apps.slack_bot.views.enqueue_inbound_event")
@patch("apps.slack_bot.views.add_processing_reaction")
def test_non_dm_message_without_thread_still_ignored(mock_reaction, mock_enqueue):
    """A non-DM message without thread_ts should still be ignored."""
    mock_reaction.return_value = MagicMock(ok=True)
    client = Client()
    payload = {
        "team_id": "TTEST123",
        "type": "event_callback",
        "event_id": "Ev_nothread",
        "event": {
            "type": "message",
            "user": "UUSER123",
            "text": "standalone message",
            "ts": "1720000000.000100",
            "channel": "C123",  # Not a DM channel
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "message_without_thread"


# ===========================================================================
# Response formatting tests
# ===========================================================================


def test_format_single_grant_granted():
    result = GrantResult(action="granted", workspace_id="T1", slack_user_id="U1")
    text = format_single_grant_response(result)
    assert "Access granted" in text
    assert "U1" in text
    assert "Read-only" in text


def test_format_single_grant_already_approved():
    result = GrantResult(action="already_approved", workspace_id="T1", slack_user_id="U1")
    text = format_single_grant_response(result)
    assert "No change" in text
    assert "Already approved" in text


def test_format_single_grant_restored():
    result = GrantResult(action="restored", workspace_id="T1", slack_user_id="U1")
    text = format_single_grant_response(result)
    assert "restored" in text.lower()


def test_format_bulk_grant_response():
    result = BulkGrantResult(
        approved=["U08ABC123", "U08DEF456"],
        restored=["U08GHI789"],
        already_approved=["U08JKL012"],
        invalid=["C08INVALID"],
        failed=[],
    )
    text = format_bulk_grant_response(result)
    assert "Bulk access update" in text
    assert "U08ABC123" in text
    assert "U08DEF456" in text
    assert "U08GHI789" in text
    assert "U08JKL012" in text
    assert "C08INVALID" in text
    assert "Read-only" in text


def test_format_bulk_grant_empty_sections_omitted():
    result = BulkGrantResult(
        approved=["U08ABC123"],
        restored=[],
        already_approved=[],
        invalid=[],
        failed=[],
    )
    text = format_bulk_grant_response(result)
    assert "Restored" not in text
    assert "Already approved" not in text
    assert "Invalid" not in text
    assert "Failed" not in text
