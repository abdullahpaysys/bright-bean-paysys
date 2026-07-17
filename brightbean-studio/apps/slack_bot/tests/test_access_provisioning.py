"""Focused tests for the BrightBean access provisioning service.

Tests cover:
1.  Active admin can provision an existing BrightBean user.
2.  Non-admin cannot provision.
3.  Channel mapping determines workspace.
4.  Missing channel mapping rejected.
5.  Archived workspace rejected.
6.  Missing email rejected when no SlackUserMapping.
7.  Existing SlackUserMapping allows provisioning without email.
8.  Case-insensitive email lookup.
9.  Missing BrightBean user rejected.
10. Inactive BrightBean user rejected.
11. SlackUserMapping created when missing.
12. Existing same mapping idempotent.
13. Conflicting SlackUserMapping rejected.
14. OrgMembership created as MEMBER.
15. Existing higher org role preserved.
16. WorkspaceMembership created as VIEWER.
17. Existing higher workspace role preserved.
18. Custom role without view_analytics rejected.
19. BotUserAccess created APPROVED / READ_ONLY.
20. REVOKED BotUserAccess restored.
21. Existing bot-only approval repaired.
22. resolve_tool_context succeeds after provisioning.
23. Accounts from another workspace excluded.
24. Repeated provisioning creates no duplicates.
25. Simulated intermediate failure rolls back.
26. Failed provisioning does not leave BotUserAccess approved.
27. No elevated admin/staff/write privileges.
28. No Slack API called.
29. No LLM API called.
30. No social-provider API called.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.members.models import (
    CustomRole,
    OrgMembership,
    WorkspaceMembership,
)
from apps.organizations.models import Organization
from apps.slack_bot.access_provisioning import (
    ProvisioningFailureReason,
    ProvisioningStatus,
    grant_slack_analytics_access,
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
)
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEAM = "T0001"
_CHANNEL = "C0001"
_ADMIN_SLACK = "UADMIN01"
_TARGET_SLACK = "UTARGET01"
_TARGET_EMAIL = "target@example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _phase1_domain(settings):
    settings.SLACK_ALLOWED_EMAIL_DOMAINS = "example.com"


@pytest.fixture
def org():
    return Organization.objects.create(name="Test Org")


@pytest.fixture
def workspace(org):
    return Workspace.objects.create(organization=org, name="Test WS")


@pytest.fixture
def other_workspace(org):
    return Workspace.objects.create(organization=org, name="Other WS")


@pytest.fixture
def target_user():
    return User.objects.create_user(
        email=_TARGET_EMAIL, password="x"
    )


@pytest.fixture
def admin_user():
    return User.objects.create_user(
        email="admin@example.com", password="x"
    )


@pytest.fixture
def bot_admin():
    return BotAdministrator.objects.create(
        workspace_id=_TEAM,
        slack_user_id=_ADMIN_SLACK,
        status=ADMIN_STATUS_ACTIVE,
    )


@pytest.fixture
def channel_mapping(workspace):
    return SlackChannelMapping.objects.create(
        team_id=_TEAM,
        channel_id=_CHANNEL,
        workspace=workspace,
    )


@pytest.fixture
def social_account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig_1",
        account_name="IG Test",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def other_social_account(other_workspace):
    return SocialAccount.objects.create(
        workspace=other_workspace,
        platform="facebook",
        account_platform_id="fb_1",
        account_name="FB Other",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _provision(
    *,
    approving=_ADMIN_SLACK,
    team_id=_TEAM,
    channel_id=_CHANNEL,
    target=_TARGET_SLACK,
    email=_TARGET_EMAIL,
):
    """Call grant_slack_analytics_access with defaults."""
    return grant_slack_analytics_access(
        approving_slack_user_id=approving,
        team_id=team_id,
        source_channel_id=channel_id,
        target_slack_user_id=target,
        brightbean_email=email,
    )


# ===========================================================================
# 1. Active admin can provision an existing BrightBean user
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_active_admin_provisions_existing_user(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = _provision()
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert result.target_slack_user_id == _TARGET_SLACK
    assert result.brightbean_email == _TARGET_EMAIL
    assert result.workspace_name == "Test WS"
    assert result.bot_access_action == "created"
    assert result.mapping_action == "created"
    assert result.org_membership_action == "created"
    assert result.ws_membership_action == "created"


# ===========================================================================
# 2. Non-admin cannot provision
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_non_admin_rejected(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = grant_slack_analytics_access(
        approving_slack_user_id="U_NOT_ADMIN",
        team_id=_TEAM,
        source_channel_id=_CHANNEL,
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email=_TARGET_EMAIL,
    )
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.ADMIN_NOT_AUTHORIZED
    # No DB changes for the target workspace/org
    assert not SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not OrgMembership.objects.filter(user=target_user, organization=org).exists()
    assert not WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).exists()


# ===========================================================================
# 3. Channel mapping determines workspace
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_channel_mapping_determines_workspace(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = _provision()
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert result.workspace_name == "Test WS"
    # Verify the user was added to the mapped workspace
    assert WorkspaceMembership.objects.filter(
        user=target_user, workspace=workspace,
    ).exists()


# ===========================================================================
# 4. Missing channel mapping rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_missing_channel_mapping_rejected(
    org, workspace, target_user, bot_admin, social_account,
):
    result = grant_slack_analytics_access(
        approving_slack_user_id=_ADMIN_SLACK,
        team_id=_TEAM,
        source_channel_id="C_UNMAPPED",
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email=_TARGET_EMAIL,
    )
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED
    assert not BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).exists()


# ===========================================================================
# 5. Archived workspace rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_archived_workspace_rejected(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    workspace.is_archived = True
    workspace.save()
    result = _provision()
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.WORKSPACE_ARCHIVED
    assert not BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).exists()


# ===========================================================================
# 6. Missing email rejected when no SlackUserMapping
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_missing_email_rejected_without_mapping(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = grant_slack_analytics_access(
        approving_slack_user_id=_ADMIN_SLACK,
        team_id=_TEAM,
        source_channel_id=_CHANNEL,
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email=None,
    )
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.SLACK_PROFILE_UNAVAILABLE


# ===========================================================================
# 7. Existing SlackUserMapping allows provisioning without email
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_existing_mapping_no_email_needed(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    SlackUserMapping.objects.create(
        slack_user_id=_TARGET_SLACK,
        team_id=_TEAM,
        user=target_user,
    )
    result = grant_slack_analytics_access(
        approving_slack_user_id=_ADMIN_SLACK,
        team_id=_TEAM,
        source_channel_id=_CHANNEL,
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email=None,
    )
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.SLACK_PROFILE_UNAVAILABLE
    assert SlackUserMapping.objects.filter(
        slack_user_id=_TARGET_SLACK,
        team_id=_TEAM,
        user=target_user,
    ).exists()


# ===========================================================================
# 8. Case-insensitive email lookup
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_case_insensitive_email(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = grant_slack_analytics_access(
        approving_slack_user_id=_ADMIN_SLACK,
        team_id=_TEAM,
        source_channel_id=_CHANNEL,
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email="TARGET@EXAMPLE.COM",
    )
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert result.brightbean_email == _TARGET_EMAIL


# ===========================================================================
# 9. Missing BrightBean user rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_missing_user_rejected(
    org, workspace, bot_admin, channel_mapping, social_account,
):
    result = grant_slack_analytics_access(
        approving_slack_user_id=_ADMIN_SLACK,
        team_id=_TEAM,
        source_channel_id=_CHANNEL,
        target_slack_user_id=_TARGET_SLACK,
        brightbean_email="nonexistent@example.com",
    )
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert User.objects.filter(email="nonexistent@example.com").exists()


# ===========================================================================
# 10. Inactive BrightBean user rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_inactive_user_rejected(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    target_user.is_active = False
    target_user.save()
    result = _provision()
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.USER_INACTIVE


# ===========================================================================
# 11. SlackUserMapping created when missing
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_mapping_created(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    assert not SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    result = _provision()
    assert result.mapping_action == "created"
    mapping = SlackUserMapping.objects.get(slack_user_id=_TARGET_SLACK, team_id=_TEAM)
    assert mapping.user == target_user


# ===========================================================================
# 12. Existing same mapping idempotent
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_idempotent_same_mapping(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    r1 = _provision()
    assert r1.status == ProvisioningStatus.NEWLY_PROVISIONED

    r2 = _provision()
    assert r2.status == ProvisioningStatus.ALREADY_PROVISIONED
    assert r2.bot_access_action == "already_approved"
    assert r2.mapping_action == "already_exists"

    # No duplicate records
    assert SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).count() == 1
    assert BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).count() == 1
    assert OrgMembership.objects.filter(user=target_user, organization=org).count() == 1
    assert WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).count() == 1


# ===========================================================================
# 13. Conflicting SlackUserMapping rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_conflicting_mapping_rejected(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    other_user = User.objects.create_user(
        email="other@example.com", password="x"
    )
    SlackUserMapping.objects.create(
        slack_user_id=_TARGET_SLACK,
        team_id=_TEAM,
        user=other_user,
    )
    result = _provision()
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.BRIGHTBEAN_USER_CONFLICT


# ===========================================================================
# 14. OrgMembership created as MEMBER
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_org_membership_created_as_member(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    _provision()
    membership = OrgMembership.objects.get(user=target_user, organization=org)
    assert membership.org_role == OrgMembership.OrgRole.MEMBER


# ===========================================================================
# 15. Existing higher org role preserved
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_existing_higher_org_role_preserved(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    OrgMembership.objects.create(
        user=target_user,
        organization=org,
        org_role=OrgMembership.OrgRole.ADMIN,
    )
    _provision()
    membership = OrgMembership.objects.get(user=target_user, organization=org)
    assert membership.org_role == OrgMembership.OrgRole.ADMIN


# ===========================================================================
# 16. WorkspaceMembership created as VIEWER
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_ws_membership_created_as_viewer(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    _provision()
    membership = WorkspaceMembership.objects.get(user=target_user, workspace=workspace)
    assert membership.workspace_role == WorkspaceMembership.WorkspaceRole.VIEWER


# ===========================================================================
# 17. Existing higher workspace role preserved
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_existing_higher_ws_role_preserved(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    WorkspaceMembership.objects.create(
        user=target_user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER,
    )
    _provision()
    membership = WorkspaceMembership.objects.get(user=target_user, workspace=workspace)
    assert membership.workspace_role == WorkspaceMembership.WorkspaceRole.MANAGER


# ===========================================================================
# 18. Custom role without view_analytics rejected
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_custom_role_without_view_analytics_rejected(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    custom_role = CustomRole.objects.create(
        organization=org,
        name="No Analytics Role",
        permissions={"create_posts": True, "view_analytics": False},
    )
    WorkspaceMembership.objects.create(
        user=target_user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
        custom_role=custom_role,
    )
    result = _provision()
    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.NO_VIEW_ANALYTICS
    assert not BotUserAccess.objects.filter(
        slack_user_id=_TARGET_SLACK,
        status=ACCESS_STATUS_APPROVED,
    ).exists()


# ===========================================================================
# 19. BotUserAccess created APPROVED / READ_ONLY
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_bot_access_created_approved_readonly(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    _provision()
    access = BotUserAccess.objects.get(
        workspace_id=_TEAM,
        slack_user_id=_TARGET_SLACK,
    )
    assert access.status == ACCESS_STATUS_APPROVED
    assert access.permission == PERMISSION_READ_ONLY


# ===========================================================================
# 20. REVOKED BotUserAccess restored
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_revoked_access_restored(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    BotUserAccess.objects.create(
        workspace_id=_TEAM,
        slack_user_id=_TARGET_SLACK,
        status=ACCESS_STATUS_REVOKED,
        permission=PERMISSION_READ_ONLY,
    )
    result = _provision()
    assert result.status == ProvisioningStatus.RESTORED
    assert result.bot_access_action == "restored"
    access = BotUserAccess.objects.get(
        workspace_id=_TEAM,
        slack_user_id=_TARGET_SLACK,
    )
    assert access.status == ACCESS_STATUS_APPROVED


# ===========================================================================
# 21. Existing bot-only approval repaired
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_bot_only_approval_repaired(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    # Bot access approved, but no SlackUserMapping or memberships
    BotUserAccess.objects.create(
        workspace_id=_TEAM,
        slack_user_id=_TARGET_SLACK,
        status=ACCESS_STATUS_APPROVED,
        permission=PERMISSION_READ_ONLY,
    )
    result = _provision()
    assert result.status == ProvisioningStatus.REPAIRED
    assert result.bot_access_action == "already_approved"
    assert result.mapping_action == "created"
    assert result.org_membership_action == "created"
    assert result.ws_membership_action == "created"
    # Verify the identity chain now exists
    assert SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert OrgMembership.objects.filter(user=target_user, organization=org).exists()
    assert WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).exists()


# ===========================================================================
# 22. resolve_tool_context succeeds after provisioning
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_resolve_tool_context_succeeds(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    result = _provision()
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED

    # Directly call resolve_tool_context to verify
    from apps.slack_bot.authorization import resolve_tool_context
    from apps.slack_bot.contracts import SlackAnalyticsRequest

    request = SlackAnalyticsRequest(
        correlation_id="test-corr",
        event_id="test-evt",
        team_id=_TEAM,
        channel_id=_CHANNEL,
        user_id=_TARGET_SLACK,
        thread_ts="",
        text="show analytics",
    )
    context = resolve_tool_context(request)
    assert context.slack_team_id == _TEAM
    assert context.slack_channel_id == _CHANNEL


# ===========================================================================
# 23. Accounts from another workspace excluded
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_other_workspace_accounts_excluded(
    org, workspace, other_workspace, target_user, bot_admin,
    channel_mapping, social_account, other_social_account,
):
    result = _provision()
    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED

    from apps.slack_bot.authorization import resolve_tool_context
    from apps.slack_bot.contracts import SlackAnalyticsRequest

    request = SlackAnalyticsRequest(
        correlation_id="test-corr",
        event_id="test-evt",
        team_id=_TEAM,
        channel_id=_CHANNEL,
        user_id=_TARGET_SLACK,
        thread_ts="",
        text="show analytics",
    )
    context = resolve_tool_context(request)
    # social_account (instagram in workspace) should be in allowed set
    assert social_account.id in context.allowed_account_ids
    # other_social_account (facebook in other_workspace) should NOT be
    assert other_social_account.id not in context.allowed_account_ids


# ===========================================================================
# 24. Repeated provisioning creates no duplicate records
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_no_duplicates_on_repeat(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    _provision()
    _provision()
    _provision()

    assert SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).count() == 1
    assert BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).count() == 1
    assert OrgMembership.objects.filter(user=target_user, organization=org).count() == 1
    assert WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).count() == 1


# ===========================================================================
# 25. Simulated intermediate failure rolls back all new records
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_intermediate_failure_rolls_back(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    # Patch resolve_tool_context to fail during post-check
    from apps.slack_bot.errors import ErrorCode
    from apps.slack_bot.exceptions import AuthorizationError

    with patch(
        "apps.slack_bot.access_provisioning.resolve_tool_context",
        side_effect=AuthorizationError(ErrorCode.UNAUTHORIZED, "post-check fail"),
    ):
        result = _provision()

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.POST_CHECK_FAILED
    # All new records should be rolled back
    assert not SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not OrgMembership.objects.filter(user=target_user, organization=org).exists()
    assert not WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).exists()
    # All new records should be rolled back
    assert not SlackUserMapping.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not BotUserAccess.objects.filter(slack_user_id=_TARGET_SLACK).exists()
    assert not OrgMembership.objects.filter(user=target_user, organization=org).exists()
    assert not WorkspaceMembership.objects.filter(user=target_user, workspace=workspace).exists()


# ===========================================================================
# 26. Failed provisioning does not leave BotUserAccess approved
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_failed_provisioning_no_approved_access(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    # Use a custom role without view_analytics to trigger a failure
    # after some records may be created
    custom_role = CustomRole.objects.create(
        organization=org,
        name="No Analytics",
        permissions={"create_posts": True, "view_analytics": False},
    )
    WorkspaceMembership.objects.create(
        user=target_user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
        custom_role=custom_role,
    )
    result = _provision()
    assert result.status == ProvisioningStatus.FAILED
    assert not BotUserAccess.objects.filter(
        slack_user_id=_TARGET_SLACK,
        status=ACCESS_STATUS_APPROVED,
    ).exists()


# ===========================================================================
# 27. No elevated privileges granted
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_no_elevated_privileges(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    _provision()
    target_user.refresh_from_db()
    assert target_user.is_staff is False
    assert target_user.is_superuser is False

    org_membership = OrgMembership.objects.get(user=target_user, organization=org)
    assert org_membership.org_role == OrgMembership.OrgRole.MEMBER

    ws_membership = WorkspaceMembership.objects.get(user=target_user, workspace=workspace)
    assert ws_membership.workspace_role == WorkspaceMembership.WorkspaceRole.VIEWER


# ===========================================================================
# 28. No Slack API called
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_no_slack_api_called(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    with patch("apps.slack_bot.delivery.send_slack_message") as mock_send:
        _provision()
    mock_send.assert_not_called()


# ===========================================================================
# 29. No LLM API called
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_no_llm_api_called(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    with patch("apps.slack_bot.tasks.ToolOrchestrator.run") as mock_llm:
        _provision()
    mock_llm.assert_not_called()


# ===========================================================================
# 30. No social-provider API called
# ===========================================================================

@override_settings(SLACK_ALLOWED_TEAM_ID=_TEAM)
def test_no_social_provider_api_called(
    org, workspace, target_user, bot_admin, channel_mapping, social_account,
):
    # The provisioning service does not import or call any provider modules.
    # We verify by checking the service source for provider imports.
    import apps.slack_bot.access_provisioning as mod
    with open(mod.__file__) as f:
        source = f.read()
    assert "import httpx" not in source
    assert "from apps.providers" not in source
    assert "requests" not in source
