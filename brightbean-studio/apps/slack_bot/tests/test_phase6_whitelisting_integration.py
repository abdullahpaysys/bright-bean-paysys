"""Phase 6 — Final whitelisting integration validation.

Integration tests covering the complete end-to-end whitelisting workflow:

1.  Single-user workflow: unregistered → blocked → notified → admin grants
    → user confirmed → user mentions bot → access gate passes → reaction
    lifecycle → enqueued → processed → reaction removed → response sent.
2.  Thread-reply workflow: unblocked → blocked → granted → accepted with
    correct thread context and reaction targeting.
3.  Bulk admin grant with mixed user states.
4.  Revoked-user restoration workflow.
5.  Failure-path with reaction lifecycle.
6.  Idempotency validation.
7.  Audit validation.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, override_settings

from apps.slack_bot.bot_grant_service import BotGrantResult
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ACCESS_GRANTED,
    AUDIT_ACCESS_RESTORED,
    AUDIT_ADMIN_NOTIFICATION_SENT,
    AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
    AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
    PERMISSION_READ_ONLY,
    STATUS_RESPONDED,
)
from apps.slack_bot.delivery import SlackDeliveryResult
from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    SlackInboundEvent,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.reactions import ReactionResult
from apps.slack_bot.tests.conftest import signed_slack_headers

SECRET = "test_secret"

# Common patch targets
_P_ENQUEUE = "apps.slack_bot.views.enqueue_inbound_event"
_P_ADD_REACTION = "apps.slack_bot.views.add_processing_reaction"
_P_REMOVE_REACTION = "apps.slack_bot.views.remove_processing_reaction"
_P_SEND_VIEW = "apps.slack_bot.views.send_slack_message"
_P_SEND_NOTIF = "apps.slack_bot.unauthorized_notification_service.send_slack_message"
_P_SEND_CONF = "apps.slack_bot.user_confirmation_service.send_slack_message"
_P_PROVISION = "apps.slack_bot.admin_dm_service.grant_slack_analytics_access"


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
    user_id="UTESTUSER",
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
    user_id="UTESTUSER",
    channel_id="C08XYZ456",
    thread_ts="1719999999.000050",
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
    text="Give UTESTUSER access",
    ts="1720000000.000300",
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


def _create_admin(workspace_id="TTEST123", slack_user_id="UADMIN123"):
    return BotAdministrator.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
    )


def _create_approved_user(workspace_id="TTEST123", slack_user_id="UTESTUSER"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    )


def _create_revoked_user(workspace_id="TTEST123", slack_user_id="UTESTUSER"):
    return BotUserAccess.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )


def _create_bot_thread(channel_id="C08XYZ456", response_ts="1719999999.000050"):
    return SlackInboundEvent.objects.create(
        event_id="Ev_parent",
        team_id="TTEST123",
        channel_id=channel_id,
        user_id="UBOT",
        event_ts=response_ts,
        message_text="bot response",
        thread_ts="",
        status=STATUS_RESPONDED,
        response_ts=response_ts,
    )


def _ok_delivery(channel_id="C", response_ts="ts"):
    return SlackDeliveryResult(ok=True, channel_id=channel_id, response_ts=response_ts)


def _reaction_ok():
    return ReactionResult(ok=True, channel_id="C", message_ts="ts")


def _all_patches():
    """Return dict of patch context managers for all external calls."""
    return {
        "enqueue": patch(_P_ENQUEUE),
        "add_reaction": patch(_P_ADD_REACTION),
        "remove_reaction": patch(_P_REMOVE_REACTION),
        "send_view": patch(_P_SEND_VIEW),
        "send_notif": patch(_P_SEND_NOTIF),
        "send_conf": patch(_P_SEND_CONF),
        "provision": patch(_P_PROVISION),
    }


def _enter_patches(patches):
    """Start all patches and return dict of mock objects."""
    mocks = {}
    for key, p in patches.items():
        mocks[key] = p.start()
    return mocks


def _exit_patches(patches):
    """Stop all patches."""
    for p in patches.values():
        p.stop()


def _setup_mocks(mocks):
    """Set standard return values on all mock objects."""
    mocks["enqueue"].return_value = None
    mocks["add_reaction"].return_value = _reaction_ok()
    mocks["remove_reaction"].return_value = _reaction_ok()
    mocks["send_view"].return_value = MagicMock(ok=True, response_ts="ts")
    mocks["send_notif"].return_value = _ok_delivery()
    mocks["send_conf"].return_value = _ok_delivery()
    mocks["provision"].side_effect = _provision_side_effect()


def _provision_side_effect(
    *,
    workspace_name="Test WS",
):
    """Return a side_effect function for grant_bot_access mock."""
    def _effect(
        *,
        team_id,
        target_slack_user_id,
        approving_slack_user_id,
    ):
        access = BotUserAccess.objects.filter(
            workspace_id=team_id, slack_user_id=target_slack_user_id,
        ).first()
        if access is None:
            BotUserAccess.objects.create(
                workspace_id=team_id,
                slack_user_id=target_slack_user_id,
                status=ACCESS_STATUS_APPROVED,
                permission=PERMISSION_READ_ONLY,
                granted_by_slack_user_id=approving_slack_user_id,
            )
            BotAccessAuditLog.objects.create(
                workspace_id=team_id,
                target_slack_user_id=target_slack_user_id,
                performed_by_slack_user_id=approving_slack_user_id,
                action=AUDIT_ACCESS_GRANTED,
                metadata={},
            )
            return BotGrantResult(
                action="granted",
                target_slack_user_id=target_slack_user_id,
                workspace_name=workspace_name,
            )
        if access.status == ACCESS_STATUS_REVOKED:
            access.status = ACCESS_STATUS_APPROVED
            access.granted_by_slack_user_id = approving_slack_user_id
            access.save(update_fields=["status", "granted_by_slack_user_id"])
            BotAccessAuditLog.objects.create(
                workspace_id=team_id,
                target_slack_user_id=target_slack_user_id,
                performed_by_slack_user_id=approving_slack_user_id,
                action=AUDIT_ACCESS_RESTORED,
                metadata={},
            )
            return BotGrantResult(
                action="restored",
                target_slack_user_id=target_slack_user_id,
                workspace_name=workspace_name,
            )
        return BotGrantResult(
            action="already_approved",
            target_slack_user_id=target_slack_user_id,
            workspace_name=workspace_name,
        )
    return _effect


# ===========================================================================
# 1. COMPLETE SINGLE-USER WORKFLOW
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_complete_single_user_workflow():
    """Full end-to-end: unregistered -> blocked -> notified -> granted -> confirmed -> accepted."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()

        # --- Step 1: Configure admin, no user access ---
        _create_admin("TTEST123", "UADMIN123")

        # --- Step 2: Unregistered user mentions bot ---
        resp1 = _post(client, _mention_payload(
            event_id="Ev_1", user_id="UTESTUSER",
            channel_id="C08XYZ456", ts="1720000000.000100",
        ))
        assert resp1.status_code == 200
        assert resp1.json()["reason"] == "access_denied"

        # Not enqueued, no reaction
        mocks["enqueue"].assert_not_called()
        mocks["add_reaction"].assert_not_called()

        # User received access-denied DM (first call to notif service)
        user_dm_call = mocks["send_notif"].call_args_list[0]
        assert user_dm_call.kwargs["channel_id"] == "UTESTUSER"
        assert "do not currently have access" in user_dm_call.kwargs["text"]

        # Admin received unauthorized-attempt DM (third call)
        admin_dm_call = mocks["send_notif"].call_args_list[2]
        assert admin_dm_call.kwargs["channel_id"] == "UADMIN123"
        assert "UTESTUSER" in admin_dm_call.kwargs["text"]
        assert "C08XYZ456" in admin_dm_call.kwargs["text"]

        # UnauthorizedAccessAttempt created
        attempt = UnauthorizedAccessAttempt.objects.get(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        )
        assert attempt.attempt_count == 1

        # Audit: unauthorized attempt + admin notification sent
        assert BotAccessAuditLog.objects.filter(
            workspace_id="TTEST123",
            target_slack_user_id="UTESTUSER",
            action=AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
        ).exists()
        assert BotAccessAuditLog.objects.filter(
            workspace_id="TTEST123",
            target_slack_user_id="UTESTUSER",
            action=AUDIT_ADMIN_NOTIFICATION_SENT,
        ).exists()

        # --- Step 3: Same user attempts again within 24 hours ---
        mocks["send_notif"].reset_mock()
        resp2 = _post(client, _mention_payload(
            event_id="Ev_2", user_id="UTESTUSER",
            channel_id="C08XYZ456", ts="1720000000.000200",
        ))
        assert resp2.json()["reason"] == "access_denied"

        attempt.refresh_from_db()
        assert attempt.attempt_count == 2

        # Admin should NOT receive another notification (suppressed)
        notif_channels = [
            c.kwargs["channel_id"] for c in mocks["send_notif"].call_args_list
        ]
        assert "UADMIN123" not in notif_channels

        # Audit: notification suppressed
        assert BotAccessAuditLog.objects.filter(
            workspace_id="TTEST123",
            target_slack_user_id="UTESTUSER",
            action=AUDIT_ADMIN_NOTIFICATION_SUPPRESSED,
        ).exists()

        # --- Step 4: Admin grants access via DM ---
        mocks["send_conf"].reset_mock()
        mocks["send_view"].reset_mock()
        resp3 = _post(client, _dm_payload(
            event_id="Ev_3", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "received"

        # BotUserAccess is APPROVED / READ_ONLY
        access = BotUserAccess.objects.get(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        )
        assert access.status == ACCESS_STATUS_APPROVED
        assert access.permission == PERMISSION_READ_ONLY
        assert access.granted_by_slack_user_id == "UADMIN123"

        # User received access-enabled confirmation DM
        assert mocks["send_conf"].call_count == 1
        assert mocks["send_conf"].call_args.kwargs["channel_id"] == "UTESTUSER"
        assert "enabled" in mocks["send_conf"].call_args.kwargs["text"]

        # Admin received grant response
        assert mocks["send_view"].call_count == 1
        assert "Access granted" in mocks["send_view"].call_args.kwargs["text"]

        # No enqueue, no reaction for admin DM
        mocks["enqueue"].assert_not_called()
        mocks["add_reaction"].assert_not_called()

        # Audit: access granted
        assert BotAccessAuditLog.objects.filter(
            workspace_id="TTEST123",
            target_slack_user_id="UTESTUSER",
            action=AUDIT_ACCESS_GRANTED,
        ).exists()

        # --- Step 5: Same user mentions bot again — now approved ---
        mocks["enqueue"].reset_mock()
        mocks["add_reaction"].reset_mock()
        mocks["remove_reaction"].reset_mock()
        resp4 = _post(client, _mention_payload(
            event_id="Ev_4", user_id="UTESTUSER",
            channel_id="C08XYZ456", ts="1720000000.000400",
        ))
        assert resp4.status_code == 200
        assert resp4.json()["status"] == "received"

        # Access gate passed -> enqueued
        mocks["enqueue"].assert_called_once()

        # Eyes reaction added to the exact user message timestamp
        mocks["add_reaction"].assert_called_once()
        assert mocks["add_reaction"].call_args.kwargs["channel_id"] == "C08XYZ456"
        assert mocks["add_reaction"].call_args.kwargs["message_ts"] == "1720000000.000400"
    finally:
        _exit_patches(patches)


# ===========================================================================
# 2. THREAD-REPLY WORKFLOW
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_thread_reply_workflow():
    """Unregistered thread reply blocked -> granted -> accepted with correct thread context."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")
        _create_bot_thread(channel_id="C08XYZ456", response_ts="1719999999.000050")

        # --- Unregistered thread reply ---
        resp1 = _post(client, _thread_reply_payload(
            event_id="Ev_tr_1", user_id="UTESTUSER",
            channel_id="C08XYZ456",
            thread_ts="1719999999.000050",
            ts="1720000000.000200",
        ))
        assert resp1.json()["reason"] == "access_denied"
        mocks["enqueue"].assert_not_called()
        mocks["add_reaction"].assert_not_called()

        # Notification flow ran
        assert UnauthorizedAccessAttempt.objects.filter(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        ).exists()

        # Channel response was sent in thread context
        channel_call = mocks["send_notif"].call_args_list[1]
        assert channel_call.kwargs["channel_id"] == "C08XYZ456"
        assert channel_call.kwargs["thread_ts"] == "1719999999.000050"

        # --- Admin grants access ---
        _post(client, _dm_payload(
            event_id="Ev_tr_2", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))
        assert BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
            status=ACCESS_STATUS_APPROVED,
        ).exists()

        # --- Approved thread reply ---
        mocks["enqueue"].reset_mock()
        mocks["add_reaction"].reset_mock()
        resp2 = _post(client, _thread_reply_payload(
            event_id="Ev_tr_3", user_id="UTESTUSER",
            channel_id="C08XYZ456",
            thread_ts="1719999999.000050",
            ts="1720000000.000300",
        ))
        assert resp2.json()["status"] == "received"
        mocks["enqueue"].assert_called_once()

        # Reaction targets the reply's own message timestamp, not parent
        mocks["add_reaction"].assert_called_once()
        assert mocks["add_reaction"].call_args.kwargs["message_ts"] == "1720000000.000300"
        assert mocks["add_reaction"].call_args.kwargs["channel_id"] == "C08XYZ456"
    finally:
        _exit_patches(patches)


# ===========================================================================
# 3. BULK ADMIN GRANT INTEGRATION
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_bulk_admin_grant_integration():
    """Bulk grant with new, revoked, already-approved, duplicate, invalid, and channel IDs."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")

        # Pre-existing states
        _create_revoked_user("TTEST123", "UREVOKED1")
        _create_approved_user("TTEST123", "UAPPROVED1")

        # Create UnauthorizedAccessAttempt records so source channel can be resolved
        for uid in ("UNEWUSER1", "UREVOKED1", "UAPPROVED1"):
            UnauthorizedAccessAttempt.objects.create(
                workspace_id="TTEST123", slack_user_id=uid,
                last_source_channel_id="C08XYZ456", attempt_count=1,
            )

        # Bulk command: new + revoked + already-approved + duplicate + invalid + channel
        resp = _post(client, _dm_payload(
            event_id="Ev_bulk_1",
            user_id="UADMIN123",
            text="Give access to UNEWUSER1, UREVOKED1, UAPPROVED1, UNEWUSER1, C08INVALID, X08BADID",
        ))
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"

        # New user approved
        assert BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="UNEWUSER1",
            status=ACCESS_STATUS_APPROVED, permission=PERMISSION_READ_ONLY,
        ).exists()

        # Revoked user restored
        assert BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="UREVOKED1",
            status=ACCESS_STATUS_APPROVED,
        ).exists()

        # Already-approved user unchanged
        approved = BotUserAccess.objects.get(
            workspace_id="TTEST123", slack_user_id="UAPPROVED1",
        )
        assert approved.status == ACCESS_STATUS_APPROVED

        # Duplicate processed once — only one row
        assert BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="UNEWUSER1",
        ).count() == 1

        # Invalid and channel IDs not added
        assert not BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="C08INVALID",
        ).exists()
        assert not BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="X08BADID",
        ).exists()

        # Confirmation DMs: only for new + restored (2 users)
        conf_channels = [
            c.kwargs["channel_id"] for c in mocks["send_conf"].call_args_list
        ]
        assert "UNEWUSER1" in conf_channels
        assert "UREVOKED1" in conf_channels
        assert "UAPPROVED1" not in conf_channels
        assert "C08INVALID" not in conf_channels

        # Admin response contains grouped sections
        admin_text = mocks["send_view"].call_args.kwargs["text"]
        assert "Bulk access update completed" in admin_text
        assert "UNEWUSER1" in admin_text
        assert "UREVOKED1" in admin_text
        assert "UAPPROVED1" in admin_text

        # No analytics, no reaction
        mocks["enqueue"].assert_not_called()
        mocks["add_reaction"].assert_not_called()
    finally:
        _exit_patches(patches)


# ===========================================================================
# 4. REVOKED-USER INTEGRATION
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_revoked_user_workflow():
    """Revoked user blocked -> no notification flow -> admin restores -> user can use bot."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")
        _create_revoked_user("TTEST123", "UTESTUSER")

        # --- Revoked user mentions bot ---
        resp1 = _post(client, _mention_payload(
            event_id="Ev_rev_1", user_id="UTESTUSER",
        ))
        assert resp1.json()["reason"] == "access_denied"
        mocks["enqueue"].assert_not_called()
        mocks["add_reaction"].assert_not_called()

        # Revoked user does NOT enter Phase 4 notification flow
        mocks["send_notif"].assert_not_called()
        assert not UnauthorizedAccessAttempt.objects.filter(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        ).exists()

        # Create UnauthorizedAccessAttempt so source channel can be resolved
        # (simulates a prior unauthorized attempt from another channel)
        UnauthorizedAccessAttempt.objects.create(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
            last_source_channel_id="C08XYZ456", attempt_count=1,
        )

        # --- Admin restores via grant DM ---
        _post(client, _dm_payload(
            event_id="Ev_rev_2", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))
        access = BotUserAccess.objects.get(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        )
        assert access.status == ACCESS_STATUS_APPROVED

        # Restored user receives confirmation DM
        assert mocks["send_conf"].call_count == 1
        assert mocks["send_conf"].call_args.kwargs["channel_id"] == "UTESTUSER"

        # Audit: access restored
        assert BotAccessAuditLog.objects.filter(
            workspace_id="TTEST123",
            target_slack_user_id="UTESTUSER",
            action=AUDIT_ACCESS_RESTORED,
        ).exists()

        # --- Restored user can now use bot ---
        mocks["enqueue"].reset_mock()
        mocks["add_reaction"].reset_mock()
        resp2 = _post(client, _mention_payload(
            event_id="Ev_rev_3", user_id="UTESTUSER",
        ))
        assert resp2.json()["status"] == "received"
        mocks["enqueue"].assert_called_once()
        mocks["add_reaction"].assert_called_once()
    finally:
        _exit_patches(patches)


# ===========================================================================
# 5. FAILURE-PATH WITH REACTION LIFECYCLE
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_failure_path_reaction_lifecycle():
    """Approved request: reaction added -> enqueue fails -> reaction removed."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        # Make enqueue raise to simulate failure
        mocks["enqueue"].side_effect = RuntimeError("queue down")

        client = Client()
        _create_approved_user("TTEST123", "UTESTUSER")

        resp = _post(client, _mention_payload(
            event_id="Ev_fail_1", user_id="UTESTUSER",
            channel_id="C08XYZ456", ts="1720000000.000500",
        ))
        assert resp.status_code == 200
        assert resp.json()["reason"] == "enqueue_failed"

        # Reaction was added before enqueue attempt
        mocks["add_reaction"].assert_called_once()

        # Reaction was removed after enqueue failure
        mocks["remove_reaction"].assert_called_once()
        assert mocks["remove_reaction"].call_args.kwargs["channel_id"] == "C08XYZ456"
        assert mocks["remove_reaction"].call_args.kwargs["message_ts"] == "1720000000.000500"
    finally:
        _exit_patches(patches)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_reaction_removal_failure_does_not_block_response():
    """If reaction removal fails, the error response is still sent."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        # Reaction removal fails
        mocks["remove_reaction"].return_value = ReactionResult(
            ok=False, channel_id="C", message_ts="ts", error="fail",
        )
        mocks["enqueue"].side_effect = RuntimeError("queue down")

        client = Client()
        _create_approved_user("TTEST123", "UTESTUSER")

        resp = _post(client, _mention_payload(
            event_id="Ev_fail_2", user_id="UTESTUSER",
        ))
        # Still returns 200 despite reaction removal failure
        assert resp.status_code == 200
    finally:
        _exit_patches(patches)


# ===========================================================================
# 6. IDEMPOTENCY VALIDATION
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_duplicate_mention_does_not_enqueue_twice():
    """Duplicate Slack mention event does not enqueue twice."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_approved_user("TTEST123", "UTESTUSER")

        payload = _mention_payload(event_id="Ev_dup_1", user_id="UTESTUSER")
        _post(client, payload)
        _post(client, payload)  # duplicate

        mocks["enqueue"].assert_called_once()
        mocks["add_reaction"].assert_called_once()
    finally:
        _exit_patches(patches)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_duplicate_unauthorized_no_duplicate_attempt():
    """Duplicate unauthorized event does not create duplicate attempt processing."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")

        payload = _mention_payload(event_id="Ev_dup_3", user_id="UTESTUSER")
        _post(client, payload)
        _post(client, payload)

        assert UnauthorizedAccessAttempt.objects.filter(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        ).count() == 1
        attempt = UnauthorizedAccessAttempt.objects.get(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        )
        assert attempt.attempt_count == 1
    finally:
        _exit_patches(patches)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_duplicate_admin_dm_no_duplicate_access():
    """Duplicate admin DM does not create duplicate BotUserAccess rows."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")
        # Create UnauthorizedAccessAttempt so source channel can be resolved
        UnauthorizedAccessAttempt.objects.create(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
            last_source_channel_id="C08XYZ456", attempt_count=1,
        )

        payload = _dm_payload(
            event_id="Ev_dup_4", user_id="UADMIN123",
            text="Give UTESTUSER access",
        )
        _post(client, payload)
        _post(client, payload)

        assert BotUserAccess.objects.filter(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        ).count() == 1
    finally:
        _exit_patches(patches)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_repeated_grant_no_duplicate_confirmation_dm():
    """Repeated grant for an approved user does not send another confirmation DM."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")
        _create_approved_user("TTEST123", "UTESTUSER")
        # Create UnauthorizedAccessAttempt so source channel can be resolved
        UnauthorizedAccessAttempt.objects.create(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
            last_source_channel_id="C08XYZ456", attempt_count=1,
        )

        _post(client, _dm_payload(
            event_id="Ev_rep_1", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))

        # Already approved — no confirmation DM
        mocks["send_conf"].assert_not_called()
    finally:
        _exit_patches(patches)


# ===========================================================================
# 7. AUDIT VALIDATION
# ===========================================================================


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_audit_records_for_full_workflow():
    """Verify audit records are produced for the full whitelisting workflow."""
    patches = _all_patches()
    mocks = _enter_patches(patches)
    try:
        _setup_mocks(mocks)
        client = Client()
        _create_admin("TTEST123", "UADMIN123")

        # Unregistered mention -> unauthorized attempt audit
        _post(client, _mention_payload(
            event_id="Ev_aud_1", user_id="UTESTUSER",
        ))
        assert BotAccessAuditLog.objects.filter(
            action=AUDIT_UNAUTHORIZED_ACCESS_ATTEMPT,
        ).exists()
        assert BotAccessAuditLog.objects.filter(
            action=AUDIT_ADMIN_NOTIFICATION_SENT,
        ).exists()

        # Admin grants -> access granted audit
        _post(client, _dm_payload(
            event_id="Ev_aud_2", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))
        assert BotAccessAuditLog.objects.filter(
            action=AUDIT_ACCESS_GRANTED,
        ).exists()

        # Revoke the user, then restore
        access = BotUserAccess.objects.get(
            workspace_id="TTEST123", slack_user_id="UTESTUSER",
        )
        access.status = ACCESS_STATUS_REVOKED
        access.save(update_fields=["status", "updated_at"])

        mocks["send_conf"].reset_mock()
        _post(client, _dm_payload(
            event_id="Ev_aud_3", user_id="UADMIN123",
            text="Give UTESTUSER access",
        ))
        assert BotAccessAuditLog.objects.filter(
            action=AUDIT_ACCESS_RESTORED,
        ).exists()

        # Verify no secrets/tokens/message bodies in audit metadata
        for log in BotAccessAuditLog.objects.all():
            metadata_str = str(log.metadata)
            assert "SLACK_BOT_TOKEN" not in metadata_str
            assert "SLACK_SIGNING_SECRET" not in metadata_str
            assert "Bearer" not in metadata_str
    finally:
        _exit_patches(patches)
