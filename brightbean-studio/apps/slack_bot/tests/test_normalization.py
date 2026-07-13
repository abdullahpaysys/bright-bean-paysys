"""Tests for Slack message normalization."""

from __future__ import annotations

import pytest

from apps.slack_bot.contracts import SlackAnalyticsRequest
from apps.slack_bot.exceptions import SlackNormalizationError
from apps.slack_bot.models import SlackInboundEvent
from apps.slack_bot.normalization import (
    clean_slack_text,
    is_meaningful_message,
    normalize_inbound_event,
    remove_bot_mentions,
)


# ===========================================================================
# remove_bot_mentions
# ===========================================================================

def test_remove_mention_basic():
    assert remove_bot_mentions("<@B123> hello") == " hello"


def test_remove_mention_with_display_name():
    assert remove_bot_mentions("<@B123|analyticsbot> help") == " help"


def test_remove_mention_at_end():
    assert remove_bot_mentions("hello <@B123>") == "hello "


def test_remove_mention_multiple():
    assert remove_bot_mentions("<@B123> hello <@U456>") == " hello "


def test_remove_mention_no_mention():
    assert remove_bot_mentions("top facebook post") == "top facebook post"


def test_remove_mention_preserves_at_in_words():
    """Normal words containing @ should not be removed."""
    assert remove_bot_mentions("email@test.com") == "email@test.com"


# ===========================================================================
# clean_slack_text
# ===========================================================================

def test_clean_text_basic_mention():
    assert clean_slack_text("<@B123> hello") == "hello"


def test_clean_text_mention_with_display_name():
    assert clean_slack_text("<@B123|analyticsbot> help") == "help"


def test_clean_text_mention_at_end():
    assert clean_slack_text("hello <@B123>") == "hello"


def test_clean_text_extra_whitespace():
    assert clean_slack_text("<@B123>   top     instagram    post") == "top instagram post"


def test_clean_text_no_mention():
    assert clean_slack_text("top facebook post") == "top facebook post"


def test_clean_text_preserves_punctuation():
    assert clean_slack_text("compare Facebook vs LinkedIn last 30 days?") == "compare Facebook vs LinkedIn last 30 days?"


def test_clean_text_preserves_urls():
    assert clean_slack_text("check https://example.com/report") == "check https://example.com/report"


# ===========================================================================
# is_meaningful_message
# ===========================================================================

def test_meaningful_hello():
    assert is_meaningful_message("hello") is True


def test_meaningful_platform_names():
    assert is_meaningful_message("compare Facebook vs LinkedIn") is True


def test_meaningful_with_numbers():
    assert is_meaningful_message("last 30 days?") is True


def test_not_meaningful_empty():
    assert is_meaningful_message("") is False


def test_not_meaningful_whitespace():
    assert is_meaningful_message("   ") is False


def test_not_meaningful_punctuation_only():
    assert is_meaningful_message("???") is False
    assert is_meaningful_message("!!!") is False
    assert is_meaningful_message(".") is False


# ===========================================================================
# normalize_inbound_event — using SlackInboundEvent model
# ===========================================================================

@pytest.mark.django_db
def test_normalize_basic_app_mention():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_1",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000100",
        message_text="<@B123> hello",
        thread_ts="1720000000.000100",
    )
    result = normalize_inbound_event(event)
    assert isinstance(result, SlackAnalyticsRequest)
    assert result.text == "hello"
    assert result.event_id == "Ev_norm_1"
    assert result.team_id == "T123"
    assert result.channel_id == "C123"
    assert result.user_id == "U123"
    assert result.thread_ts == "1720000000.000100"


@pytest.mark.django_db
def test_normalize_mention_with_display_name():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_2",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000200",
        message_text="<@B123|analyticsbot> help",
        thread_ts="1720000000.000200",
    )
    result = normalize_inbound_event(event)
    assert result.text == "help"


@pytest.mark.django_db
def test_normalize_mention_at_end():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_3",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000300",
        message_text="hello <@B123>",
        thread_ts="1720000000.000300",
    )
    result = normalize_inbound_event(event)
    assert result.text == "hello"


@pytest.mark.django_db
def test_normalize_extra_whitespace():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_4",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000400",
        message_text="<@B123>   top     instagram    post",
        thread_ts="1720000000.000400",
    )
    result = normalize_inbound_event(event)
    assert result.text == "top instagram post"


@pytest.mark.django_db
def test_normalize_no_mention():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_5",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000500",
        message_text="top facebook post",
        thread_ts="1720000000.000500",
    )
    result = normalize_inbound_event(event)
    assert result.text == "top facebook post"


@pytest.mark.django_db
def test_normalize_empty_after_mention_raises():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_6",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000600",
        message_text="<@B123>",
        thread_ts="1720000000.000600",
    )
    with pytest.raises(SlackNormalizationError):
        normalize_inbound_event(event)


@pytest.mark.django_db
def test_normalize_punctuation_only_raises():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_7",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000700",
        message_text="???",
        thread_ts="1720000000.000700",
    )
    with pytest.raises(SlackNormalizationError):
        normalize_inbound_event(event)


@pytest.mark.django_db
def test_normalize_whitespace_only_raises():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_8",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000800",
        message_text="   ",
        thread_ts="1720000000.000800",
    )
    with pytest.raises(SlackNormalizationError):
        normalize_inbound_event(event)


@pytest.mark.django_db
def test_normalize_thread_ts_fallback_to_event_ts():
    """If thread_ts is empty, normalized thread_ts should equal event_ts."""
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_9",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000900",
        message_text="<@B123> hello",
        thread_ts="",  # empty
    )
    result = normalize_inbound_event(event)
    assert result.thread_ts == "1720000000.000900"


@pytest.mark.django_db
def test_normalize_explicit_thread_ts_preserved():
    """If thread_ts exists, preserve it."""
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_10",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000001.000100",
        message_text="<@B123> hello",
        thread_ts="1720000000.000100",
    )
    result = normalize_inbound_event(event)
    assert result.thread_ts == "1720000000.000100"


@pytest.mark.django_db
def test_normalize_preserves_platform_names():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_11",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.001100",
        message_text="compare Facebook vs LinkedIn last 30 days",
        thread_ts="1720000000.001100",
    )
    result = normalize_inbound_event(event)
    assert result.text == "compare Facebook vs LinkedIn last 30 days"


@pytest.mark.django_db
def test_normalize_preserves_numbers():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_12",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.001200",
        message_text="top 5 instagram posts in last 7 days",
        thread_ts="1720000000.001200",
    )
    result = normalize_inbound_event(event)
    assert result.text == "top 5 instagram posts in last 7 days"


@pytest.mark.django_db
def test_normalize_correlation_id_passed_through():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_13",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.001300",
        message_text="<@B123> show analytics",
        thread_ts="1720000000.001300",
        correlation_id="corr-abc-123",
    )
    result = normalize_inbound_event(event)
    assert result.correlation_id == "corr-abc-123"


@pytest.mark.django_db
def test_normalize_dataclass_is_frozen():
    event = SlackInboundEvent.objects.create(
        event_id="Ev_norm_14",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.001400",
        message_text="<@B123> hello",
        thread_ts="1720000000.001400",
    )
    result = normalize_inbound_event(event)
    with pytest.raises(AttributeError):
        result.text = "mutated"
