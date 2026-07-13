"""Phase 1 data contracts for the Slack analytics bot.

All structures in this module are **pure data** — no database queries,
no LLM calls, no Slack API interaction.  They define the stable boundary
between Iqra's Slack transport layer and Abdullah's analytics/LLM layer.

Conventions
-----------
* ``@dataclass(frozen=True)`` for internal immutable contracts — matches
  the pattern in ``apps/analytics/derive.py::DerivedMetric``.
* ``enum.StrEnum`` for platform and status enums — Python 3.12+ native,
  serializes as plain strings.
* ``uuid.UUID`` for all BrightBean entity identifiers — matches
  ``SocialAccount.id``, ``Workspace.id``, etc.
* ``datetime`` values must be timezone-aware; ``__post_init__`` rejects
  naive datetimes where the contract enforces it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Supported platform
# ---------------------------------------------------------------------------


class SupportedPlatform(StrEnum):
    """Platforms the Slack bot supports for analytics queries.

    Values are canonical lowercase strings matching the ``platform`` field
    on :class:`apps.social_accounts.models.SocialAccount`.

    Only Instagram, Facebook, and LinkedIn are supported in the MVP.
    LinkedIn maps to ``linkedin_company`` internally (LinkedIn Personal
    has no analytics surface — see
    :data:`apps.analytics.constants.NO_ANALYTICS_PLATFORMS`).
    """

    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"

    @classmethod
    def from_string(cls, value: str) -> SupportedPlatform:
        """Parse a platform string, raising ``ValueError`` if unsupported.

        Does **not** silently normalize unknown platforms — the caller
        must handle the error and report it to the user.
        """
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unsupported platform: {value!r}") from None


# ---------------------------------------------------------------------------
# Iqra → Abdullah integration boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackAnalyticsRequest:
    """Normalized analytics request from Iqra's Slack event pipeline.

    Iqra's background task produces this after:
    * signature verification
    * event deduplication
    * bot-mention removal and whitespace trimming
    * extraction of Slack identifiers

    Abdullah's :func:`build_analytics_response` consumes it.

    Fields
    ------
    correlation_id : str
        Stable ID for log correlation across the entire pipeline.
    event_id : str
        Slack's ``event_id`` (used for deduplication by Iqra).
    team_id : str
        Slack workspace/team ID (``T0123…``).
    channel_id : str
        Slack channel ID (``C0123…``).
    user_id : str
        Slack user ID of the person who mentioned the bot (``U0123…``).
    thread_ts : str
        Thread timestamp for the Slack thread.  When the original event
        was not in a thread, Iqra's normalization falls back to
        ``event_ts`` so the bot always replies in a thread anchored to
        the original message.  Never empty.
    text : str
        Normalized message text (bot mention removed, whitespace trimmed).
        Must be non-empty — Iqra's normalization rejects empty messages
        before this structure is created.
    """

    correlation_id: str
    event_id: str
    team_id: str
    channel_id: str
    user_id: str
    thread_ts: str
    text: str

    def __post_init__(self) -> None:
        if not self.correlation_id:
            raise ValueError("correlation_id must not be empty")
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not self.team_id:
            raise ValueError("team_id must not be empty")
        if not self.channel_id:
            raise ValueError("channel_id must not be empty")
        if not self.user_id:
            raise ValueError("user_id must not be empty")
        if not self.thread_ts:
            raise ValueError("thread_ts must not be empty — normalization should fall back to event_ts")
        if not self.text:
            raise ValueError("text must not be empty — normalization should have rejected it")


# ---------------------------------------------------------------------------
# Authorization context (application-created, never LLM-created)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """Application-created authorization context for analytics tools.

    **Security invariants:**
    * ``workspace_id``, ``user_id``, ``organization_id``, and
      ``allowed_account_ids`` must **never** come from LLM-generated
      arguments.  They are resolved by the authorization layer from
      Slack identifiers in :class:`SlackAnalyticsRequest`.
    * ``allowed_account_ids`` is a ``frozenset`` to prevent silent
      mutation by tool code.
    * No secrets (tokens, keys) are stored here.

    Fields
    ------
    workspace_id : uuid.UUID
        BrightBean workspace the Slack channel maps to.
    user_id : uuid.UUID
        BrightBean user the Slack user maps to.
    organization_id : uuid.UUID
        Owning organization (for org-scoped queries).
    allowed_account_ids : frozenset[uuid.UUID]
        Social account IDs the user is authorized to query.  Tools must
        reject any account_id not in this set.
    slack_team_id : str
        Slack team ID (audit context only).
    slack_channel_id : str
        Slack channel ID (audit context only).
    """

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    allowed_account_ids: frozenset[uuid.UUID]
    slack_team_id: str
    slack_channel_id: str

    def can_access_account(self, account_id: uuid.UUID) -> bool:
        """True when *account_id* is in the allowed set."""
        return account_id in self.allowed_account_ids


# ---------------------------------------------------------------------------
# Analytics period
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalyticsPeriod:
    """A closed date range ``[start, end]`` with a pre-computed ``days`` count.

    ``end`` is **inclusive** — a 7-day period ending today includes today.

    Conventions match :func:`apps.analytics.services.account_analytics_bundle`
    which uses ``end = timezone.now().date()`` and
    ``start = end - timedelta(days=2*days - 1)``.
    """

    start: date
    end: date
    days: int

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start ({self.start}) must not be after end ({self.end})")
        expected_days = (self.end - self.start).days + 1
        if self.days != expected_days:
            raise ValueError(
                f"days ({self.days}) must equal (end - start).days + 1 = {expected_days}"
            )
        if self.days < 1:
            raise ValueError("days must be at least 1")


# ---------------------------------------------------------------------------
# Account reference (safe, no secrets)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountReference:
    """Safe reference to a connected social account.

    Contains only display-safe fields — no tokens, no provider payloads.
    Used in :class:`ToolResult` and :class:`StructuredAnswer` so the
    formatter can show which account was queried without exposing secrets.
    """

    account_id: uuid.UUID
    platform: str
    display_name: str
    handle: str = ""


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------


class ToolResultStatus(StrEnum):
    """Outcome of an analytics tool execution."""

    SUCCESS = "success"
    NO_DATA = "no_data"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


@dataclass(frozen=True)
class ToolResult:
    """Normalized result returned by a BrightBean analytics tool.

    Represents successful, empty, clarification-required, and failed
    results unambiguously via :attr:`status`.

    Fields
    ------
    status : ToolResultStatus
        Outcome of the tool execution.
    tool_name : str
        Name of the tool that produced this result.
    platform : str | None
        Platform the result pertains to (``None`` for cross-platform tools).
    selected_account : AccountReference | None
        Account that was queried, if applicable.
    period : AnalyticsPeriod | None
        Requested/effective analytics period.
    data_as_of : datetime | None
        Most recent ``captured_at`` across the snapshots backing this result.
        Must be timezone-aware.
    last_synced_at : datetime | None
        When the background sync last refreshed this data.  Must be
        timezone-aware.
    is_stale : bool
        True when the data is older than the sync interval.
    data : dict[str, Any]
        Structured tool payload.  Typed per tool but kept as ``dict``
        to avoid a premature generic type system.  The formatter
        resolves exact values from this field deterministically.
    error_code : str | None
        Machine-readable error code from :class:`ErrorCode` when
        ``status`` is ``failed`` or ``no_data``.
    warnings : list[str]
        Non-fatal warning codes (e.g. ``stale_data``).
    """

    status: ToolResultStatus
    tool_name: str
    platform: str | None = None
    selected_account: AccountReference | None = None
    period: AnalyticsPeriod | None = None
    data_as_of: datetime | None = None
    last_synced_at: datetime | None = None
    is_stale: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.data_as_of is not None and self.data_as_of.tzinfo is None:
            raise ValueError("data_as_of must be timezone-aware")
        if self.last_synced_at is not None and self.last_synced_at.tzinfo is None:
            raise ValueError("last_synced_at must be timezone-aware")
        if self.status == ToolResultStatus.FAILED and self.error_code is None:
            raise ValueError("error_code must be set when status is 'failed'")
        if self.status != ToolResultStatus.FAILED and self.error_code is not None:
            raise ValueError("error_code must be None when status is not 'failed'")


# ---------------------------------------------------------------------------
# Structured answer (LLM qualitative interpretation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricRef:
    """A reference to a metric value from a :class:`ToolResult`.

    The formatter resolves the exact value from the tool result data —
    the LLM must not generate the value itself.  This structure lets the
    formatter validate that every number in the Slack response traces
    back to a real tool output.
    """

    tool_name: str
    metric_key: str
    label: str
    kind: str  # "count" | "percent" | "minutes"
    value: float
    delta: float | None = None


@dataclass(frozen=True)
class FreshnessRef:
    """Freshness metadata surfaced in the structured answer.

    Values come from :class:`ToolResult`, not from the LLM.
    """

    data_as_of: datetime | None
    last_synced_at: datetime | None
    is_stale: bool

    def __post_init__(self) -> None:
        if self.data_as_of is not None and self.data_as_of.tzinfo is None:
            raise ValueError("data_as_of must be timezone-aware")
        if self.last_synced_at is not None and self.last_synced_at.tzinfo is None:
            raise ValueError("last_synced_at must be timezone-aware")


@dataclass(frozen=True)
class StructuredAnswer:
    """Internal result of the LLM qualitative interpretation layer.

    The LLM generates :attr:`summary` (qualitative text) and
    :attr:`clarification` (when ambiguous).  Exact metric values are
    referenced via :attr:`metric_refs` — the formatter resolves them
    from :class:`ToolResult` data, not from LLM-generated numbers.

    Slack Block Kit, Claude response formats, and GLM response formats
    must **not** appear in this structure.
    """

    summary: str
    metric_refs: list[MetricRef] = field(default_factory=list)
    period_ref: AnalyticsPeriod | None = None
    freshness_ref: FreshnessRef | None = None
    warning_codes: list[str] = field(default_factory=list)
    clarification: str | None = None


# ---------------------------------------------------------------------------
# Slack response payload (Abdullah → Iqra delivery boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackResponsePayload:
    """Slack-formatted response returned by Abdullah's formatter.

    Iqra's delivery layer takes this and calls ``chat.postMessage``.

    Fields
    ------
    text : str
        Plain-text fallback (always required).  Slack displays this when
        Block Kit is not supported or as the notification preview.
    blocks : list[dict[str, Any]] | None
        Slack Block Kit blocks for rich rendering.  ``None`` means
        plain-text-only response.

    No Slack tokens, channel IDs, or thread timestamps are included —
    Iqra's delivery layer owns destination resolution from the original
    event.
    """

    text: str
    blocks: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("text must not be empty — it is the Slack fallback")


# ---------------------------------------------------------------------------
# Public analytics entrypoint contract
# ---------------------------------------------------------------------------


class AnalyticsEntrypoint(Protocol):
    """Protocol defining the public analytics entrypoint signature.

    Abdullah's implementation (Phase 2+) will resolve
    :class:`ToolContext` internally from the Slack identifiers in
    :class:`SlackAnalyticsRequest` — matching the existing pattern in
    ``apps/analytics/views.py`` (resolves workspace from request, then
    calls services) and ``apps/api/routers/analytics.py`` (resolves
    account from bearer token, then calls ``build_account_analytics``).

    Iqra's background task calls this entrypoint and passes the result
    to her delivery layer.  She does not need to know about
    :class:`ToolContext`.
    """

    def __call__(self, request: SlackAnalyticsRequest) -> SlackResponsePayload: ...


# The intended concrete function name (documented for Iqra's integration).
#
# def build_analytics_response(
#     request: SlackAnalyticsRequest,
# ) -> SlackResponsePayload:
#     ...
#
# This will be implemented in Phase 2 after authorization resolution
# is available.  It is not implemented here to avoid placeholder
# production logic.
