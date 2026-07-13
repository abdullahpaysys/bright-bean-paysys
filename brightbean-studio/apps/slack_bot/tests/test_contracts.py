"""Tests for Phase 1 contracts and schemas.

All tests are deterministic — no network calls, no database access,
no real Slack/Claude/Z.AI/social-platform interaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.slack_bot.contracts import (
    AnalyticsPeriod,
    FreshnessRef,
    MetricRef,
    SlackAnalyticsRequest,
    SlackResponsePayload,
    StructuredAnswer,
    SupportedPlatform,
    ToolContext,
    ToolResult,
    ToolResultStatus,
)
from apps.slack_bot.errors import ErrorCode
from apps.slack_bot.schemas import (
    ComparePlatformsInput,
    GetAccountStatsInput,
    GetEngagementSummaryInput,
    GetFollowerGrowthInput,
    GetPostDetailInput,
    GetTopPostsInput,
    ListConnectedAccountsInput,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_uuid() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tz_aware_dt() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def slack_request() -> SlackAnalyticsRequest:
    return SlackAnalyticsRequest(
        correlation_id="corr-123",
        event_id="ev-123",
        team_id="T0123",
        channel_id="C0123",
        user_id="U0123",
        thread_ts="1234567890.123",
        text="What was our top Instagram post this week?",
    )


@pytest.fixture
def tool_context(valid_uuid) -> ToolContext:
    return ToolContext(
        workspace_id=valid_uuid,
        user_id=valid_uuid,
        organization_id=valid_uuid,
        allowed_account_ids=frozenset({uuid.uuid4(), uuid.uuid4()}),
        slack_team_id="T0123",
        slack_channel_id="C0123",
    )


# ---------------------------------------------------------------------------
# SlackAnalyticsRequest
# ---------------------------------------------------------------------------


class TestSlackAnalyticsRequest:
    def test_valid_request_with_thread(self, slack_request):
        assert slack_request.thread_ts == "1234567890.123"
        assert slack_request.text == "What was our top Instagram post this week?"

    def test_valid_request_without_thread(self):
        req = SlackAnalyticsRequest(
            correlation_id="c1",
            event_id="e1",
            team_id="T1",
            channel_id="C1",
            user_id="U1",
            thread_ts="1720000000.000100",
            text="help",
        )
        assert req.thread_ts == "1720000000.000100"

    @pytest.mark.parametrize("field", ["correlation_id", "event_id", "team_id", "channel_id", "user_id", "thread_ts", "text"])
    def test_empty_required_field_rejected(self, field):
        kwargs = dict(
            correlation_id="c1",
            event_id="e1",
            team_id="T1",
            channel_id="C1",
            user_id="U1",
            thread_ts="1720000000.000100",
            text="hello",
        )
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must not be empty"):
            SlackAnalyticsRequest(**kwargs)

    def test_is_frozen(self, slack_request):
        with pytest.raises(AttributeError):
            slack_request.text = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------


class TestToolContext:
    def test_valid_context(self, tool_context):
        assert len(tool_context.allowed_account_ids) == 2

    def test_is_frozen(self, tool_context):
        with pytest.raises(AttributeError):
            tool_context.workspace_id = uuid.uuid4()  # type: ignore[misc]

    def test_allowed_accounts_is_frozenset(self, tool_context):
        assert isinstance(tool_context.allowed_account_ids, frozenset)

    def test_can_access_account_true(self, valid_uuid):
        account_id = uuid.uuid4()
        ctx = ToolContext(
            workspace_id=valid_uuid,
            user_id=valid_uuid,
            organization_id=valid_uuid,
            allowed_account_ids=frozenset({account_id}),
            slack_team_id="T1",
            slack_channel_id="C1",
        )
        assert ctx.can_access_account(account_id) is True

    def test_can_access_account_false(self, valid_uuid):
        ctx = ToolContext(
            workspace_id=valid_uuid,
            user_id=valid_uuid,
            organization_id=valid_uuid,
            allowed_account_ids=frozenset({uuid.uuid4()}),
            slack_team_id="T1",
            slack_channel_id="C1",
        )
        assert ctx.can_access_account(uuid.uuid4()) is False

    def test_no_secret_fields(self):
        """ToolContext must not have token or key fields."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(ToolContext)}
        forbidden = {"oauth_access_token", "oauth_refresh_token", "api_key", "signing_secret", "bot_token"}
        assert not (field_names & forbidden), f"ToolContext has forbidden fields: {field_names & forbidden}"


# ---------------------------------------------------------------------------
# SupportedPlatform
# ---------------------------------------------------------------------------


class TestSupportedPlatform:
    def test_instagram_accepted(self):
        assert SupportedPlatform.from_string("instagram") == SupportedPlatform.INSTAGRAM

    def test_facebook_accepted(self):
        assert SupportedPlatform.from_string("facebook") == SupportedPlatform.FACEBOOK

    def test_linkedin_accepted(self):
        assert SupportedPlatform.from_string("linkedin") == SupportedPlatform.LINKEDIN

    def test_unsupported_rejected(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            SupportedPlatform.from_string("tiktok")

    def test_case_insensitive(self):
        assert SupportedPlatform.from_string("Instagram") == SupportedPlatform.INSTAGRAM

    def test_str_serialization(self):
        assert str(SupportedPlatform.INSTAGRAM) == "instagram"


# ---------------------------------------------------------------------------
# ErrorCode
# ---------------------------------------------------------------------------


class TestErrorCode:
    def test_no_duplicate_values(self):
        values = [member.value for member in ErrorCode]
        assert len(values) == len(set(values)), "Duplicate error code values"

    def test_warning_codes(self):
        assert ErrorCode.is_warning(ErrorCode.NO_DATA) is True
        assert ErrorCode.is_warning(ErrorCode.STALE_DATA) is True

    def test_fatal_codes(self):
        assert ErrorCode.is_fatal(ErrorCode.UNAUTHORIZED) is True
        assert ErrorCode.is_fatal(ErrorCode.TOOL_NOT_FOUND) is True
        assert ErrorCode.is_fatal(ErrorCode.NO_CONNECTED_ACCOUNT) is True

    def test_warning_via_string(self):
        assert ErrorCode.is_warning("no_data") is True
        assert ErrorCode.is_fatal("unauthorized") is True

    def test_str_serialization(self):
        assert str(ErrorCode.UNAUTHORIZED) == "unauthorized"


# ---------------------------------------------------------------------------
# AnalyticsPeriod
# ---------------------------------------------------------------------------


class TestAnalyticsPeriod:
    def test_valid_period(self):
        end = date(2026, 7, 13)
        start = end - timedelta(days=6)
        period = AnalyticsPeriod(start=start, end=end, days=7)
        assert period.days == 7

    def test_start_after_end_rejected(self):
        with pytest.raises(ValueError, match="must not be after"):
            AnalyticsPeriod(start=date(2026, 7, 14), end=date(2026, 7, 13), days=1)

    def test_mismatched_days_rejected(self):
        end = date(2026, 7, 13)
        start = end - timedelta(days=6)
        with pytest.raises(ValueError, match="must equal"):
            AnalyticsPeriod(start=start, end=end, days=10)

    def test_single_day(self):
        d = date(2026, 7, 13)
        period = AnalyticsPeriod(start=d, end=d, days=1)
        assert period.days == 1

    def test_is_frozen(self):
        period = AnalyticsPeriod(start=date(2026, 7, 1), end=date(2026, 7, 7), days=7)
        with pytest.raises(AttributeError):
            period.days = 30  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


class TestToolResult:
    def test_successful_result(self, tz_aware_dt):
        result = ToolResult(
            status=ToolResultStatus.SUCCESS,
            tool_name="get_account_stats",
            data_as_of=tz_aware_dt,
            data={"reach": 1000},
        )
        assert result.status == ToolResultStatus.SUCCESS
        assert result.error_code is None

    def test_no_data_result(self):
        result = ToolResult(
            status=ToolResultStatus.NO_DATA,
            tool_name="get_account_stats",
            error_code=None,
        )
        assert result.status == ToolResultStatus.NO_DATA

    def test_failed_result_requires_error_code(self):
        with pytest.raises(ValueError, match="error_code must be set"):
            ToolResult(
                status=ToolResultStatus.FAILED,
                tool_name="get_account_stats",
            )

    def test_non_failed_rejects_error_code(self):
        with pytest.raises(ValueError, match="error_code must be None"):
            ToolResult(
                status=ToolResultStatus.SUCCESS,
                tool_name="get_account_stats",
                error_code="unauthorized",
            )

    def test_failed_result_with_error_code(self):
        result = ToolResult(
            status=ToolResultStatus.FAILED,
            tool_name="get_account_stats",
            error_code=ErrorCode.UNAUTHORIZED,
        )
        assert result.error_code == "unauthorized"

    def test_timezone_naive_data_as_of_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            ToolResult(
                status=ToolResultStatus.SUCCESS,
                tool_name="get_account_stats",
                data_as_of=datetime(2026, 7, 13),  # naive
            )

    def test_timezone_naive_last_synced_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            ToolResult(
                status=ToolResultStatus.SUCCESS,
                tool_name="get_account_stats",
                last_synced_at=datetime(2026, 7, 13),  # naive
            )

    def test_is_frozen(self, tz_aware_dt):
        result = ToolResult(
            status=ToolResultStatus.SUCCESS,
            tool_name="get_account_stats",
            data_as_of=tz_aware_dt,
        )
        with pytest.raises(AttributeError):
            result.tool_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StructuredAnswer
# ---------------------------------------------------------------------------


class TestStructuredAnswer:
    def test_valid_answer(self, tz_aware_dt):
        answer = StructuredAnswer(
            summary="Your top Instagram post reached 18,942 people.",
            metric_refs=[
                MetricRef(
                    tool_name="get_top_posts",
                    metric_key="reach",
                    label="Reach",
                    kind="count",
                    value=18942.0,
                ),
            ],
            freshness_ref=FreshnessRef(
                data_as_of=tz_aware_dt,
                last_synced_at=tz_aware_dt,
                is_stale=False,
            ),
        )
        assert answer.summary == "Your top Instagram post reached 18,942 people."
        assert len(answer.metric_refs) == 1

    def test_clarification(self):
        answer = StructuredAnswer(
            summary="",
            clarification="Did you mean your LinkedIn Company Page or Personal Profile?",
        )
        assert answer.clarification is not None

    def test_no_slack_blocks_field(self):
        """StructuredAnswer must not contain Slack Block Kit data."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(StructuredAnswer)}
        assert "blocks" not in field_names
        assert "block_kit" not in field_names

    def test_is_frozen(self):
        answer = StructuredAnswer(summary="test")
        with pytest.raises(AttributeError):
            answer.summary = "modified"  # type: ignore[misc]

    def test_freshness_ref_rejects_naive_dt(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            FreshnessRef(
                data_as_of=datetime(2026, 7, 13),  # naive
                last_synced_at=None,
                is_stale=False,
            )


# ---------------------------------------------------------------------------
# SlackResponsePayload
# ---------------------------------------------------------------------------


class TestSlackResponsePayload:
    def test_text_only(self):
        payload = SlackResponsePayload(text="Hello!")
        assert payload.text == "Hello!"
        assert payload.blocks is None

    def test_with_blocks(self):
        payload = SlackResponsePayload(
            text="Fallback",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*Bold*"}}],
        )
        assert payload.blocks is not None
        assert len(payload.blocks) == 1

    def test_empty_text_rejected(self):
        with pytest.raises(ValueError, match="text must not be empty"):
            SlackResponsePayload(text="")

    def test_is_frozen(self):
        payload = SlackResponsePayload(text="Hello!")
        with pytest.raises(AttributeError):
            payload.text = "modified"  # type: ignore[misc]

    def test_no_token_or_channel_fields(self):
        """SlackResponsePayload must not contain delivery concerns."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SlackResponsePayload)}
        forbidden = {"channel_id", "thread_ts", "token", "bot_token"}
        assert not (field_names & forbidden), f"SlackResponsePayload has forbidden fields: {field_names & forbidden}"


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------


class TestToolInputSchemas:
    def test_list_connected_accounts_no_fields(self):
        inp = ListConnectedAccountsInput()
        assert inp is not None

    def test_get_account_stats_valid(self):
        inp = GetAccountStatsInput(platform=SupportedPlatform.INSTAGRAM, days=30)
        assert inp.platform == SupportedPlatform.INSTAGRAM
        assert inp.account_id is None

    def test_get_account_stats_with_account_id(self, valid_uuid):
        inp = GetAccountStatsInput(
            platform=SupportedPlatform.FACEBOOK,
            account_id=valid_uuid,
            days=7,
        )
        assert inp.account_id == valid_uuid

    def test_get_account_stats_days_below_min(self):
        with pytest.raises(ValidationError):
            GetAccountStatsInput(platform=SupportedPlatform.INSTAGRAM, days=1)

    def test_get_account_stats_days_above_max(self):
        with pytest.raises(ValidationError):
            GetAccountStatsInput(platform=SupportedPlatform.INSTAGRAM, days=365)

    def test_get_account_stats_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            GetAccountStatsInput(
                platform=SupportedPlatform.INSTAGRAM,
                days=30,
                workspace_id=uuid.uuid4(),  # type: ignore[call-arg]
            )

    def test_get_top_posts_valid(self):
        inp = GetTopPostsInput(
            platform=SupportedPlatform.INSTAGRAM,
            metric="reach",
            limit=5,
            days=30,
        )
        assert inp.metric == "reach"
        assert inp.limit == 5

    def test_get_top_posts_limit_above_max(self):
        with pytest.raises(ValidationError):
            GetTopPostsInput(
                platform=SupportedPlatform.INSTAGRAM,
                metric="reach",
                limit=50,
            )

    def test_get_top_posts_rejects_workspace_id(self):
        with pytest.raises(ValidationError):
            GetTopPostsInput(
                platform=SupportedPlatform.INSTAGRAM,
                metric="reach",
                workspace_id=uuid.uuid4(),  # type: ignore[call-arg]
            )

    def test_get_post_detail_valid(self, valid_uuid):
        inp = GetPostDetailInput(post_id=valid_uuid)
        assert inp.post_id == valid_uuid

    def test_get_post_detail_rejects_workspace_id(self):
        with pytest.raises(ValidationError):
            GetPostDetailInput(
                post_id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),  # type: ignore[call-arg]
            )

    def test_get_engagement_summary_valid(self):
        inp = GetEngagementSummaryInput(platform=SupportedPlatform.FACEBOOK, days=30)
        assert inp.platform == SupportedPlatform.FACEBOOK

    def test_get_follower_growth_valid(self):
        inp = GetFollowerGrowthInput(platform=SupportedPlatform.LINKEDIN, days=90)
        assert inp.days == 90

    def test_compare_platforms_valid(self):
        inp = ComparePlatformsInput(
            platforms=[SupportedPlatform.INSTAGRAM, SupportedPlatform.FACEBOOK],
            metric="reach",
            days=30,
        )
        assert len(inp.platforms) == 2

    def test_compare_platforms_rejects_user_id(self):
        with pytest.raises(ValidationError):
            ComparePlatformsInput(
                platforms=[SupportedPlatform.INSTAGRAM],
                metric="reach",
                user_id=uuid.uuid4(),  # type: ignore[call-arg]
            )

    def test_unsupported_platform_rejected_in_schema(self):
        with pytest.raises(ValidationError):
            GetAccountStatsInput(platform="tiktok", days=30)  # type: ignore[arg-type]
