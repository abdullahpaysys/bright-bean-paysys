"""Format administrator DM responses for access-grant results.

Produces the exact text strings sent back to the administrator in
their DM channel after a grant command is processed.

Includes both the legacy Phase 1 formatters (for backward compatibility
with existing tests) and the new Phase C provisioning formatters.
"""

from __future__ import annotations

from .access_provisioning import (
    ProvisioningFailureReason,
    ProvisioningResult,
    ProvisioningStatus,
)
from .access_service import BulkGrantResult, GrantResult
from .bot_grant_service import BotGrantResult

# ---------------------------------------------------------------------------
# Single-user responses
# ---------------------------------------------------------------------------


def format_single_grant_response(
    result: GrantResult,
    notification_failed: bool = False,
) -> str:
    """Format the DM response for a single-user grant operation.

    Args:
        result: The :class:`GrantResult` from ``grant_user_access``.
        notification_failed: If True, the user confirmation DM could not
            be delivered.  Only relevant for ``granted`` and ``restored``.

    Returns:
        Formatted text suitable for a Slack DM message.
    """
    if result.action == "granted":
        base = (
            "Access granted successfully.\n\n"
            f"Member ID: {result.slack_user_id}\n"
            "Permission: Read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.action == "restored":
        base = (
            "Access restored successfully.\n\n"
            f"Member ID: {result.slack_user_id}\n"
            "Permission: Read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    # already_approved
    return (
        "No change required.\n\n"
        f"Member ID: {result.slack_user_id}\n"
        "Permission: Read-only\n"
        "Status: Already approved"
    )


# ---------------------------------------------------------------------------
# Bulk-user response
# ---------------------------------------------------------------------------


def format_bulk_grant_response(
    result: BulkGrantResult,
    notification_failures: list[str] | None = None,
) -> str:
    """Format the DM response for a bulk grant operation.

    Args:
        result: The :class:`BulkGrantResult` from ``grant_bulk_user_access``.
        notification_failures: List of Slack user IDs where the confirmation
            DM could not be delivered.  Omitted from output when empty.

    Returns:
        Formatted text with grouped sections.  Empty sections are omitted
        except that the result always shows whether anything was approved.
    """
    lines: list[str] = ["Bulk access update completed.", ""]

    if result.approved:
        lines.append("Approved:")
        for uid in result.approved:
            lines.append(f"• {uid}")
        lines.append("")

    if result.restored:
        lines.append("Restored:")
        for uid in result.restored:
            lines.append(f"• {uid}")
        lines.append("")

    if result.already_approved:
        lines.append("Already approved:")
        for uid in result.already_approved:
            lines.append(f"• {uid}")
        lines.append("")

    if result.invalid:
        lines.append("Invalid Member IDs:")
        for uid in result.invalid:
            lines.append(f"• {uid}")
        lines.append("")

    if result.failed:
        lines.append("Failed:")
        for uid in result.failed:
            lines.append(f"• {uid}")
        lines.append("")

    if notification_failures:
        lines.append("User notifications failed:")
        for uid in notification_failures:
            lines.append(f"• {uid}")
        lines.append("")

    lines.append("Permission: Read-only")

    return "\n".join(lines)


# ===========================================================================
# Phase C — Provisioning response formatters
# ===========================================================================


# Map provisioning failure reasons to safe user-facing messages.
_FAILURE_MESSAGES: dict[str, str] = {
    ProvisioningFailureReason.NOT_ADMIN: "You are not an authorised administrator.",
    ProvisioningFailureReason.ADMIN_NOT_AUTHORIZED: "You are not an authorised administrator.",
    ProvisioningFailureReason.INVALID_SLACK_USER_ID: "Invalid Slack member ID.",
    ProvisioningFailureReason.SLACK_USER_NOT_FOUND: "Slack user was not found.",
    ProvisioningFailureReason.SLACK_PROFILE_UNAVAILABLE: "Slack profile is unavailable.",
    ProvisioningFailureReason.SLACK_USER_INACTIVE: "Slack user is deactivated.",
    ProvisioningFailureReason.SLACK_USER_IS_BOT: "Bot users cannot be granted access.",
    ProvisioningFailureReason.SLACK_GUEST_NOT_ALLOWED: "Guest users are not supported.",
    ProvisioningFailureReason.SLACK_EMAIL_MISSING: "Slack profile email is missing.",
    ProvisioningFailureReason.COMPANY_EMAIL_REQUIRED: "Slack profile email is not an approved company email.",
    ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED: "BrightBean workspace is not configured for this Slack team.",
    ProvisioningFailureReason.CHANNEL_NOT_MAPPED: "Source channel not found.",
    ProvisioningFailureReason.WORKSPACE_ARCHIVED: "Workspace is archived.",
    ProvisioningFailureReason.BRIGHTBEAN_USER_CONFLICT: "Slack user is already mapped to a different BrightBean user.",
    ProvisioningFailureReason.USER_INACTIVE: "BrightBean user is inactive.",
    ProvisioningFailureReason.EMAIL_MISMATCH: "Supplied email does not match the existing user.",
    ProvisioningFailureReason.NO_VIEW_ANALYTICS: "User lacks analytics permission.",
    ProvisioningFailureReason.POST_CHECK_FAILED: "Authorization verification failed.",
    ProvisioningFailureReason.PROVISIONING_FAILED: "Provisioning failed.",
}


def _safe_failure_message(reason: str, fallback: str = "") -> str:
    """Return a safe user-facing message for a provisioning failure reason."""
    if fallback:
        return fallback

    if reason in _FAILURE_MESSAGES:
        return _FAILURE_MESSAGES[reason]
    for key, message in _FAILURE_MESSAGES.items():
        if str(key) == str(reason):
            return message
    return fallback or "Provisioning failed."


def format_provisioning_response(
    result: ProvisioningResult,
    notification_failed: bool = False,
) -> str:
    """Format the DM response for a single-user provisioning operation.

    Args:
        result: The :class:`ProvisioningResult` from
            ``grant_slack_analytics_access``.
        notification_failed: If True, the user confirmation DM could not
            be delivered.

    Returns:
        Formatted text suitable for a Slack DM message.
    """
    member = result.target_slack_user_id
    email = result.brightbean_email or "—"
    ws_name = result.workspace_name or "—"

    if result.status == ProvisioningStatus.NEWLY_PROVISIONED:
        base = (
            "Access granted successfully.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.RESTORED:
        base = (
            "Access restored successfully.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.REPAIRED:
        base = (
            "Access provisioning completed.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: BrightBean access linked"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.status == ProvisioningStatus.ALREADY_PROVISIONED:
        return (
            "No change required.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Already approved and linked"
        )

    if result.status == ProvisioningStatus.REVOKED:
        return (
            "Access revoked successfully.\n\n"
            f"Member: {member}\n"
            f"BrightBean user: {email}\n"
            f"Workspace: {ws_name}\n"
            "Status: Revoked"
        )

    if result.status == ProvisioningStatus.ALREADY_REVOKED:
        return (
            "No change required.\n\n"
            f"Member: {member}\n"
            "Status: Already revoked"
        )

    # FAILED
    reason_msg = _safe_failure_message(
        result.failure_reason, result.failure_message,
    )
    return (
        "Access was not granted.\n\n"
        f"Member: {member}\n"
        f"Reason: {reason_msg}"
    )


# ---------------------------------------------------------------------------
# Bulk provisioning response
# ---------------------------------------------------------------------------


def format_bulk_provisioning_response(
    results: list[tuple[ProvisioningResult, bool]],
) -> str:
    """Format the DM response for a bulk provisioning operation.

    Args:
        results: List of ``(ProvisioningResult, notification_failed)`` tuples.

    Returns:
        Formatted text with grouped sections.
    """
    approved: list[str] = []
    repaired: list[str] = []
    restored: list[str] = []
    already_linked: list[str] = []
    revoked: list[str] = []
    already_revoked: list[str] = []
    failed: list[str] = []
    notification_failures: list[str] = []

    for result, notif_failed in results:
        member = result.target_slack_user_id
        email = result.brightbean_email

        if notif_failed:
            notification_failures.append(member)

        if result.status == ProvisioningStatus.NEWLY_PROVISIONED:
            label = f"• {member} — {email}" if email else f"• {member}"
            approved.append(label)
        elif result.status == ProvisioningStatus.REPAIRED:
            label = f"• {member} — {email}" if email else f"• {member}"
            repaired.append(label)
        elif result.status == ProvisioningStatus.RESTORED:
            label = f"• {member} — {email}" if email else f"• {member}"
            restored.append(label)
        elif result.status == ProvisioningStatus.ALREADY_PROVISIONED:
            label = f"• {member} — {email}" if email else f"• {member}"
            already_linked.append(label)
        elif result.status == ProvisioningStatus.REVOKED:
            label = f"â€¢ {member} â€” {email}" if email else f"â€¢ {member}"
            revoked.append(label)
        elif result.status == ProvisioningStatus.ALREADY_REVOKED:
            already_revoked.append(f"â€¢ {member}")
        else:
            # FAILED
            reason_msg = _safe_failure_message(
                result.failure_reason, result.failure_message,
            )
            failed.append(f"• {member} — {reason_msg}")

    lines: list[str] = ["Bulk access update completed.", ""]

    if approved:
        lines.append("Approved and linked:")
        lines.extend(approved)
        lines.append("")

    if repaired:
        lines.append("Repaired:")
        lines.extend(repaired)
        lines.append("")

    if restored:
        lines.append("Restored:")
        lines.extend(restored)
        lines.append("")

    if already_linked:
        lines.append("Already approved and linked:")
        lines.extend(already_linked)
        lines.append("")

    if revoked:
        lines.append("Revoked:")
        lines.extend(revoked)
        lines.append("")

    if already_revoked:
        lines.append("Already revoked:")
        lines.extend(already_revoked)
        lines.append("")

    if failed:
        lines.append("Failed:")
        lines.extend(failed)
        lines.append("")

    if notification_failures:
        lines.append("User notifications failed:")
        for uid in notification_failures:
            lines.append(f"• {uid}")
        lines.append("")

    lines.append("Permission: Analytics read-only")

    return "\n".join(lines)


# ===========================================================================
# Phase 1 — Bot access grant response formatters
# ===========================================================================


def format_bot_grant_response(
    result: BotGrantResult,
    notification_failed: bool = False,
) -> str:
    """Format the DM response for a single-user bot access grant.

    Args:
        result: The :class:`BotGrantResult` from ``grant_bot_access``.
        notification_failed: If True, the user confirmation DM could not
            be delivered.

    Returns:
        Formatted text suitable for a Slack DM message.
    """
    member = result.target_slack_user_id
    ws_name = result.workspace_name or "—"

    if result.action == "granted":
        base = (
            "Access granted successfully.\n\n"
            f"Member: {member}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.action == "restored":
        base = (
            "Access restored successfully.\n\n"
            f"Member: {member}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Approved"
        )
        if notification_failed:
            return base + "\n\nUser notification: Failed"
        return base

    if result.action == "already_approved":
        return (
            "No change required.\n\n"
            f"Member: {member}\n"
            f"Workspace: {ws_name}\n"
            "Permission: Analytics read-only\n"
            "Status: Already approved"
        )

    # failed
    reason = result.failure_reason or "Access was not granted."
    return (
        "Access was not granted.\n\n"
        f"Member: {member}\n"
        f"Reason: {reason}"
    )


def format_bulk_bot_grant_response(
    results: list[tuple[BotGrantResult, bool]],
) -> str:
    """Format the DM response for a bulk bot access grant.

    Args:
        results: List of ``(BotGrantResult, notification_failed)`` tuples.

    Returns:
        Formatted text with grouped sections.
    """
    granted: list[str] = []
    restored: list[str] = []
    already_approved: list[str] = []
    failed: list[str] = []
    notification_failures: list[str] = []

    for result, notif_failed in results:
        member = result.target_slack_user_id

        if notif_failed:
            notification_failures.append(member)

        if result.action == "granted":
            granted.append(f"• {member}")
        elif result.action == "restored":
            restored.append(f"• {member}")
        elif result.action == "already_approved":
            already_approved.append(f"• {member}")
        else:
            reason = result.failure_reason or "Access was not granted."
            failed.append(f"• {member} — {reason}")

    lines: list[str] = ["Bulk access update completed.", ""]

    if granted:
        lines.append("Approved:")
        lines.extend(granted)
        lines.append("")

    if restored:
        lines.append("Restored:")
        lines.extend(restored)
        lines.append("")

    if already_approved:
        lines.append("Already approved:")
        lines.extend(already_approved)
        lines.append("")

    if failed:
        lines.append("Failed:")
        lines.extend(failed)
        lines.append("")

    if notification_failures:
        lines.append("User notifications failed:")
        for uid in notification_failures:
            lines.append(f"• {uid}")
        lines.append("")

    lines.append("Permission: Analytics read-only")

    return "\n".join(lines)
