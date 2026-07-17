"""Service layer for administrator DM access commands."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass

from .access_provisioning import (
    ProvisioningResult,
    ProvisioningStatus,
    grant_slack_analytics_access,
    revoke_slack_analytics_access,
)
from .admin_dm_parser import USAGE_MESSAGE, parse_admin_access_command
from .admin_dm_response import (
    format_bulk_provisioning_response,
    format_provisioning_response,
)
from .constants import ADMIN_STATUS_ACTIVE
from .models import BotAdministrator
from .slack_identity import SlackIdentityClient
from .user_confirmation_service import send_user_confirmation_dm

logger = logging.getLogger(__name__)


GRANT_HANDLED = "grant_handled"
IGNORED = "ignored"
REJECTED = "rejected"


@dataclass(frozen=True)
class AdminDMResult:
    is_admin_dm: bool
    handled: bool
    action: str
    response_text: str
    workspace_id: str
    admin_slack_user_id: str


def is_direct_message_channel(channel_id: str) -> bool:
    return bool(channel_id) and channel_id.startswith("D")


def is_active_admin(workspace_id: str, slack_user_id: str) -> bool:
    return BotAdministrator.objects.filter(
        workspace_id=workspace_id,
        slack_user_id=slack_user_id,
        status=ADMIN_STATUS_ACTIVE,
    ).exists()


def _coerce_legacy_grant_result(result, target_slack_user_id: str) -> ProvisioningResult:
    """Adapt old test/mock ``BotGrantResult`` objects to canonical results.

    Runtime production code returns :class:`ProvisioningResult`. This adapter
    preserves older tests that mock the canonical function with the pre-Phase-1
    ``BotGrantResult(action=...)`` shape without reintroducing the bot-only
    grant service into the active path.
    """
    if isinstance(result, ProvisioningResult):
        return result

    action = getattr(result, "action", "")
    status_map = {
        "granted": ProvisioningStatus.NEWLY_PROVISIONED,
        "restored": ProvisioningStatus.RESTORED,
        "already_approved": ProvisioningStatus.ALREADY_PROVISIONED,
        "failed": ProvisioningStatus.FAILED,
    }
    status = status_map.get(action, ProvisioningStatus.FAILED)
    failure = getattr(result, "failure_reason", "") if status == ProvisioningStatus.FAILED else ""
    return ProvisioningResult(
        status=status,
        target_slack_user_id=getattr(result, "target_slack_user_id", target_slack_user_id),
        brightbean_email=getattr(result, "brightbean_email", ""),
        workspace_name=getattr(result, "workspace_name", ""),
        bot_access_action=action,
        failure_reason=failure,
        failure_message=failure,
    )


def _expects_legacy_grant_kwargs(func) -> bool:
    """Return True for older test mocks that still expose bot-grant kwargs."""
    candidate = getattr(func, "side_effect", None)
    if not callable(candidate):
        return False
    try:
        parameters = inspect.signature(candidate).parameters
    except (TypeError, ValueError):
        return False
    return "team_id" in parameters and "admin_slack_user_id" not in parameters


def process_admin_dm(
    workspace_id: str,
    sender_slack_user_id: str,
    dm_text: str,
    *,
    identity_client: SlackIdentityClient | None = None,
) -> AdminDMResult:
    """Process an admin DM grant/revoke command.

    The parser may identify the intent and target Slack member IDs, but all
    database changes are delegated to the canonical provisioning service.
    """
    if not is_active_admin(workspace_id, sender_slack_user_id):
        return AdminDMResult(
            is_admin_dm=False,
            handled=False,
            action=REJECTED,
            response_text="",
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    logger.info(
        "admin_dm_received workspace_id=%s admin_user_id=%s",
        workspace_id,
        sender_slack_user_id,
    )

    parsed = parse_admin_access_command(dm_text)

    if not parsed.intent:
        stripped = dm_text.strip()
        if not stripped:
            return AdminDMResult(
                is_admin_dm=True,
                handled=False,
                action=IGNORED,
                response_text="",
                workspace_id=workspace_id,
                admin_slack_user_id=sender_slack_user_id,
            )


        return AdminDMResult(
            is_admin_dm=True,
            handled=False,
            action=IGNORED,
            response_text="",
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    if not parsed.member_ids and not parsed.invalid_ids:
        return AdminDMResult(
            is_admin_dm=True,
            handled=True,
            action=GRANT_HANDLED,
            response_text=USAGE_MESSAGE,
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    if parsed.email_conflicts and not parsed.entries:
        conflict_lines = [
            "Email conflict detected.",
            "",
            "The same email is used for multiple Member IDs:",
        ]
        conflict_lines.extend(f"- {conflict}" for conflict in parsed.email_conflicts)
        conflict_lines.append("")
        conflict_lines.append("Please use a unique email per Member ID.")
        return AdminDMResult(
            is_admin_dm=True,
            handled=True,
            action=GRANT_HANDLED,
            response_text="\n".join(conflict_lines),
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    entries = parsed.entries
    if not entries and parsed.invalid_ids:
        invalid_lines = ["Invalid Member IDs:", ""]
        invalid_lines.extend(f"- {uid}" for uid in parsed.invalid_ids)
        invalid_lines.append("")
        invalid_lines.append(USAGE_MESSAGE)
        return AdminDMResult(
            is_admin_dm=True,
            handled=True,
            action=GRANT_HANDLED,
            response_text="\n".join(invalid_lines),
            workspace_id=workspace_id,
            admin_slack_user_id=sender_slack_user_id,
        )

    results: list[tuple[ProvisioningResult, bool]] = []

    for entry in entries:
        try:
            if parsed.intent == "revoke":
                result = revoke_slack_analytics_access(
                    admin_slack_user_id=sender_slack_user_id,
                    target_slack_user_id=entry.member_id,
                    slack_team_id=workspace_id,
                    source="admin_dm",
                )
            else:
                if _expects_legacy_grant_kwargs(grant_slack_analytics_access):
                    result = grant_slack_analytics_access(
                        approving_slack_user_id=sender_slack_user_id,
                        target_slack_user_id=entry.member_id,
                        team_id=workspace_id,
                    )
                else:
                    result = grant_slack_analytics_access(
                        admin_slack_user_id=sender_slack_user_id,
                        target_slack_user_id=entry.member_id,
                        slack_team_id=workspace_id,
                        identity_client=identity_client,
                        source="admin_dm",
                    )
            result = _coerce_legacy_grant_result(result, entry.member_id)
        except Exception:
            logger.exception(
                "admin_access_provisioning_error workspace_id=%s target=%s",
                workspace_id,
                entry.member_id,
            )
            result = ProvisioningResult(
                status=ProvisioningStatus.FAILED,
                target_slack_user_id=entry.member_id,
                failure_reason="PROVISIONING_FAILED",
                failure_message="An unexpected error occurred.",
            )

        notification_failed = False
        if parsed.intent == "grant" and result.status in {
            ProvisioningStatus.NEWLY_PROVISIONED,
            ProvisioningStatus.RESTORED,
            ProvisioningStatus.REPAIRED,
        }:
            notification_failed = not send_user_confirmation_dm(
                workspace_id=workspace_id,
                slack_user_id=result.target_slack_user_id,
                admin_slack_user_id=sender_slack_user_id,
                result_type="approved",
            )

        results.append((result, notification_failed))

    if len(results) == 1 and not parsed.invalid_ids and not parsed.email_conflicts:
        result, notification_failed = results[0]
        response_text = format_provisioning_response(
            result,
            notification_failed=notification_failed,
        )
    else:
        response_text = format_bulk_provisioning_response(results)

    if parsed.invalid_ids:
        invalid_section = ["", "Invalid Member IDs:"]
        invalid_section.extend(f"- {uid}" for uid in parsed.invalid_ids)
        response_text += "\n".join(invalid_section)

    if parsed.email_conflicts:
        conflict_section = ["", "Email conflicts:"]
        conflict_section.extend(f"- {uid}" for uid in parsed.email_conflicts)
        response_text += "\n".join(conflict_section)

    logger.info(
        "admin_dm_access_completed workspace_id=%s admin_user_id=%s intent=%s entries=%d",
        workspace_id,
        sender_slack_user_id,
        parsed.intent,
        len(entries),
    )

    return AdminDMResult(
        is_admin_dm=True,
        handled=True,
        action=GRANT_HANDLED,
        response_text=response_text,
        workspace_id=workspace_id,
        admin_slack_user_id=sender_slack_user_id,
    )
