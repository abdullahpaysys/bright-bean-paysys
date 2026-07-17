from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.slack_bot.access_provisioning import (
    ProvisioningFailureReason,
    ProvisioningStatus,
    grant_slack_analytics_access,
    revoke_slack_analytics_access,
)
from apps.slack_bot.admin_dm_service import process_admin_dm
from apps.slack_bot.constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
)
from apps.slack_bot.models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    SlackChannelMapping,
    SlackUserMapping,
)
from apps.slack_bot.slack_identity import SlackUserIdentity
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

TEAM = "TTEAM01"
CHANNEL = "CCHANNEL01"
ADMIN = "UADMIN01"
TARGET = "UTARGET01"
POSTGRES_ONLY_REASON = "Requires PostgreSQL transaction and row-lock semantics"


@dataclass
class FakeIdentityClient:
    identity: SlackUserIdentity | None = None
    error: Exception | None = None

    def get_user(self, *, slack_user_id: str) -> SlackUserIdentity:
        if self.error:
            raise self.error
        assert self.identity is not None
        return self.identity


@pytest.fixture(autouse=True)
def phase1_settings(settings):
    settings.SLACK_ALLOWED_EMAIL_DOMAINS = "example.com"
    settings.SLACK_ALLOWED_TEAM_ID = TEAM


def _identity(
    *,
    slack_user_id: str = TARGET,
    team_id: str = TEAM,
    email: str = "target@example.com",
    is_bot: bool = False,
    is_deleted: bool = False,
    is_guest: bool = False,
) -> SlackUserIdentity:
    return SlackUserIdentity(
        slack_user_id=slack_user_id,
        team_id=team_id,
        email=email,
        display_name="Target",
        real_name="Target User",
        is_bot=is_bot,
        is_deleted=is_deleted,
        is_guest=is_guest,
    )


def _run_in_thread(fn):
    close_old_connections()
    try:
        return fn()
    finally:
        close_old_connections()


def _wait_for_overlap(barrier: threading.Barrier) -> None:
    barrier.wait(timeout=10)


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="BrightBean")
    return Workspace.objects.create(organization=org, name="Company Workspace")


@pytest.fixture
def channel_mapping(workspace):
    return SlackChannelMapping.objects.create(
        team_id=TEAM,
        channel_id=CHANNEL,
        workspace=workspace,
    )


@pytest.fixture
def bot_admin():
    return BotAdministrator.objects.create(
        workspace_id=TEAM,
        slack_user_id=ADMIN,
        status=ADMIN_STATUS_ACTIVE,
    )


def test_grant_valid_company_user_creates_full_access_chain(
    workspace,
    channel_mapping,
    bot_admin,
):
    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    user = User.objects.get(email="target@example.com")
    assert not user.has_usable_password()
    assert OrgMembership.objects.filter(
        user=user,
        organization=workspace.organization,
        org_role=OrgMembership.OrgRole.MEMBER,
    ).exists()
    assert WorkspaceMembership.objects.filter(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
    ).exists()
    assert SlackUserMapping.objects.filter(
        team_id=TEAM,
        slack_user_id=TARGET,
        user=user,
    ).exists()
    access = BotUserAccess.objects.get(
        workspace_id=TEAM,
        slack_user_id=TARGET,
        status=ACCESS_STATUS_APPROVED,
    )
    assert access.brightbean_user == user
    assert access.brightbean_workspace == workspace
    assert access.bot_created_org_membership is True
    assert access.bot_created_workspace_membership is True
    assert BotAccessAuditLog.objects.filter(
        workspace_id=TEAM,
        target_slack_user_id=TARGET,
    ).exists()


def test_repeated_grant_is_idempotent(workspace, channel_mapping, bot_admin):
    client = FakeIdentityClient(_identity())

    first = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=client,
    )
    second = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=client,
    )

    assert first.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert second.status == ProvisioningStatus.ALREADY_PROVISIONED
    assert User.objects.filter(email="target@example.com").count() == 1
    assert SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).count() == 1
    assert BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).count() == 1


def test_existing_brightbean_user_is_reused(workspace, channel_mapping, bot_admin):
    existing = User.objects.create_user(email="target@example.com", name="Existing")

    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    assert result.status == ProvisioningStatus.NEWLY_PROVISIONED
    assert User.objects.filter(email="target@example.com").count() == 1
    assert SlackUserMapping.objects.get(team_id=TEAM, slack_user_id=TARGET).user == existing


def test_external_email_domain_is_rejected_without_partial_records(
    workspace,
    channel_mapping,
    bot_admin,
):
    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity(email="target@evil-example.com")),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.COMPANY_EMAIL_REQUIRED
    assert not User.objects.filter(email="target@evil-example.com").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_malicious_suffix_domain_is_rejected_without_partial_records(workspace, channel_mapping, bot_admin):
    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity(email="target@example.com.evil.test")),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.COMPANY_EMAIL_REQUIRED
    assert not User.objects.filter(email="target@example.com.evil.test").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_wrong_admin_is_rejected_without_partial_records(workspace, channel_mapping, bot_admin):
    result = grant_slack_analytics_access(
        admin_slack_user_id="UWRONG01",
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.ADMIN_NOT_AUTHORIZED
    assert not User.objects.filter(email="target@example.com").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_wrong_slack_team_is_rejected_without_partial_records(workspace, channel_mapping, bot_admin):
    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity(team_id="TOTHER01")),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.INVALID_SLACK_USER_ID
    assert not User.objects.filter(email="target@example.com").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_bot_deleted_guest_and_missing_email_users_are_rejected(workspace, channel_mapping, bot_admin):
    cases = [
        (_identity(is_bot=True), ProvisioningFailureReason.SLACK_USER_IS_BOT),
        (_identity(is_deleted=True), ProvisioningFailureReason.SLACK_USER_INACTIVE),
        (_identity(is_guest=True), ProvisioningFailureReason.SLACK_GUEST_NOT_ALLOWED),
        (_identity(email=""), ProvisioningFailureReason.SLACK_EMAIL_MISSING),
    ]

    for identity, reason in cases:
        result = grant_slack_analytics_access(
            admin_slack_user_id=ADMIN,
            target_slack_user_id=TARGET,
            slack_team_id=TEAM,
            identity_client=FakeIdentityClient(identity),
        )

        assert result.status == ProvisioningStatus.FAILED
        assert result.failure_reason == reason
        assert not User.objects.filter(email="target@example.com").exists()
        assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
        assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_invalid_slack_id_is_rejected_without_partial_records(workspace, channel_mapping, bot_admin):
    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id="not-a-user",
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity(slack_user_id="not-a-user")),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.INVALID_SLACK_USER_ID
    assert not User.objects.filter(email="target@example.com").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id="not-a-user").exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id="not-a-user").exists()


def test_post_check_failure_rolls_back_partial_records(settings, workspace, channel_mapping, bot_admin):
    settings.SLACK_ALLOWED_TEAM_ID = "TOTHER01"

    result = grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    assert result.status == ProvisioningStatus.FAILED
    assert result.failure_reason == ProvisioningFailureReason.POST_CHECK_FAILED
    assert not User.objects.filter(email="target@example.com").exists()
    assert not SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).exists()
    assert not BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).exists()


def test_revoke_removes_bot_access_and_bot_created_workspace_membership(
    workspace,
    channel_mapping,
    bot_admin,
):
    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    result = revoke_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
    )

    assert result.status == ProvisioningStatus.REVOKED
    access = BotUserAccess.objects.get(workspace_id=TEAM, slack_user_id=TARGET)
    assert access.status == ACCESS_STATUS_REVOKED
    user = User.objects.get(email="target@example.com")
    assert not WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()
    assert User.objects.filter(id=user.id).exists()
    assert not OrgMembership.objects.filter(user=user, organization=workspace.organization).exists()


def test_revoke_preserves_preexisting_workspace_membership(
    workspace,
    channel_mapping,
    bot_admin,
):
    user = User.objects.create_user(email="target@example.com")
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )
    revoke_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
    )

    assert WorkspaceMembership.objects.filter(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER,
    ).exists()


def test_repeated_revoke_is_idempotent(workspace, channel_mapping, bot_admin):
    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )

    first = revoke_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
    )
    second = revoke_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
    )

    assert first.status == ProvisioningStatus.REVOKED
    assert second.status == ProvisioningStatus.ALREADY_REVOKED
    assert BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).count() == 1


def test_revoke_ignores_malformed_audit_and_uses_durable_provenance(workspace, channel_mapping, bot_admin):
    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )
    BotAccessAuditLog.objects.update(metadata={"bot_created_workspace_membership": False, "garbage": True})

    result = revoke_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
    )

    assert result.status == ProvisioningStatus.REVOKED
    user = User.objects.get(email="target@example.com")
    assert not WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()


def test_admin_dm_uses_canonical_provisioning_not_bot_only_service(
    workspace,
    channel_mapping,
    bot_admin,
):
    with patch("apps.slack_bot.admin_dm_service.grant_slack_analytics_access") as canonical:
        canonical.return_value.status = ProvisioningStatus.NEWLY_PROVISIONED
        canonical.return_value.target_slack_user_id = TARGET
        canonical.return_value.brightbean_email = "target@example.com"
        canonical.return_value.workspace_name = workspace.name
        canonical.return_value.failure_reason = ""
        canonical.return_value.failure_message = ""

        result = process_admin_dm(
            workspace_id=TEAM,
            sender_slack_user_id=ADMIN,
            dm_text=f"Give {TARGET} access",
            identity_client=FakeIdentityClient(_identity()),
        )

    assert result.handled is True
    canonical.assert_called_once()


@pytest.mark.skipif(connection.vendor != "postgresql", reason=POSTGRES_ONLY_REASON)
@pytest.mark.django_db(transaction=True)
def test_concurrent_grant_does_not_create_duplicates(workspace, channel_mapping, bot_admin):
    start = threading.Barrier(2)

    def _grant():
        return _run_in_thread(
            lambda: (
                _wait_for_overlap(start),
                grant_slack_analytics_access(
                    admin_slack_user_id=ADMIN,
                    target_slack_user_id=TARGET,
                    slack_team_id=TEAM,
                    identity_client=FakeIdentityClient(_identity()),
                ),
            )[1]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _grant(), range(2)))

    assert {result.status for result in results} <= {
        ProvisioningStatus.NEWLY_PROVISIONED,
        ProvisioningStatus.ALREADY_PROVISIONED,
        ProvisioningStatus.REPAIRED,
    }
    user = User.objects.get(email="target@example.com")
    assert User.objects.filter(email="target@example.com").count() == 1
    assert OrgMembership.objects.filter(user=user, organization=workspace.organization).count() == 1
    assert WorkspaceMembership.objects.filter(user=user, workspace=workspace).count() == 1
    assert SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).count() == 1
    assert BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).count() == 1
    assert BotUserAccess.objects.get(workspace_id=TEAM, slack_user_id=TARGET).status == ACCESS_STATUS_APPROVED


@pytest.mark.skipif(connection.vendor != "postgresql", reason=POSTGRES_ONLY_REASON)
@pytest.mark.django_db(transaction=True)
def test_concurrent_revoke_is_idempotent(workspace, channel_mapping, bot_admin):
    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )
    start = threading.Barrier(2)

    def _revoke():
        return _run_in_thread(
            lambda: (
                _wait_for_overlap(start),
                revoke_slack_analytics_access(
                    admin_slack_user_id=ADMIN,
                    target_slack_user_id=TARGET,
                    slack_team_id=TEAM,
                ),
            )[1]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _revoke(), range(2)))

    assert {result.status for result in results} <= {
        ProvisioningStatus.REVOKED,
        ProvisioningStatus.ALREADY_REVOKED,
    }
    assert BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).count() == 1
    access = BotUserAccess.objects.get(workspace_id=TEAM, slack_user_id=TARGET)
    assert access.status == ACCESS_STATUS_REVOKED
    user = User.objects.get(email="target@example.com")
    assert User.objects.filter(id=user.id).exists()
    assert not WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()
    assert not OrgMembership.objects.filter(user=user, organization=workspace.organization).exists()


@pytest.mark.skipif(connection.vendor != "postgresql", reason=POSTGRES_ONLY_REASON)
@pytest.mark.django_db(transaction=True)
def test_grant_revoke_race_keeps_single_auditable_access_row(workspace, channel_mapping, bot_admin):
    grant_slack_analytics_access(
        admin_slack_user_id=ADMIN,
        target_slack_user_id=TARGET,
        slack_team_id=TEAM,
        identity_client=FakeIdentityClient(_identity()),
    )
    start = threading.Barrier(2)

    def _grant():
        return _run_in_thread(
            lambda: (
                _wait_for_overlap(start),
                grant_slack_analytics_access(
                    admin_slack_user_id=ADMIN,
                    target_slack_user_id=TARGET,
                    slack_team_id=TEAM,
                    identity_client=FakeIdentityClient(_identity()),
                ),
            )[1]
        )

    def _revoke():
        return _run_in_thread(
            lambda: (
                _wait_for_overlap(start),
                revoke_slack_analytics_access(
                    admin_slack_user_id=ADMIN,
                    target_slack_user_id=TARGET,
                    slack_team_id=TEAM,
                ),
            )[1]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda fn: fn(), [_grant, _revoke]))

    assert {result.status for result in results} <= {
        ProvisioningStatus.ALREADY_PROVISIONED,
        ProvisioningStatus.REVOKED,
        ProvisioningStatus.ALREADY_REVOKED,
        ProvisioningStatus.REPAIRED,
    }
    assert User.objects.filter(email="target@example.com").count() == 1
    user = User.objects.get(email="target@example.com")
    assert SlackUserMapping.objects.filter(team_id=TEAM, slack_user_id=TARGET).count() == 1
    assert WorkspaceMembership.objects.filter(user=user, workspace=workspace).count() <= 1
    assert OrgMembership.objects.filter(user=user, organization=workspace.organization).count() <= 1
    assert BotUserAccess.objects.filter(workspace_id=TEAM, slack_user_id=TARGET).count() == 1

    access = BotUserAccess.objects.get(workspace_id=TEAM, slack_user_id=TARGET)
    assert access.brightbean_user == user
    assert access.status in {ACCESS_STATUS_APPROVED, ACCESS_STATUS_REVOKED}
    if access.status == ACCESS_STATUS_APPROVED:
        assert WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()
        assert OrgMembership.objects.filter(user=user, organization=workspace.organization).exists()
    else:
        assert not WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()
        assert not OrgMembership.objects.filter(user=user, organization=workspace.organization).exists()

    assert BotAccessAuditLog.objects.filter(workspace_id=TEAM, target_slack_user_id=TARGET).count() >= 2
