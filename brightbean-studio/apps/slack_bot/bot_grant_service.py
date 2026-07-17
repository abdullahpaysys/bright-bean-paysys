"""Legacy bot-only access grant service.

This module is kept for backward compatibility with older tests and
manual utilities.  It is not the production admin-DM grant path.

Production access changes must use
``apps.slack_bot.access_provisioning.grant_slack_analytics_access`` and
``revoke_slack_analytics_access`` so Slack identity, BrightBean user
mapping, organization membership, workspace membership, bot access, and
audit logging are handled atomically.

The helpers below grant ``APPROVED / READ_ONLY`` bot access without
creating BrightBean identity records (User, SlackUserMapping,
OrgMembership, WorkspaceMembership).

This service does **not**:
- create BrightBean users or memberships;
- call the LLM or external APIs;
- use the admin DM channel for workspace resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction

from .access_service import grant_user_access
from .workspace_grant_service import (
    resolve_grant_workspace,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BotGrantResult:
    """Outcome of a single bot access grant.

    Attributes:
        action: "granted", "already_approved", "restored", "failed".
        target_slack_user_id: The Slack Member ID.
        workspace_name: Safe workspace name (empty on failure).
        failure_reason: Human-facing failure message (empty on success).
    """

    action: str
    target_slack_user_id: str
    workspace_name: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class BulkBotGrantResult:
    """Outcome of a bulk bot access grant.

    Attributes:
        granted: List of Member IDs that were newly approved.
        restored: List of Member IDs restored from REVOKED.
        already_approved: List of Member IDs that were already approved.
        failed: List of (member_id, failure_reason) tuples.
    """

    granted: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    already_approved: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single-user grant
# ---------------------------------------------------------------------------


@transaction.atomic
def grant_bot_access(
    *,
    team_id: str,
    target_slack_user_id: str,
    approving_slack_user_id: str,
) -> BotGrantResult:
    """Grant bot access to a single Slack user.

    1. Resolve the workspace via :func:`resolve_grant_workspace`.
    2. If resolution fails, return a failed result with no DB changes.
    3. Call :func:`grant_user_access` to create/restore BotUserAccess.
    4. Return the result with the safe workspace name.

    Does not create BrightBean users, SlackUserMapping, OrgMembership,
    or WorkspaceMembership.
    """
    resolution = resolve_grant_workspace(
        team_id=team_id,
        target_slack_user_id=target_slack_user_id,
    )

    if not resolution.ok:
        return BotGrantResult(
            action="failed",
            target_slack_user_id=target_slack_user_id,
            failure_reason=resolution.failure_reason,
        )

    grant_result = grant_user_access(
        workspace_id=team_id,
        slack_user_id=target_slack_user_id,
        granted_by_slack_user_id=approving_slack_user_id,
    )

    return BotGrantResult(
        action=grant_result.action,
        target_slack_user_id=target_slack_user_id,
        workspace_name=resolution.workspace_name,
    )


# ---------------------------------------------------------------------------
# Bulk grant
# ---------------------------------------------------------------------------


def grant_bulk_bot_access(
    *,
    team_id: str,
    target_slack_user_ids: list[str],
    approving_slack_user_id: str,
) -> BulkBotGrantResult:
    """Grant bot access to multiple Slack users.

    Each user is processed independently — one failure does not block
    others.  Each user gets its own atomic transaction via
    :func:`grant_bot_access`.
    """
    result = BulkBotGrantResult()

    for member_id in target_slack_user_ids:
        try:
            single = grant_bot_access(
                team_id=team_id,
                target_slack_user_id=member_id,
                approving_slack_user_id=approving_slack_user_id,
            )
        except Exception as exc:
            logger.exception(
                "bulk_grant_error team=%s target=%s: %s",
                team_id, member_id, exc,
            )
            result.failed.append((member_id, "An unexpected error occurred."))
            continue

        if single.action == "granted":
            result.granted.append(member_id)
        elif single.action == "restored":
            result.restored.append(member_id)
        elif single.action == "already_approved":
            result.already_approved.append(member_id)
        else:
            result.failed.append((member_id, single.failure_reason))

    return result
