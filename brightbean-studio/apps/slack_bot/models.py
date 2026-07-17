"""Models for the Slack analytics bot.

Phase 4: minimal event persistence + deduplication by ``event_id``.
"""

from __future__ import annotations

import uuid
from typing import cast

from django.conf import settings
from django.db import models

from .constants import (
    ACCESS_STATUS_APPROVED,
    ACCESS_STATUS_CHOICES,
    ADMIN_STATUS_ACTIVE,
    ADMIN_STATUS_CHOICES,
    AUDIT_ACTION_CHOICES,
    PERMISSION_CHOICES,
    PERMISSION_READ_ONLY,
    STATUS_CHOICES,
    STATUS_RECEIVED,
    SYSTEM_ACTOR,
)


class SlackInboundEventManager(models.Manager):
    """Custom manager providing the deduplication helper."""

    def get_or_create_inbound_event(
        self,
        *,
        event_id: str,
        team_id: str,
        channel_id: str,
        user_id: str,
        event_ts: str,
        message_text: str = "",
        thread_ts: str | None = None,
    ) -> tuple[SlackInboundEvent, bool]:
        """Return ``(event, created)`` for the given ``event_id``.

        * If ``event_id`` is new → create with ``status=RECEIVED`` and
          return ``(event, True)``.
        * If ``event_id`` already exists → return the existing record and
          ``(event, False)``.

        No duplicate is ever created.  Database-level uniqueness on
        ``event_id`` is the final safety net.
        """
        defaults = {
            "team_id": team_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "event_ts": event_ts,
            "message_text": message_text,
            "thread_ts": thread_ts or "",
        }
        return cast(
            tuple[SlackInboundEvent, bool],
            self.get_or_create(event_id=event_id, defaults=defaults),
        )


class SlackInboundEvent(models.Model):
    """A single accepted Slack event, persisted for deduplication and audit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    correlation_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True
    )

    # Main deduplication key — Slack's unique event identifier.
    event_id = models.CharField(max_length=64, unique=True, db_index=True)

    team_id = models.CharField(max_length=64, db_index=True)
    channel_id = models.CharField(max_length=64, db_index=True)
    user_id = models.CharField(max_length=64, db_index=True)

    thread_ts = models.CharField(max_length=32, blank=True, default="")
    event_ts = models.CharField(max_length=32)
    message_text = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_RECEIVED,
        db_index=True,
    )

    response_ts = models.CharField(max_length=32, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SlackInboundEventManager()

    class Meta:
        db_table = "slack_bot_inbound_event"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["channel_id", "thread_ts"], name="slack_bot_chan_thread"),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} [{self.status}]"


# ---------------------------------------------------------------------------
# Phase 2 — Slack ↔ BrightBean identity mappings
# ---------------------------------------------------------------------------


class SlackChannelMapping(models.Model):
    """Maps a Slack ``(team_id, channel_id)`` pair to a BrightBean workspace.

    Created by an admin during onboarding.  When a Slack analytics request
    arrives, the resolver looks up this mapping to determine which
    workspace's social accounts are in scope.

    Security: the resolver **fails closed** — if no mapping exists, the
    request is rejected with ``ErrorCode.CHANNEL_NOT_MAPPED``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    team_id = models.CharField(max_length=64, db_index=True)
    channel_id = models.CharField(max_length=64, db_index=True)

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="slack_channel_mappings",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_channel_mapping"
        unique_together = [("team_id", "channel_id")]

    def __str__(self):
        return f"{self.team_id}/{self.channel_id} → {self.workspace.name}"


class SlackUserMapping(models.Model):
    """Maps a Slack ``(slack_user_id, team_id)`` pair to a BrightBean user.

    Created by an admin during onboarding.  When a Slack analytics request
    arrives, the resolver looks up this mapping to determine which
    BrightBean user is making the request — used for membership and
    permission checks.

    Security: the resolver **fails closed** — if no mapping exists, the
    request is rejected with ``ErrorCode.USER_NOT_MAPPED``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    slack_user_id = models.CharField(max_length=64, db_index=True)
    team_id = models.CharField(max_length=64, db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="slack_user_mappings",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_user_mapping"
        unique_together = [("slack_user_id", "team_id")]

    def __str__(self):
        return f"{self.slack_user_id} ({self.team_id}) → {self.user.email}"


# ---------------------------------------------------------------------------
# Phase 1 — Bot whitelisting models
# ---------------------------------------------------------------------------


class BotAdministrator(models.Model):
    """The single configured bot administrator for a workspace.

    Only one record may exist per ``workspace_id`` (enforced by
    ``unique=True`` on that field).  Created manually via the
    ``create_bot_admin`` management command.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace_id = models.CharField(max_length=64, unique=True, db_index=True)
    slack_user_id = models.CharField(max_length=64, db_index=True)

    status = models.CharField(
        max_length=16,
        choices=ADMIN_STATUS_CHOICES,
        default=ADMIN_STATUS_ACTIVE,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_administrator"

    def __str__(self) -> str:
        return f"{self.workspace_id} → admin {self.slack_user_id} [{self.status}]"


class BotUserAccess(models.Model):
    """Approved or revoked bot access for a Slack user in a workspace.

    ``workspace_id`` + ``slack_user_id`` is unique — no duplicate
    access rows for the same user in the same workspace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace_id = models.CharField(max_length=64, db_index=True)
    slack_user_id = models.CharField(max_length=64, db_index=True)

    status = models.CharField(
        max_length=16,
        choices=ACCESS_STATUS_CHOICES,
        default=ACCESS_STATUS_APPROVED,
        db_index=True,
    )
    permission = models.CharField(
        max_length=16,
        choices=PERMISSION_CHOICES,
        default=PERMISSION_READ_ONLY,
    )

    granted_by_slack_user_id = models.CharField(max_length=64, default=SYSTEM_ACTOR)
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # Durable Phase 1 provisioning provenance. ``workspace_id`` above is the
    # Slack team/workspace ID; these fields point at the BrightBean objects
    # touched by the canonical provisioning flow. Revoke uses the booleans to
    # remove only memberships that the bot created, never manually managed
    # memberships.
    brightbean_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="slack_bot_access_records",
    )
    brightbean_workspace = models.ForeignKey(
        "workspaces.Workspace",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="slack_bot_access_records",
    )
    bot_created_org_membership = models.BooleanField(default=False)
    bot_created_workspace_membership = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_user_access"
        unique_together = [("workspace_id", "slack_user_id")]

    def __str__(self) -> str:
        return f"{self.workspace_id}/{self.slack_user_id} [{self.status}]"


class UnauthorizedAccessAttempt(models.Model):
    """Tracks unauthorized access attempts for the future 24-hour notification.

    ``workspace_id`` + ``slack_user_id`` is unique.  The
    ``last_admin_notification_at`` field is nullable and will be used
    in a later phase to enforce one admin DM per user per 24 hours.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace_id = models.CharField(max_length=64, db_index=True)
    slack_user_id = models.CharField(max_length=64, db_index=True)

    attempt_count = models.PositiveIntegerField(default=0)
    first_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_admin_notification_at = models.DateTimeField(null=True, blank=True)

    last_source_channel_id = models.CharField(max_length=64, blank=True, default="")
    last_message_ts = models.CharField(max_length=32, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slack_bot_unauth_access_attempt"
        unique_together = [("workspace_id", "slack_user_id")]

    def __str__(self) -> str:
        return f"{self.workspace_id}/{self.slack_user_id} attempts={self.attempt_count}"


class BotAccessAuditLog(models.Model):
    """Append-only audit log for bot access management actions.

    Records are never updated or deleted in normal application logic.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace_id = models.CharField(max_length=64, db_index=True)
    target_slack_user_id = models.CharField(max_length=64, db_index=True)
    performed_by_slack_user_id = models.CharField(max_length=64, default=SYSTEM_ACTOR)
    action = models.CharField(
        max_length=64,
        choices=AUDIT_ACTION_CHOICES,
        db_index=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "slack_bot_access_audit_log"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.workspace_id} {self.action} → {self.target_slack_user_id}"
