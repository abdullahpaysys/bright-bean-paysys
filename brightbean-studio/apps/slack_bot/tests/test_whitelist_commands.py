"""Tests for Phase 1 bot whitelisting management commands."""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.slack_bot.access_provisioning import ProvisioningResult, ProvisioningStatus
from apps.slack_bot.models import (
    BotAdministrator,
    BotUserAccess,
)

# ===========================================================================
# create_bot_admin
# ===========================================================================


@pytest.mark.django_db
def test_create_bot_admin_first_time(capsys):
    call_command(
        "create_bot_admin",
        "--workspace-id", "T0001",
        "--user-id", "U0001",
    )
    out = capsys.readouterr().out
    assert "configured successfully" in out
    assert "T0001" in out
    assert "U0001" in out

    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0001"

    access = BotUserAccess.objects.get(workspace_id="T0001", slack_user_id="U0001")
    assert access.status == "APPROVED"


@pytest.mark.django_db
def test_create_bot_admin_repeated_same_user(capsys):
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    out = capsys.readouterr().out
    assert "No change required" in out
    assert BotAdministrator.objects.count() == 1


@pytest.mark.django_db
def test_create_bot_admin_different_user(capsys):
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0001")
    call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "U0002")
    out = capsys.readouterr().out
    assert "updated" in out
    admin = BotAdministrator.objects.get(workspace_id="T0001")
    assert admin.slack_user_id == "U0002"


@pytest.mark.django_db
def test_create_bot_admin_invalid_workspace_id():
    with pytest.raises(CommandError, match="Invalid workspace ID"):
        call_command("create_bot_admin", "--workspace-id", "X0001", "--user-id", "U0001")


@pytest.mark.django_db
def test_create_bot_admin_invalid_user_id():
    with pytest.raises(CommandError, match="Invalid user ID"):
        call_command("create_bot_admin", "--workspace-id", "T0001", "--user-id", "C0001")


# ===========================================================================
# grant_bot_access
# ===========================================================================


@pytest.mark.django_db
def test_grant_bot_access_invokes_canonical_service(capsys):
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.NEWLY_PROVISIONED, target_slack_user_id="U0001"),
    ) as canonical:
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001",
        )

    out = capsys.readouterr().out
    canonical.assert_called_once_with(
        admin_slack_user_id="UADMIN1",
        target_slack_user_id="U0001",
        slack_team_id="T0001",
        source="management_command",
    )
    assert "Approved" in out
    assert "U0001" in out
    assert BotUserAccess.objects.filter(workspace_id="T0001", slack_user_id="U0001").count() == 0


@pytest.mark.django_db
def test_grant_bot_access_multiple_users(capsys):
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.NEWLY_PROVISIONED, target_slack_user_id="U0001"),
    ) as canonical:
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001", "U0002", "U0003",
        )

    out = capsys.readouterr().out
    assert "U0001" in out
    assert "U0002" in out
    assert "U0003" in out
    assert canonical.call_count == 3


@pytest.mark.django_db
def test_grant_bot_access_duplicate_users_are_deduplicated():
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.NEWLY_PROVISIONED, target_slack_user_id="U0001"),
    ) as canonical:
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001", "U0001",
        )

    assert canonical.call_count == 1


@pytest.mark.django_db
def test_grant_bot_access_mixed_valid_invalid(capsys):
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.NEWLY_PROVISIONED, target_slack_user_id="U0001"),
    ) as canonical:
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001", "C0001", "U0002", "G0001",
        )

    out = capsys.readouterr().out
    assert "C0001" in out
    assert "G0001" in out
    assert canonical.call_count == 2


@pytest.mark.django_db
def test_grant_bot_access_channel_id_rejected(capsys):
    with patch("apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access") as canonical:
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "C0001",
        )

    out = capsys.readouterr().out
    assert "C0001" in out
    canonical.assert_not_called()
    assert BotUserAccess.objects.count() == 0


@pytest.mark.django_db
def test_grant_bot_access_invalid_workspace_id():
    with pytest.raises(CommandError, match="Invalid workspace ID"):
        call_command(
            "grant_bot_access",
            "--workspace-id", "X0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001",
        )


@pytest.mark.django_db
def test_grant_bot_access_invalid_admin_user_id():
    with pytest.raises(CommandError, match="Invalid admin user ID"):
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "CADMIN1",
            "--user-ids", "U0001",
        )


@pytest.mark.django_db
def test_grant_bot_access_already_approved(capsys):
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.ALREADY_PROVISIONED, target_slack_user_id="U0001"),
    ):
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001",
        )

    out = capsys.readouterr().out
    assert "Already approved" in out


@pytest.mark.django_db
def test_grant_bot_access_restores_revoked(capsys):
    with patch(
        "apps.slack_bot.management.commands.grant_bot_access.grant_slack_analytics_access",
        return_value=ProvisioningResult(status=ProvisioningStatus.RESTORED, target_slack_user_id="U0001"),
    ):
        call_command(
            "grant_bot_access",
            "--workspace-id", "T0001",
            "--admin-user-id", "UADMIN1",
            "--user-ids", "U0001",
        )

    out = capsys.readouterr().out
    assert "Restored" in out
    assert "U0001" in out
