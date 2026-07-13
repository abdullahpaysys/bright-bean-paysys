"""Tests for event deduplication via the manager helper."""

import pytest

from apps.slack_bot.constants import STATUS_RECEIVED
from apps.slack_bot.models import SlackInboundEvent


@pytest.mark.django_db
def test_first_call_creates_event():
    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvDEDUP1",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001000",
        message_text="First message",
    )
    assert created is True
    assert event.event_id == "EvDEDUP1"
    assert event.status == STATUS_RECEIVED
    assert event.message_text == "First message"


@pytest.mark.django_db
def test_second_call_returns_existing_no_duplicate():
    SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvDEDUP2",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001100",
        message_text="Original",
    )

    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvDEDUP2",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001100",
        message_text="Duplicate attempt",
    )
    assert created is False
    assert event.message_text == "Original"

    total = SlackInboundEvent.objects.filter(event_id="EvDEDUP2").count()
    assert total == 1


@pytest.mark.django_db
def test_thread_ts_passed_through():
    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvTHREAD",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001200",
        message_text="Threaded reply",
        thread_ts="1719999999.000900",
    )
    assert created is True
    assert event.thread_ts == "1719999999.000900"


@pytest.mark.django_db
def test_none_thread_ts_becomes_blank():
    event, created = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvNOTHREAD",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001300",
        message_text="Top-level message",
        thread_ts=None,
    )
    assert created is True
    assert event.thread_ts == ""


@pytest.mark.django_db
def test_concurrent_same_event_id_no_duplicate():
    """Simulate two rapid calls with the same event_id."""
    e1, c1 = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvCONCURRENT",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001400",
        message_text="First",
    )
    e2, c2 = SlackInboundEvent.objects.get_or_create_inbound_event(
        event_id="EvCONCURRENT",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.001400",
        message_text="Second",
    )
    assert c1 is True
    assert c2 is False
    assert e1.pk == e2.pk
    assert SlackInboundEvent.objects.count() == 1
