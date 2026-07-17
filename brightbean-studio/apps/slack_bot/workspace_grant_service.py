"""Workspace resolution for administrator access grants.

Resolves which BrightBean workspace a Slack user should be granted
access to, using two strategies:

1. **Mention-first**: If the user has an ``UnauthorizedAccessAttempt``
   with a ``last_source_channel_id``, resolve the
   ``SlackChannelMapping`` for that channel.

2. **Proactive fallback**: If no attempt exists (or the attempt has no
   source channel), find active ``SlackChannelMapping`` records for the
   Slack team.  If exactly one exists, use it.  Zero or multiple
   mappings result in a controlled failure.

This service does **not**:
- use the admin DM channel;
- hardcode a channel or workspace;
- trust workspace IDs from message text;
- call Slack, the LLM, or any external API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import SlackChannelMapping, UnauthorizedAccessAttempt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceResolutionResult:
    """Outcome of workspace resolution for a grant.

    Attributes:
        ok: True if resolution succeeded.
        source_channel_id: The Slack channel ID used for resolution.
        workspace_id: The BrightBean workspace UUID (str).
        workspace_name: The BrightBean workspace name (safe to display).
        failure_reason: Human-facing failure message (empty when ok).
    """

    ok: bool
    source_channel_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    failure_reason: str = ""


# ---------------------------------------------------------------------------
# Failure messages
# ---------------------------------------------------------------------------

_NO_MAPPING_MSG = (
    "No BrightBean workspace is mapped to this Slack workspace."
)

_AMBIGUOUS_MSG = (
    "Multiple BrightBean workspaces are mapped. "
    "Ask the user to mention the bot in the intended channel first."
)

_ARCHIVED_MSG = "The mapped workspace is archived."

_NOT_MAPPED_MSG = "No workspace mapping found for the source channel."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_grant_workspace(
    *,
    team_id: str,
    target_slack_user_id: str,
) -> WorkspaceResolutionResult:
    """Resolve the BrightBean workspace for a grant operation.

    Resolution order:
    1. ``UnauthorizedAccessAttempt`` → ``last_source_channel_id``
       → ``SlackChannelMapping`` → active workspace.
    2. Fallback: exactly one active ``SlackChannelMapping`` for the team.
    3. Zero mappings → fail with ``_NO_MAPPING_MSG``.
    4. Multiple mappings → fail with ``_AMBIGUOUS_MSG``.

    Returns a :class:`WorkspaceResolutionResult`.
    """
    # --- 1. Try mention-first resolution ---
    attempt = UnauthorizedAccessAttempt.objects.filter(
        workspace_id=team_id,
        slack_user_id=target_slack_user_id,
    ).first()

    if attempt and attempt.last_source_channel_id:
        channel_id = attempt.last_source_channel_id
        mapping = (
            SlackChannelMapping.objects
            .select_related("workspace")
            .filter(team_id=team_id, channel_id=channel_id)
            .first()
        )
        if mapping is not None:
            if mapping.workspace.is_archived:
                return WorkspaceResolutionResult(
                    ok=False,
                    source_channel_id=channel_id,
                    failure_reason=_ARCHIVED_MSG,
                )
            return WorkspaceResolutionResult(
                ok=True,
                source_channel_id=channel_id,
                workspace_id=str(mapping.workspace.id),
                workspace_name=mapping.workspace.name,
            )
        # Source channel not mapped — fall through to fallback

    # --- 2. Fallback: active SlackChannelMapping records for the team ---
    active_mappings = list(
        SlackChannelMapping.objects
        .select_related("workspace")
        .filter(team_id=team_id, workspace__is_archived=False)
    )

    if len(active_mappings) == 0:
        return WorkspaceResolutionResult(
            ok=False,
            failure_reason=_NO_MAPPING_MSG,
        )

    if len(active_mappings) > 1:
        return WorkspaceResolutionResult(
            ok=False,
            failure_reason=_AMBIGUOUS_MSG,
        )

    mapping = active_mappings[0]
    return WorkspaceResolutionResult(
        ok=True,
        source_channel_id=mapping.channel_id,
        workspace_id=str(mapping.workspace.id),
        workspace_name=mapping.workspace.name,
    )
