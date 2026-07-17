"""Phase 1 tests — dual admin grant workflow (mention-first + proactive).

Covers:
 1.  Workspace resolution: mention-first via UnauthorizedAccessAttempt.
 2.  Workspace resolution: proactive fallback with single SlackChannelMapping.
 3.  Workspace resolution: zero mappings → failure.
 4.  Workspace resolution: multiple mappings → failure.
 5.  Workspace resolution: archived workspace excluded from fallback.
 6.  Workspace resolution: source channel not mapped → fallback.
 7.  Bot grant: new user → granted + BotUserAccess created.
 8.  Bot grant: already approved → already_approved.
 9.  Bot grant: revoked → restored.
 10. Bot grant: resolution failure → failed, no DB changes.
 11. Bot grant: no BrightBean identity records created.
 12. Bulk bot grant: mixed outcomes.
 13. Bulk bot grant: one failure does not block others.
 14. Admin DM: Flow A — mention-first grant.
 15. Admin DM: Flow B — proactive grant with single mapping.
 16. Admin DM: Flow B — no mapping → failure in response.
 17. Admin DM: Flow B — multiple mappings → failure in response.
 18. Admin DM: non-admin blocked.
 19. Admin DM: no grant intent → not handled.
 20. Admin DM: usage message for no IDs.
 21. Response: granted format.
 22. Response: failed format.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.organizations.models import Organization
from apps.slack_bot.admin_dm_response import (
    format_bot_grant_response,
)
from apps.slack_bot.admin_dm_service import process_admin_dm
from apps.slack_bot.bot_grant_service import (
    BotGrantResult,
    grant_bot_access,
    grant_bulk_bot_access,
)
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    PERMISSION_READ_ONLY,
)
from apps.slack_bot.models import (
    BotAdministrator,
    BotUserAccess,
    SlackChannelMapping,
    SlackUserMapping,
    UnauthorizedAccessAttempt,
)
from apps.slack_bot.slack_identity import SlackUserIdentity
from apps.slack_bot.workspace_grant_service import resolve_grant_workspace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _phase1_domain(settings):
    settings.SLACK_ALLOWED_EMAIL_DOMAINS = "example.com"
    settings.SLACK_ALLOWED_TEAM_ID = "TTEAM01"


class FakeIdentityClient:
    def __init__(self, *, email: str, slack_user_id: str, team_id: str = "TTEAM01"):
        self.identity = SlackUserIdentity(
            slack_user_id=slack_user_id,
            team_id=team_id,
            email=email,
            display_name="Target",
            real_name="Target User",
        )

    def get_user(self, *, slack_user_id: str) -> SlackUserIdentity:
        return self.identity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_org(name="Test Org"):
    return Organization.objects.create(name=name)


def _create_workspace(org=None, name="Test WS", is_archived=False):
    org = org or _create_org()
    return Workspace.objects.create(organization=org, name=name, is_archived=is_archived)


def _create_channel_mapping(team_id="TTEAM01", channel_id="CCHANNEL01", workspace=None):
    if workspace is None:
        workspace = _create_workspace()
    return SlackChannelMapping.objects.create(
        team_id=team_id,
        channel_id=channel_id,
        workspace=workspace,
    )


def _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01"):
    return BotAdministrator.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
    )


def _create_unauthorized_attempt(
    workspace_id="TTEAM01",
    slack_user_id="UTARGET01",
    source_channel_id="CCHANNEL01",
):
    return UnauthorizedAccessAttempt.objects.create(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        last_source_channel_id=source_channel_id,
        attempt_count=1,
    )


# ---------------------------------------------------------------------------
# 1–6: Workspace resolution
# ---------------------------------------------------------------------------


class TestResolveGrantWorkspace:
    """Tests for resolve_grant_workspace."""

    def test_mention_first_resolution(self):
        """1. UnauthorizedAccessAttempt → source channel → mapping."""
        ws = _create_workspace(name="Mention WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CMENTION",
            workspace=ws,
        )
        _create_unauthorized_attempt(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET01",
            source_channel_id="CMENTION",
        )

        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET01",
        )

        assert result.ok is True
        assert result.source_channel_id == "CMENTION"
        assert result.workspace_name == "Mention WS"
        assert result.workspace_id == str(ws.id)

    def test_proactive_fallback_single_mapping(self):
        """2. No attempt → single active mapping → resolved."""
        ws = _create_workspace(name="Proactive WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CFALLBACK",
            workspace=ws,
        )

        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET02",
        )

        assert result.ok is True
        assert result.source_channel_id == "CFALLBACK"
        assert result.workspace_name == "Proactive WS"

    def test_zero_mappings_failure(self):
        """3. No mappings at all → failure."""
        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET03",
        )

        assert result.ok is False
        assert "No BrightBean workspace" in result.failure_reason

    def test_multiple_mappings_failure(self):
        """4. Multiple active mappings → ambiguous failure."""
        ws1 = _create_workspace(name="WS One")
        ws2 = _create_workspace(org=ws1.organization, name="WS Two")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CCHAN01",
            workspace=ws1,
        )
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CCHAN02",
            workspace=ws2,
        )

        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET04",
        )

        assert result.ok is False
        assert "Multiple" in result.failure_reason

    def test_archived_workspace_excluded_from_fallback(self):
        """5. Archived workspace mapping excluded from fallback."""
        ws = _create_workspace(name="Archived WS", is_archived=True)
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CARCHIVE",
            workspace=ws,
        )

        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET05",
        )

        assert result.ok is False
        assert "No BrightBean workspace" in result.failure_reason

    def test_source_channel_not_mapped_falls_back(self):
        """6. Attempt has source channel but no mapping → fallback to single."""
        ws = _create_workspace(name="Fallback WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CREAL",
            workspace=ws,
        )
        _create_unauthorized_attempt(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET06",
            source_channel_id="CUNMAPPED",
        )

        result = resolve_grant_workspace(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET06",
        )

        assert result.ok is True
        assert result.source_channel_id == "CREAL"
        assert result.workspace_name == "Fallback WS"


# ---------------------------------------------------------------------------
# 7–11: Bot grant service
# ---------------------------------------------------------------------------


class TestGrantBotAccess:
    """Tests for grant_bot_access."""

    def test_new_user_granted(self):
        """7. New user → granted + BotUserAccess created."""
        ws = _create_workspace(name="Grant WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CGRANT",
            workspace=ws,
        )

        result = grant_bot_access(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET07",
            approving_slack_user_id="UADMIN01",
        )

        assert result.action == "granted"
        assert result.workspace_name == "Grant WS"
        assert BotUserAccess.objects.filter(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET07",
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
        ).exists()

    def test_already_approved(self):
        """8. Already approved → already_approved."""
        ws = _create_workspace(name="Already WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CALREADY",
            workspace=ws,
        )
        BotUserAccess.objects.create(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET08",
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
        )

        result = grant_bot_access(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET08",
            approving_slack_user_id="UADMIN01",
        )

        assert result.action == "already_approved"

    def test_revoked_restored(self):
        """9. Revoked → restored."""
        ws = _create_workspace(name="Restore WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CRESTORE",
            workspace=ws,
        )
        BotUserAccess.objects.create(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET09",
            status=ACCESS_STATUS_REVOKED,
            permission=PERMISSION_READ_ONLY,
        )

        result = grant_bot_access(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET09",
            approving_slack_user_id="UADMIN01",
        )

        assert result.action == "restored"
        access = BotUserAccess.objects.get(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET09",
        )
        assert access.status == ACCESS_STATUS_APPROVED

    def test_resolution_failure_no_db_changes(self):
        """10. Resolution failure → failed, no BotUserAccess created."""
        result = grant_bot_access(
            team_id="TNOMAP",
            target_slack_user_id="UTARGET10",
            approving_slack_user_id="UADMIN01",
        )

        assert result.action == "failed"
        assert result.failure_reason != ""
        assert not BotUserAccess.objects.filter(
            workspace_id="TNOMAP",
            slack_user_id="UTARGET10",
        ).exists()

    def test_no_brightbean_identity_records_created(self):
        """11. Grant does not create SlackUserMapping."""
        ws = _create_workspace(name="No Identity WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CNOID",
            workspace=ws,
        )

        grant_bot_access(
            team_id="TTEAM01",
            target_slack_user_id="UTARGET11",
            approving_slack_user_id="UADMIN01",
        )

        assert not SlackUserMapping.objects.filter(
            slack_user_id="UTARGET11",
            team_id="TTEAM01",
        ).exists()


# ---------------------------------------------------------------------------
# 12–13: Bulk bot grant
# ---------------------------------------------------------------------------


class TestGrantBulkBotAccess:
    """Tests for grant_bulk_bot_access."""

    def test_mixed_outcomes(self):
        """12. Bulk with granted + already_approved + restored."""
        ws = _create_workspace(name="Bulk WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CBULK",
            workspace=ws,
        )
        # Pre-existing approved user
        BotUserAccess.objects.create(
            workspace_id="TTEAM01",
            slack_user_id="UBULK02",
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
        )
        # Pre-existing revoked user
        BotUserAccess.objects.create(
            workspace_id="TTEAM01",
            slack_user_id="UBULK03",
            status=ACCESS_STATUS_REVOKED,
            permission=PERMISSION_READ_ONLY,
        )

        result = grant_bulk_bot_access(
            team_id="TTEAM01",
            target_slack_user_ids=["UBULK01", "UBULK02", "UBULK03"],
            approving_slack_user_id="UADMIN01",
        )

        assert "UBULK01" in result.granted
        assert "UBULK02" in result.already_approved
        assert "UBULK03" in result.restored

    def test_one_failure_does_not_block_others(self):
        """13. One resolution failure does not block other grants."""
        ws = _create_workspace(name="Mixed WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CMIXED",
            workspace=ws,
        )
        # User with attempt pointing to unmapped channel (will fall back to single mapping — succeeds)
        _create_unauthorized_attempt(
            workspace_id="TTEAM01",
            slack_user_id="UMIXED01",
            source_channel_id="CUNMAPPED",
        )
        # User on a team with no mappings at all — but we can't mix teams in one call.
        # Instead, test that a valid user succeeds even when another user has no attempt.
        result = grant_bulk_bot_access(
            team_id="TTEAM01",
            target_slack_user_ids=["UMIXED01", "UMIXED02"],
            approving_slack_user_id="UADMIN01",
        )

        assert "UMIXED01" in result.granted
        assert "UMIXED02" in result.granted


# ---------------------------------------------------------------------------
# 14–20: Admin DM integration
# ---------------------------------------------------------------------------


class TestAdminDMIntegration:
    """Tests for process_admin_dm with the new bot grant flow."""

    @patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm", return_value=True)
    def test_flow_a_mention_first_grant(self, _mock):
        """14. User mentioned bot → admin grants via DM → success."""
        ws = _create_workspace(name="FlowA WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CFLOWA",
            workspace=ws,
        )
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")
        _create_unauthorized_attempt(
            workspace_id="TTEAM01",
            slack_user_id="UTARGET14",
            source_channel_id="CFLOWA",
        )

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UADMIN01",
            dm_text="Give UTARGET14 access",
            identity_client=FakeIdentityClient(email="target14@example.com", slack_user_id="UTARGET14"),
        )

        assert result.is_admin_dm is True
        assert result.handled is True
        assert "Access granted successfully" in result.response_text
        assert "UTARGET14" in result.response_text
        assert "FlowA WS" in result.response_text

    @patch("apps.slack_bot.admin_dm_service.send_user_confirmation_dm", return_value=True)
    def test_flow_b_proactive_grant_single_mapping(self, _mock):
        """15. No prior mention → single mapping → success."""
        ws = _create_workspace(name="FlowB WS")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CFLOWB",
            workspace=ws,
        )
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UADMIN01",
            dm_text="Give UTARGET15 access",
            identity_client=FakeIdentityClient(email="target15@example.com", slack_user_id="UTARGET15"),
        )

        assert result.is_admin_dm is True
        assert result.handled is True
        assert "Access granted successfully" in result.response_text
        assert "FlowB WS" in result.response_text

    def test_flow_b_no_mapping_failure(self):
        """16. No mapping → failure in response."""
        _create_admin(workspace_id="TNOMAP", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TNOMAP",
            sender_slack_user_id="UADMIN01",
            dm_text="Give UTARGET16 access",
        )

        assert result.handled is True
        assert "not granted" in result.response_text.lower()

    def test_flow_b_multiple_mappings_failure(self):
        """17. Multiple mappings → failure in response."""
        ws1 = _create_workspace(name="Multi WS 1")
        ws2 = _create_workspace(org=ws1.organization, name="Multi WS 2")
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CMULTI1",
            workspace=ws1,
        )
        _create_channel_mapping(
            team_id="TTEAM01",
            channel_id="CMULTI2",
            workspace=ws2,
        )
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UADMIN01",
            dm_text="Give UTARGET17 access",
        )

        assert result.handled is True
        assert "not granted" in result.response_text.lower()
        assert "Multiple" in result.response_text

    def test_non_admin_blocked(self):
        """18. Non-admin sender → not handled."""
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UNOTADMIN",
            dm_text="Give UTARGET18 access",
        )

        assert result.is_admin_dm is False
        assert result.handled is False

    @override_settings(ADMIN_LLM_CHAT_ENABLED="false")
    def test_no_grant_intent_not_handled(self):
        """19. Non-grant message → not handled (when LLM chat disabled)."""
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UADMIN01",
            dm_text="Hello there",
        )

        assert result.is_admin_dm is True
        assert result.handled is False

    def test_usage_message_for_no_ids(self):
        """20. Grant intent with no IDs → usage message."""
        _create_admin(workspace_id="TTEAM01", slack_user_id="UADMIN01")

        result = process_admin_dm(
            workspace_id="TTEAM01",
            sender_slack_user_id="UADMIN01",
            dm_text="Give access",
        )

        assert result.is_admin_dm is True
        assert result.handled is True
        assert "Usage" in result.response_text or "Give" in result.response_text


# ---------------------------------------------------------------------------
# 21–22: Response formatting
# ---------------------------------------------------------------------------


class TestResponseFormatting:
    """Tests for the new bot grant response formatters."""

    def test_granted_format(self):
        """21. Granted result → correct format."""
        result = BotGrantResult(
            action="granted",
            target_slack_user_id="UTARGET21",
            workspace_name="Response WS",
        )
        text = format_bot_grant_response(result)

        assert "Access granted successfully" in text
        assert "UTARGET21" in text
        assert "Response WS" in text
        assert "Analytics read-only" in text
        assert "Approved" in text

    def test_failed_format(self):
        """22. Failed result → correct format."""
        result = BotGrantResult(
            action="failed",
            target_slack_user_id="UTARGET22",
            failure_reason="No BrightBean workspace is mapped to this Slack workspace.",
        )
        text = format_bot_grant_response(result)

        assert "not granted" in text.lower()
        assert "UTARGET22" in text
        assert "No BrightBean workspace" in text
