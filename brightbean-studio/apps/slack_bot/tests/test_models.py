"""Tests for SlackInboundEvent model creation and field defaults."""

import pytest
from django.db import IntegrityError

from apps.slack_bot.constants import (
    STATUS_PROCESSING,
    STATUS_RECEIVED,
    STATUS_RESPONDED,
)
from apps.slack_bot.models import SlackInboundEvent


@pytest.mark.django_db
def test_model_can_be_created_with_required_fields():
    event = SlackInboundEvent.objects.create(
        event_id="Ev0001",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000100",
        message_text="Hello from Slack",
    )
    assert event.pk is not None
    assert event.event_id == "Ev0001"
    assert event.team_id == "T0001"
    assert event.channel_id == "C0001"
    assert event.user_id == "U0001"
    assert event.event_ts == "1720000000.000100"
    assert event.message_text == "Hello from Slack"


@pytest.mark.django_db
def test_default_status_is_received():
    event = SlackInboundEvent.objects.create(
        event_id="Ev0002",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000200",
    )
    assert event.status == STATUS_RECEIVED


@pytest.mark.django_db
def test_unique_event_id_blocks_duplicates():
    SlackInboundEvent.objects.create(
        event_id="EvDUP",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000300",
    )
    with pytest.raises(IntegrityError):
        SlackInboundEvent.objects.create(
            event_id="EvDUP",
            team_id="T0001",
            channel_id="C0001",
            user_id="U0001",
            event_ts="1720000000.000400",
        )


@pytest.mark.django_db
def test_status_transitions_and_response_ts():
    event = SlackInboundEvent.objects.create(
        event_id="EvTRANS",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000500",
    )
    assert event.status == STATUS_RECEIVED

    event.status = STATUS_PROCESSING
    event.save()
    event.refresh_from_db()
    assert event.status == STATUS_PROCESSING

    event.status = STATUS_RESPONDED
    event.response_ts = "1720000001.000600"
    event.save()
    event.refresh_from_db()
    assert event.status == STATUS_RESPONDED
    assert event.response_ts == "1720000001.000600"


@pytest.mark.django_db
def test_str_representation():
    event = SlackInboundEvent.objects.create(
        event_id="EvSTR",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000700",
    )
    assert str(event) == "EvSTR [RECEIVED]"


@pytest.mark.django_db
def test_optional_fields_default_to_blank():
    event = SlackInboundEvent.objects.create(
        event_id="EvOPT",
        team_id="T0001",
        channel_id="C0001",
        user_id="U0001",
        event_ts="1720000000.000800",
    )
    assert event.thread_ts == ""
    assert event.message_text == ""
    assert event.response_ts == ""
    assert event.correlation_id == ""
