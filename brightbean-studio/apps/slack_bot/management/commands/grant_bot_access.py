"""Django management command for canonical Slack analytics provisioning.

Usage:
    python manage.py grant_bot_access --workspace-id T0123 --admin-user-id UADMIN --user-ids U001 U002
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.slack_bot.access_provisioning import ProvisioningStatus, grant_slack_analytics_access
from apps.slack_bot.slack_id_validation import (
    is_valid_member_id,
    is_valid_workspace_id,
)


class Command(BaseCommand):
    help = "Grant Slack analytics access through the canonical provisioning service."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace-id",
            required=True,
            help="Slack workspace/team ID (e.g. T0123456789)",
        )
        parser.add_argument(
            "--user-ids",
            nargs="+",
            required=True,
            help="One or more Slack user/member IDs (e.g. U001 U002 U003)",
        )
        parser.add_argument(
            "--admin-user-id",
            required=True,
            help="Slack user ID of an active BotAdministrator performing the grant.",
        )

    def handle(self, *args, **options):
        workspace_id = options["workspace_id"].strip()
        admin_user_id = options["admin_user_id"].strip()
        user_ids = options["user_ids"]

        if not is_valid_workspace_id(workspace_id):
            raise CommandError(
                f"Invalid workspace ID: {workspace_id!r}. "
                "Expected format: T followed by uppercase alphanumeric characters."
            )

        if not is_valid_member_id(admin_user_id):
            raise CommandError(
                f"Invalid admin user ID: {admin_user_id!r}. "
                "Expected a Slack member ID, not a channel ID."
            )

        buckets: dict[str, list[str]] = {
            "approved": [],
            "restored": [],
            "repaired": [],
            "already_approved": [],
            "invalid": [],
            "failed": [],
        }

        seen: set[str] = set()
        for raw_user_id in user_ids:
            user_id = str(raw_user_id).strip()
            if user_id in seen:
                continue
            seen.add(user_id)

            if not is_valid_member_id(user_id):
                buckets["invalid"].append(user_id)
                continue

            result = grant_slack_analytics_access(
                admin_slack_user_id=admin_user_id,
                target_slack_user_id=user_id,
                slack_team_id=workspace_id,
                source="management_command",
            )

            if result.status == ProvisioningStatus.NEWLY_PROVISIONED:
                buckets["approved"].append(user_id)
            elif result.status == ProvisioningStatus.RESTORED:
                buckets["restored"].append(user_id)
            elif result.status == ProvisioningStatus.REPAIRED:
                buckets["repaired"].append(user_id)
            elif result.status == ProvisioningStatus.ALREADY_PROVISIONED:
                buckets["already_approved"].append(user_id)
            else:
                suffix = f" ({result.failure_reason})" if result.failure_reason else ""
                buckets["failed"].append(f"{user_id}{suffix}")

        lines: list[str] = []

        if buckets["approved"]:
            lines.append("Approved:")
            for uid in buckets["approved"]:
                lines.append(f"  - {uid}")

        if buckets["restored"]:
            lines.append("Restored:")
            for uid in buckets["restored"]:
                lines.append(f"  - {uid}")

        if buckets["repaired"]:
            lines.append("Repaired:")
            for uid in buckets["repaired"]:
                lines.append(f"  - {uid}")

        if buckets["already_approved"]:
            lines.append("Already approved:")
            for uid in buckets["already_approved"]:
                lines.append(f"  - {uid}")

        if buckets["invalid"]:
            lines.append("Invalid Member IDs:")
            for uid in buckets["invalid"]:
                lines.append(f"  - {uid}")

        if buckets["failed"]:
            lines.append("Failed:")
            for uid in buckets["failed"]:
                lines.append(f"  - {uid}")

        if not lines:
            lines.append("No changes.")

        self.stdout.write(self.style.SUCCESS("\n".join(lines)))
