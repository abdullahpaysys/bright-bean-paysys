"""Canonical Slack-to-BrightBean access provisioning service.

This module owns the one production grant/revoke path.  It resolves Slack
identity through an injectable client, validates company email domains, and
updates BrightBean membership plus bot access inside one transaction.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .authorization import resolve_tool_context
from .constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_REVOKED,
    ADMIN_STATUS_ACTIVE,
    AUDIT_ACCESS_ALREADY_PRESENT,
    AUDIT_ACCESS_GRANTED,
    AUDIT_ACCESS_RESTORED,
    AUDIT_ACCESS_REVOKED,
    PERMISSION_READ_ONLY,
)
from .contracts import SlackAnalyticsRequest
from .models import (
    BotAccessAuditLog,
    BotAdministrator,
    BotUserAccess,
    SlackChannelMapping,
    SlackUserMapping,
)
from .slack_id_validation import is_valid_member_id
from .slack_identity import (
    SlackIdentityClient,
    SlackIdentityError,
    SlackIdentityErrorCode,
    SlackUserIdentity,
    SlackWebIdentityClient,
)
from .workspace_grant_service import resolve_grant_workspace

logger = logging.getLogger(__name__)


class ProvisioningStatus(StrEnum):
    NEWLY_PROVISIONED = "newly_provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    REPAIRED = "repaired"
    RESTORED = "restored"
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    FAILED = "failed"


class ProvisioningFailureReason(StrEnum):
    INVALID_SLACK_USER_ID = "INVALID_SLACK_USER_ID"
    SLACK_USER_NOT_FOUND = "SLACK_USER_NOT_FOUND"
    SLACK_PROFILE_UNAVAILABLE = "SLACK_PROFILE_UNAVAILABLE"
    SLACK_USER_INACTIVE = "SLACK_USER_INACTIVE"
    SLACK_USER_IS_BOT = "SLACK_USER_IS_BOT"
    SLACK_GUEST_NOT_ALLOWED = "SLACK_GUEST_NOT_ALLOWED"
    SLACK_EMAIL_MISSING = "SLACK_EMAIL_MISSING"
    COMPANY_EMAIL_REQUIRED = "COMPANY_EMAIL_REQUIRED"
    ADMIN_NOT_AUTHORIZED = "ADMIN_NOT_AUTHORIZED"
    NOT_ADMIN = "not_admin"  # legacy alias
    WORKSPACE_NOT_CONFIGURED = "WORKSPACE_NOT_CONFIGURED"
    CHANNEL_NOT_MAPPED = "channel_not_mapped"  # legacy alias
    WORKSPACE_ARCHIVED = "workspace_archived"
    BRIGHTBEAN_USER_CONFLICT = "BRIGHTBEAN_USER_CONFLICT"
    USER_INACTIVE = "user_inactive"
    EMAIL_MISMATCH = "email_mismatch"
    NO_VIEW_ANALYTICS = "no_view_analytics"
    POST_CHECK_FAILED = "post_check_failed"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    ALREADY_APPROVED = "ALREADY_APPROVED"
    ALREADY_REVOKED = "ALREADY_REVOKED"


@dataclass(frozen=True)
class ProvisioningResult:
    status: ProvisioningStatus
    target_slack_user_id: str
    brightbean_email: str = ""
    workspace_name: str = ""
    bot_access_action: str = ""
    mapping_action: str = ""
    org_membership_action: str = ""
    ws_membership_action: str = ""
    failure_reason: str = ""
    failure_message: str = ""
    correlation_id: str = ""


_DIAGNOSTIC_TEXT = "show me workspace analytics overview"
_AUDIT_GRANT_FAILED = "ACCESS_GRANT_FAILED"
_AUDIT_REVOKE_FAILED = "ACCESS_REVOKE_FAILED"


def _allowed_email_domains() -> set[str]:
    raw = (
        getattr(settings, "SLACK_ALLOWED_EMAIL_DOMAINS", "")
        or os.environ.get("SLACK_ALLOWED_EMAIL_DOMAINS", "")
    )
    return {
        part.strip().lower()
        for part in str(raw).split(",")
        if part.strip()
    }


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _email_domain(email: str) -> str:
    normalized = _normalize_email(email)
    if normalized.count("@") != 1:
        return ""
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return ""
    return domain


def _validate_company_email(email: str) -> str:
    normalized = _normalize_email(email)
    domain = _email_domain(normalized)
    allowed = _allowed_email_domains()
    if not normalized or not domain or not allowed or domain not in allowed:
        raise ValueError(ProvisioningFailureReason.COMPANY_EMAIL_REQUIRED)
    return normalized


def _safe_name(identity: SlackUserIdentity) -> str:
    return (identity.real_name or identity.display_name or "").strip()


def _audit(
    *,
    workspace_id: str,
    target_slack_user_id: str,
    performed_by_slack_user_id: str,
    action: str,
    result: str,
    reason: str = "",
    email: str = "",
    correlation_id: str = "",
    source: str = "",
    workspace_uuid: str = "",
    source_channel_id: str = "",
    extra: dict | None = None,
) -> None:
    metadata = {
        "result": result,
        "reason": reason,
        "target_email": email,
        "correlation_id": correlation_id,
        "source": source,
        "brightbean_workspace_id": workspace_uuid,
        "source_channel_id": source_channel_id,
    }
    if extra:
        metadata.update(extra)
    BotAccessAuditLog.objects.create(
        workspace_id=workspace_id,
        target_slack_user_id=target_slack_user_id,
        performed_by_slack_user_id=performed_by_slack_user_id,
        action=action,
        metadata={k: v for k, v in metadata.items() if v not in ("", None)},
    )


def _failed(
    *,
    target_slack_user_id: str,
    reason: ProvisioningFailureReason | str,
    message: str,
    admin_slack_user_id: str = "",
    slack_team_id: str = "",
    correlation_id: str = "",
    source: str = "",
    audit: bool = True,
) -> ProvisioningResult:
    reason_value = reason.value if isinstance(reason, ProvisioningFailureReason) else str(reason)
    if audit and slack_team_id:
        _audit(
            workspace_id=slack_team_id,
            target_slack_user_id=target_slack_user_id,
            performed_by_slack_user_id=admin_slack_user_id,
            action=_AUDIT_GRANT_FAILED,
            result="failed",
            reason=reason_value,
            correlation_id=correlation_id,
            source=source,
        )
    return ProvisioningResult(
        status=ProvisioningStatus.FAILED,
        target_slack_user_id=target_slack_user_id,
        failure_reason=reason_value,
        failure_message=message,
        correlation_id=correlation_id,
    )


def _resolve_workspace_mapping(
    *,
    slack_team_id: str,
    target_slack_user_id: str,
    workspace=None,
):
    if workspace is not None:
        mapping = (
            SlackChannelMapping.objects
            .select_related("workspace", "workspace__organization")
            .filter(team_id=slack_team_id, workspace=workspace)
            .first()
        )
        if mapping is None:
            return None, ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED, (
                "No Slack channel mapping exists for the selected workspace."
            )
        return mapping, "", ""

    resolution = resolve_grant_workspace(
        team_id=slack_team_id,
        target_slack_user_id=target_slack_user_id,
    )
    if not resolution.ok:
        return None, ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED, resolution.failure_reason

    mapping = (
        SlackChannelMapping.objects
        .select_related("workspace", "workspace__organization")
        .filter(team_id=slack_team_id, channel_id=resolution.source_channel_id)
        .first()
    )
    if mapping is None:
        return None, ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED, (
            "Workspace mapping could not be resolved."
        )
    return mapping, "", ""


def _validate_slack_identity(
    *,
    identity: SlackUserIdentity,
    target_slack_user_id: str,
    slack_team_id: str,
) -> str:
    if identity.slack_user_id != target_slack_user_id:
        raise ValueError(ProvisioningFailureReason.INVALID_SLACK_USER_ID)
    if identity.team_id and identity.team_id != slack_team_id:
        raise ValueError(ProvisioningFailureReason.INVALID_SLACK_USER_ID)
    if identity.is_bot:
        raise ValueError(ProvisioningFailureReason.SLACK_USER_IS_BOT)
    if identity.is_deleted:
        raise ValueError(ProvisioningFailureReason.SLACK_USER_INACTIVE)
    if identity.is_guest:
        raise ValueError(ProvisioningFailureReason.SLACK_GUEST_NOT_ALLOWED)
    if not identity.email:
        raise ValueError(ProvisioningFailureReason.SLACK_EMAIL_MISSING)
    return _validate_company_email(identity.email)


def _identity_failure_reason(exc: SlackIdentityError) -> ProvisioningFailureReason:
    if exc.code == SlackIdentityErrorCode.INVALID_SLACK_USER_ID:
        return ProvisioningFailureReason.INVALID_SLACK_USER_ID
    if exc.code == SlackIdentityErrorCode.SLACK_USER_NOT_FOUND:
        return ProvisioningFailureReason.SLACK_USER_NOT_FOUND
    return ProvisioningFailureReason.SLACK_PROFILE_UNAVAILABLE


@transaction.atomic
def grant_slack_analytics_access(
    *,
    admin_slack_user_id: str | None = None,
    target_slack_user_id: str,
    slack_team_id: str | None = None,
    workspace=None,
    correlation_id: str | None = None,
    identity_client: SlackIdentityClient | None = None,
    source: str = "admin_dm",
    # Backward-compatible names used by pre-existing tests/callers.
    approving_slack_user_id: str | None = None,
    team_id: str | None = None,
    source_channel_id: str | None = None,
    brightbean_email: str | None = None,
) -> ProvisioningResult:
    """Grant complete bot and BrightBean analytics access atomically."""
    admin_id = admin_slack_user_id or approving_slack_user_id or ""
    team = slack_team_id or team_id or ""
    correlation = correlation_id or ""
    target = (target_slack_user_id or "").strip()

    if not is_valid_member_id(target):
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.INVALID_SLACK_USER_ID,
            message="Invalid Slack member ID.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    if not BotAdministrator.objects.filter(
        workspace_id=team,
        slack_user_id=admin_id,
        status=ADMIN_STATUS_ACTIVE,
    ).exists():
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.ADMIN_NOT_AUTHORIZED,
            message="Approving user is not an active bot administrator.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    if source_channel_id:
        mapping = (
            SlackChannelMapping.objects
            .select_related("workspace", "workspace__organization")
            .filter(team_id=team, channel_id=source_channel_id)
            .first()
        )
        if mapping is None:
            return _failed(
                target_slack_user_id=target,
                reason=ProvisioningFailureReason.WORKSPACE_NOT_CONFIGURED,
                message="Workspace mapping could not be resolved.",
                admin_slack_user_id=admin_id,
                slack_team_id=team,
                correlation_id=correlation,
                source=source,
            )
    else:
        mapping, failure_reason, failure_message = _resolve_workspace_mapping(
            slack_team_id=team,
            target_slack_user_id=target,
            workspace=workspace,
        )
        if mapping is None:
            return _failed(
                target_slack_user_id=target,
                reason=failure_reason,
                message=failure_message,
                admin_slack_user_id=admin_id,
                slack_team_id=team,
                correlation_id=correlation,
                source=source,
            )

    workspace_obj = mapping.workspace
    if workspace_obj.is_archived:
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.WORKSPACE_ARCHIVED,
            message="Workspace is archived.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    identity: SlackUserIdentity
    if identity_client is None and brightbean_email:
        # Legacy test/management compatibility only.  Production admin DMs do
        # not pass ``brightbean_email`` and therefore use SlackWebIdentityClient.
        identity = SlackUserIdentity(
            slack_user_id=target,
            team_id=team,
            email=brightbean_email,
        )
    else:
        client = identity_client or SlackWebIdentityClient()
        try:
            identity = client.get_user(slack_user_id=target)
        except SlackIdentityError as exc:
            return _failed(
                target_slack_user_id=target,
                reason=_identity_failure_reason(exc),
                message="Slack user profile is unavailable.",
                admin_slack_user_id=admin_id,
                slack_team_id=team,
                correlation_id=correlation,
                source=source,
            )

    try:
        normalized_email = _validate_slack_identity(
            identity=identity,
            target_slack_user_id=target,
            slack_team_id=team,
        )
    except ValueError as exc:
        reason = str(exc)
        return _failed(
            target_slack_user_id=target,
            reason=reason,
            message="Slack user is not eligible for BrightBean access.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    organization = workspace_obj.organization

    from apps.accounts.models import User
    from apps.members.models import OrgMembership, WorkspaceMembership

    existing_mapping = (
        SlackUserMapping.objects
        .select_for_update()
        .select_related("user")
        .filter(slack_user_id=target, team_id=team)
        .first()
    )

    user_created = False
    if existing_mapping is not None:
        brightbean_user = existing_mapping.user
        if brightbean_user.email.lower() != normalized_email:
            return _failed(
                target_slack_user_id=target,
                reason=ProvisioningFailureReason.BRIGHTBEAN_USER_CONFLICT,
                message="Slack user is already mapped to a different BrightBean user.",
                admin_slack_user_id=admin_id,
                slack_team_id=team,
                correlation_id=correlation,
                source=source,
            )
    else:
        existing_user = User.objects.select_for_update().filter(email__iexact=normalized_email).first()
        if existing_user is None:
            brightbean_user = User.objects.create_user(
                email=normalized_email,
                password=None,
                name=_safe_name(identity),
            )
            brightbean_user.set_unusable_password()
            brightbean_user.save(update_fields=["password", "updated_at"])
            user_created = True
        else:
            brightbean_user = existing_user

    if not brightbean_user.is_active:
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.USER_INACTIVE,
            message="BrightBean user is inactive.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    org_membership_action = "already_exists"
    org_membership = OrgMembership.objects.filter(
        user=brightbean_user,
        organization=organization,
    ).first()
    org_membership_created_by_bot = False
    if org_membership is None:
        OrgMembership.objects.create(
            user=brightbean_user,
            organization=organization,
            org_role=OrgMembership.OrgRole.MEMBER,
        )
        org_membership_action = "created"
        org_membership_created_by_bot = True

    ws_membership_action = "already_exists"
    ws_membership = WorkspaceMembership.objects.filter(
        user=brightbean_user,
        workspace=workspace_obj,
    ).first()
    ws_membership_created_by_bot = False
    if ws_membership is None:
        ws_membership = WorkspaceMembership.objects.create(
            user=brightbean_user,
            workspace=workspace_obj,
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
        )
        ws_membership_action = "created"
        ws_membership_created_by_bot = True

    permissions = ws_membership.effective_permissions
    if not permissions or not permissions.get("view_analytics", False):
        transaction.set_rollback(True)
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.NO_VIEW_ANALYTICS,
            message="User's workspace role does not include view_analytics.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
            audit=False,
        )

    mapping_action = "already_exists"
    if existing_mapping is None:
        SlackUserMapping.objects.create(
            slack_user_id=target,
            team_id=team,
            user=brightbean_user,
        )
        mapping_action = "created"

    access = BotUserAccess.objects.select_for_update().filter(
        workspace_id=team,
        slack_user_id=target,
    ).first()
    bot_access_action = "already_approved"
    if access is None:
        BotUserAccess.objects.create(
            workspace_id=team,
            slack_user_id=target,
            status=ACCESS_STATUS_APPROVED,
            permission=PERMISSION_READ_ONLY,
            granted_by_slack_user_id=admin_id,
            brightbean_user=brightbean_user,
            brightbean_workspace=workspace_obj,
            bot_created_org_membership=org_membership_created_by_bot,
            bot_created_workspace_membership=ws_membership_created_by_bot,
        )
        bot_access_action = "created"
    elif access.status == ACCESS_STATUS_REVOKED:
        access.status = ACCESS_STATUS_APPROVED
        access.permission = PERMISSION_READ_ONLY
        access.revoked_at = None
        access.granted_by_slack_user_id = admin_id
        access.brightbean_user = brightbean_user
        access.brightbean_workspace = workspace_obj
        access.bot_created_org_membership = org_membership_created_by_bot
        access.bot_created_workspace_membership = ws_membership_created_by_bot
        access.save(update_fields=[
            "status",
            "permission",
            "revoked_at",
            "granted_by_slack_user_id",
            "brightbean_user",
            "brightbean_workspace",
            "bot_created_org_membership",
            "bot_created_workspace_membership",
            "updated_at",
        ])
        bot_access_action = "restored"
    else:
        access_updates: list[str] = []
        if access.brightbean_user_id != brightbean_user.id:
            access.brightbean_user = brightbean_user
            access_updates.append("brightbean_user")
        if access.brightbean_workspace_id != workspace_obj.id:
            access.brightbean_workspace = workspace_obj
            access_updates.append("brightbean_workspace")
        if ws_membership_created_by_bot and not access.bot_created_workspace_membership:
            access.bot_created_workspace_membership = True
            access_updates.append("bot_created_workspace_membership")
        if org_membership_created_by_bot and not access.bot_created_org_membership:
            access.bot_created_org_membership = True
            access_updates.append("bot_created_org_membership")
        if access_updates:
            access_updates.append("updated_at")
            access.save(update_fields=access_updates)

    request = SlackAnalyticsRequest(
        correlation_id=correlation or f"provision-{target}",
        event_id=correlation or f"provision-{target}",
        team_id=team,
        channel_id=mapping.channel_id,
        user_id=target,
        thread_ts="",
        text=_DIAGNOSTIC_TEXT,
    )

    try:
        context = resolve_tool_context(request)
    except Exception:
        transaction.set_rollback(True)
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            message="Authorization post-check failed after provisioning.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
            audit=False,
        )

    if (
        context.workspace_id != workspace_obj.id
        or context.user_id != brightbean_user.id
        or context.organization_id != organization.id
    ):
        transaction.set_rollback(True)
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.POST_CHECK_FAILED,
            message="Authorization post-check mismatch.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
            audit=False,
        )

    if bot_access_action == "restored":
        final_status = ProvisioningStatus.RESTORED
        audit_action = AUDIT_ACCESS_RESTORED
    elif (
        bot_access_action == "already_approved"
        and mapping_action == "already_exists"
        and org_membership_action == "already_exists"
        and ws_membership_action == "already_exists"
        and not user_created
    ):
        final_status = ProvisioningStatus.ALREADY_PROVISIONED
        audit_action = AUDIT_ACCESS_ALREADY_PRESENT
    elif bot_access_action == "already_approved":
        final_status = ProvisioningStatus.REPAIRED
        audit_action = AUDIT_ACCESS_GRANTED
    else:
        final_status = ProvisioningStatus.NEWLY_PROVISIONED
        audit_action = AUDIT_ACCESS_GRANTED

    _audit(
        workspace_id=team,
        target_slack_user_id=target,
        performed_by_slack_user_id=admin_id,
        action=audit_action,
        result=final_status.value,
        email=normalized_email,
        correlation_id=correlation,
        source=source,
        workspace_uuid=str(workspace_obj.id),
        source_channel_id=mapping.channel_id,
        extra={
            "brightbean_user_created": user_created,
            "bot_created_org_membership": org_membership_created_by_bot,
            "bot_created_workspace_membership": ws_membership_created_by_bot,
            "mapping_action": mapping_action,
            "bot_access_action": bot_access_action,
        },
    )

    return ProvisioningResult(
        status=final_status,
        target_slack_user_id=target,
        brightbean_email=brightbean_user.email,
        workspace_name=workspace_obj.name,
        bot_access_action=bot_access_action,
        mapping_action=mapping_action,
        org_membership_action=org_membership_action,
        ws_membership_action=ws_membership_action,
        correlation_id=correlation,
    )


def _latest_bot_grant_metadata(*, team: str, target: str) -> dict:
    log = (
        BotAccessAuditLog.objects
        .filter(
            workspace_id=team,
            target_slack_user_id=target,
            action__in=[
                AUDIT_ACCESS_GRANTED,
                AUDIT_ACCESS_RESTORED,
                AUDIT_ACCESS_ALREADY_PRESENT,
            ],
        )
        .order_by("-created_at")
        .first()
    )
    return dict(log.metadata or {}) if log else {}


@transaction.atomic
def revoke_slack_analytics_access(
    *,
    admin_slack_user_id: str,
    target_slack_user_id: str,
    slack_team_id: str,
    correlation_id: str | None = None,
    source: str = "admin_dm",
) -> ProvisioningResult:
    """Revoke bot access and bot-managed BrightBean workspace access."""
    admin_id = (admin_slack_user_id or "").strip()
    team = (slack_team_id or "").strip()
    target = (target_slack_user_id or "").strip()
    correlation = correlation_id or ""

    if not is_valid_member_id(target):
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.INVALID_SLACK_USER_ID,
            message="Invalid Slack member ID.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    if not BotAdministrator.objects.filter(
        workspace_id=team,
        slack_user_id=admin_id,
        status=ADMIN_STATUS_ACTIVE,
    ).exists():
        return _failed(
            target_slack_user_id=target,
            reason=ProvisioningFailureReason.ADMIN_NOT_AUTHORIZED,
            message="Approving user is not an active bot administrator.",
            admin_slack_user_id=admin_id,
            slack_team_id=team,
            correlation_id=correlation,
            source=source,
        )

    access = BotUserAccess.objects.select_for_update().select_related(
        "brightbean_user",
        "brightbean_workspace",
        "brightbean_workspace__organization",
    ).filter(
        workspace_id=team,
        slack_user_id=target,
    ).first()
    mapping = (
        SlackUserMapping.objects
        .select_related("user")
        .filter(team_id=team, slack_user_id=target)
        .first()
    )

    if access is None or access.status == ACCESS_STATUS_REVOKED:
        _audit(
            workspace_id=team,
            target_slack_user_id=target,
            performed_by_slack_user_id=admin_id,
            action=AUDIT_ACCESS_REVOKED,
            result=ProvisioningStatus.ALREADY_REVOKED.value,
            reason=ProvisioningFailureReason.ALREADY_REVOKED,
            correlation_id=correlation,
            source=source,
        )
        return ProvisioningResult(
            status=ProvisioningStatus.ALREADY_REVOKED,
            target_slack_user_id=target,
            bot_access_action="already_revoked",
            failure_reason=ProvisioningFailureReason.ALREADY_REVOKED,
            correlation_id=correlation,
        )

    access.status = ACCESS_STATUS_REVOKED
    access.revoked_at = timezone.now()
    access.save(update_fields=["status", "revoked_at", "updated_at"])

    legacy_metadata = _latest_bot_grant_metadata(team=team, target=target)
    workspace_obj = access.brightbean_workspace
    workspace_uuid = str(workspace_obj.id) if workspace_obj is not None else ""
    workspace_name = ""
    brightbean_email = ""
    ws_removed = False
    org_removed = False

    if mapping is not None:
        brightbean_user = mapping.user
        brightbean_email = brightbean_user.email

        from apps.members.models import OrgMembership, WorkspaceMembership

        if workspace_obj is None:
            workspace_uuid = str(legacy_metadata.get("brightbean_workspace_id", ""))

        if workspace_obj is not None:
            workspace_name = workspace_obj.name
            ws_membership = WorkspaceMembership.objects.filter(
                user=brightbean_user,
                workspace=workspace_obj,
            ).first()
            if (
                access.bot_created_workspace_membership
                and ws_membership is not None
                and ws_membership.workspace_role == WorkspaceMembership.WorkspaceRole.VIEWER
                and ws_membership.custom_role_id is None
            ):
                ws_membership.delete()
                ws_removed = True

            remaining = WorkspaceMembership.objects.filter(
                user=brightbean_user,
                workspace__organization=workspace_obj.organization,
            ).exists()
            org_membership = OrgMembership.objects.filter(
                user=brightbean_user,
                organization=workspace_obj.organization,
            ).first()
            if (
                access.bot_created_org_membership
                and not remaining
                and org_membership is not None
                and org_membership.org_role == OrgMembership.OrgRole.MEMBER
            ):
                org_membership.delete()
                org_removed = True

    _audit(
        workspace_id=team,
        target_slack_user_id=target,
        performed_by_slack_user_id=admin_id,
        action=AUDIT_ACCESS_REVOKED,
        result=ProvisioningStatus.REVOKED.value,
        email=brightbean_email,
        correlation_id=correlation,
        source=source,
        workspace_uuid=workspace_uuid,
        extra={
            "bot_access_action": "revoked",
            "bot_removed_workspace_membership": ws_removed,
            "bot_removed_org_membership": org_removed,
        },
    )

    return ProvisioningResult(
        status=ProvisioningStatus.REVOKED,
        target_slack_user_id=target,
        brightbean_email=brightbean_email,
        workspace_name=workspace_name,
        bot_access_action="revoked",
        ws_membership_action="removed" if ws_removed else "preserved",
        org_membership_action="removed" if org_removed else "preserved",
        correlation_id=correlation,
    )
