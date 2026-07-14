"""Phase 9 — Mocked end-to-end tests.

These tests exercise the full pipeline from the HTTP endpoint through
persistence and enqueue, and from ``process_inbound_event`` through
normalization, routing, and delivery — all without a real Slack
installation or network calls.

``unittest.mock.patch`` is used to intercept ``enqueue_inbound_event``
so we can assert it is called exactly once for new events and never
for duplicates / ignored / invalid requests.
"""

from __future__ import annotations

import json
import logging

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from unittest.mock import patch

from apps.slack_bot.constants import (
    STATUS_IGNORED,
    STATUS_RESPONDED,
)
from apps.slack_bot.models import SlackInboundEvent
from apps.slack_bot.tasks import (
    RESULT_IGNORED,
    RESULT_FAILED,
    process_inbound_event,
)
from apps.slack_bot.tests.conftest import signed_slack_headers

SECRET = "test_secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(client, body_dict, secret=SECRET, timestamp=None):
    """Post a JSON body with valid Slack signature headers."""
    raw = json.dumps(body_dict).encode("utf-8")
    headers = signed_slack_headers(raw, secret=secret, timestamp=timestamp)
    url = reverse("slack_bot:events")
    return client.post(url, data=raw, content_type="application/json", **headers)


def _post_raw(client, raw: bytes, secret=SECRET, timestamp=None):
    """Post raw bytes with valid Slack signature headers."""
    headers = signed_slack_headers(raw, secret=secret, timestamp=timestamp)
    url = reverse("slack_bot:events")
    return client.post(url, data=raw, content_type="application/json", **headers)


def _post_no_sig(client, body_dict):
    """Post JSON without signature headers."""
    raw = json.dumps(body_dict).encode("utf-8")
    url = reverse("slack_bot:events")
    return client.post(url, data=raw, content_type="application/json")


def _mention_payload(event_id="Ev_e2e_1", text="<@B123> hello"):
    return {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": "U123",
            "text": text,
            "ts": "1720000000.000100",
            "channel": "C123",
            "event_ts": "1720000000.000100",
        },
    }


def _threaded_message_payload(event_id="Ev_e2e_msg", text="hello"):
    return {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": "U123",
            "text": text,
            "ts": "1720000001.000100",
            "thread_ts": "1720000000.000100",
            "channel": "C123",
        },
    }


def _bot_message_payload(event_id="Ev_e2e_bot"):
    return {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "bot_id": "B999",
            "text": "I am a bot",
            "ts": "1720000002.000100",
            "thread_ts": "1720000000.000100",
            "channel": "C123",
        },
    }


def _no_thread_payload(event_id="Ev_e2e_nothread"):
    return {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": "U123",
            "text": "standalone",
            "ts": "1720000003.000100",
            "channel": "C123",
        },
    }


def _url_verify_payload():
    return {"type": "url_verification", "challenge": "challenge_xyz"}


def _create_event(event_id="Ev_proc_1", message_text="<@B123> hello",
                  thread_ts="1720000000.000100"):
    return SlackInboundEvent.objects.create(
        event_id=event_id,
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        event_ts="1720000000.000100",
        message_text=message_text,
        thread_ts=thread_ts,
    )


def _fake_delivery(response_ts="1720000001.000200"):
    """Return a fake delivery callback that records its calls."""
    calls = []

    def _deliver(**kwargs):
        calls.append(kwargs)
        return response_ts

    _deliver.calls = calls
    return _deliver


# ===========================================================================
# A. Endpoint → enqueue wiring
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_valid_mention_enqueues_once():
    """A valid signed app_mention is persisted and enqueued exactly once."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    mock_enqueue.assert_called_once()
    assert mock_enqueue.call_args[0][0] == "Ev_e2e_1"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_duplicate_does_not_enqueue():
    """A duplicate event_id must not trigger a second enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        _post(client, _mention_payload())
        assert mock_enqueue.call_count == 1
        response = _post(client, _mention_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert mock_enqueue.call_count == 1


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_bot_message_does_not_enqueue():
    """Bot messages are ignored before persistence — no enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post(client, _bot_message_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_message_without_thread_does_not_enqueue():
    """Messages without thread_ts are ignored — no enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post(client, _no_thread_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_url_verification_does_not_enqueue():
    """URL verification handshake is handled without enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post(client, _url_verify_payload())
    assert response.status_code == 200
    assert response.json()["challenge"] == "challenge_xyz"
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_invalid_signature_does_not_enqueue():
    """Invalid signature → 401, no enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post_no_sig(client, _mention_payload())
    assert response.status_code == 401
    mock_enqueue.assert_not_called()


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_invalid_json_does_not_enqueue():
    """Invalid JSON body → 400, no enqueue."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event") as mock_enqueue:
        response = _post_raw(client, b"not valid json {{{")
    assert response.status_code == 400
    mock_enqueue.assert_not_called()


# ===========================================================================
# B. Full processing pipeline (process_inbound_event with fake delivery)
# ===========================================================================

@pytest.mark.django_db
def test_e2e_greeting_full_pipeline():
    """Greeting message → normalize → route → no_response → IGNORED."""
    event = _create_event(event_id="Ev_e2e_greet", message_text="<@B123> hello")
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED
    assert len(delivery.calls) == 0


@pytest.mark.django_db
def test_e2e_help_full_pipeline():
    """Help command → normalize → route → no_response → IGNORED."""
    event = _create_event(event_id="Ev_e2e_help", message_text="<@B123> help")
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


@pytest.mark.django_db
def test_e2e_status_full_pipeline():
    """Status command → normalize → route → no_response → IGNORED."""
    event = _create_event(event_id="Ev_e2e_status", message_text="<@B123> status")
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


@pytest.mark.django_db
def test_e2e_analytics_query_full_pipeline():
    """Analytics query → normalize → route → no_response → IGNORED."""
    event = _create_event(
        event_id="Ev_e2e_analytics",
        message_text="<@B123> analytics for facebook",
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    assert result.response_type == "no_response"
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


@pytest.mark.django_db
def test_e2e_delivery_not_called_for_no_response():
    """All routes return no_response, so delivery is never called."""
    event = _create_event(event_id="Ev_e2e_fail", message_text="<@B123> hello")

    def _bad_delivery(**kwargs):
        from apps.slack_bot.exceptions import SlackDeliveryError
        raise SlackDeliveryError("Slack API error")

    result = process_inbound_event(event.event_id, deliver_response=_bad_delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED


@pytest.mark.django_db
def test_e2e_ignored_message_no_delivery():
    """A message that normalization rejects → IGNORED, no delivery call."""
    event = _create_event(
        event_id="Ev_e2e_ignored",
        message_text="<@B123> ",  # only bot mention, no meaningful text
    )
    delivery = _fake_delivery()

    result = process_inbound_event(event.event_id, deliver_response=delivery)

    assert result.ok is True
    assert result.status == RESULT_IGNORED
    event.refresh_from_db()
    assert event.status == STATUS_IGNORED
    assert len(delivery.calls) == 0


# ===========================================================================
# C. Logging tests — event_id present, no secrets
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_log_contains_event_id_on_receive(caplog):
    """When a new event is received, the log must contain the event_id."""
    client = Client()
    caplog.set_level(logging.INFO, logger="apps.slack_bot.views")
    with patch("apps.slack_bot.views.enqueue_inbound_event"):
        _post(client, _mention_payload(event_id="Ev_log_1"))
    assert any("Ev_log_1" in r.message for r in caplog.records)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_log_contains_event_id_on_duplicate(caplog):
    """Duplicate event logs must contain the event_id."""
    client = Client()
    with patch("apps.slack_bot.views.enqueue_inbound_event"):
        _post(client, _mention_payload(event_id="Ev_log_dup"))
    caplog.clear()
    caplog.set_level(logging.INFO, logger="apps.slack_bot.views")
    with patch("apps.slack_bot.views.enqueue_inbound_event"):
        _post(client, _mention_payload(event_id="Ev_log_dup"))
    assert any("Ev_log_dup" in r.message for r in caplog.records)


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_e2e_log_no_secrets_on_receive(caplog):
    """Logs must never contain the signing secret or bot token."""
    client = Client()
    caplog.set_level(logging.DEBUG)
    with patch("apps.slack_bot.views.enqueue_inbound_event"):
        _post(client, _mention_payload())
    for record in caplog.records:
        assert SECRET not in record.message
        assert "xoxb-" not in record.message


@pytest.mark.django_db
def test_e2e_log_processing_contains_event_id(caplog):
    """process_inbound_event logs must contain the event_id."""
    event = _create_event(event_id="Ev_log_proc", message_text="<@B123> hello")
    delivery = _fake_delivery()
    caplog.set_level(logging.INFO, logger="apps.slack_bot.tasks")
    process_inbound_event(event.event_id, deliver_response=delivery)
    assert any("Ev_log_proc" in r.message for r in caplog.records)


@pytest.mark.django_db
def test_e2e_log_delivery_failure_contains_event_id(caplog):
    """Processing logs must contain the event_id even for no_response."""
    event = _create_event(event_id="Ev_log_fail", message_text="<@B123> hello")
    caplog.set_level(logging.INFO, logger="apps.slack_bot.tasks")

    def _bad_delivery(**kwargs):
        from apps.slack_bot.exceptions import SlackDeliveryError
        raise SlackDeliveryError("Slack API error")

    process_inbound_event(event.event_id, deliver_response=_bad_delivery)
    assert any("Ev_log_fail" in r.message for r in caplog.records)


@pytest.mark.django_db
def test_e2e_log_no_secrets_in_tasks(caplog):
    """Task processing logs must never contain secrets."""
    event = _create_event(event_id="Ev_log_nosec", message_text="<@B123> hello")
    delivery = _fake_delivery()
    caplog.set_level(logging.DEBUG)
    process_inbound_event(event.event_id, deliver_response=delivery)
    for record in caplog.records:
        assert "test_secret" not in record.message
        assert "xoxb-" not in record.message
