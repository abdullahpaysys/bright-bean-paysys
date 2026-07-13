"""Tests for Slack event parsing, filtering, and the /slack/events/ endpoint."""

from __future__ import annotations

import json
import time

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.slack_bot.models import SlackInboundEvent
from apps.slack_bot.tests.conftest import signed_slack_headers

SECRET = "test_secret"


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


# ===========================================================================
# URL Verification
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_url_verification_returns_challenge():
    client = Client()
    response = _post(client, {"type": "url_verification", "challenge": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}


# ===========================================================================
# app_mention — accepted and persisted
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_app_mention_accepted_and_persisted():
    client = Client()
    payload = {
        "token": "verification-token",
        "team_id": "T123",
        "api_app_id": "A123",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "text": "<@B123> hello",
            "ts": "1720000000.000100",
            "channel": "C123",
            "event_ts": "1720000000.000100",
        },
        "type": "event_callback",
        "event_id": "Ev_mention_1",
        "event_time": 1720000000,
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "received"

    assert SlackInboundEvent.objects.filter(event_id="Ev_mention_1").count() == 1
    event = SlackInboundEvent.objects.get(event_id="Ev_mention_1")
    assert event.team_id == "T123"
    assert event.channel_id == "C123"
    assert event.user_id == "U123"
    assert event.message_text == "<@B123> hello"


# ===========================================================================
# Duplicate event
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_duplicate_event_returns_duplicate():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_dup_1",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "text": "hello",
            "ts": "1720000000.000100",
            "channel": "C123",
        },
    }
    # First request
    r1 = _post(client, payload)
    assert r1.json()["status"] == "received"

    # Second request with same event_id
    r2 = _post(client, payload)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"

    assert SlackInboundEvent.objects.filter(event_id="Ev_dup_1").count() == 1


# ===========================================================================
# Bot message — ignored
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_bot_message_ignored():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_bot_1",
        "event": {
            "type": "message",
            "bot_id": "B123",
            "text": "I am a bot",
            "ts": "1720000000.000200",
            "channel": "C123",
            "thread_ts": "1720000000.000100",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "bot_message"
    assert SlackInboundEvent.objects.filter(event_id="Ev_bot_1").count() == 0


# ===========================================================================
# Message with subtype — ignored
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_message_with_subtype_ignored():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_sub_1",
        "event": {
            "type": "message",
            "user": "U123",
            "text": "edited",
            "ts": "1720000000.000300",
            "channel": "C123",
            "thread_ts": "1720000000.000100",
            "subtype": "message_changed",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "ignored_subtype"


# ===========================================================================
# Message with thread_ts — accepted
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_message_with_thread_ts_accepted():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_thread_1",
        "event": {
            "type": "message",
            "user": "U123",
            "text": "what about Facebook?",
            "ts": "1720000001.000100",
            "thread_ts": "1720000000.000100",
            "channel": "C123",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "received"
    event = SlackInboundEvent.objects.get(event_id="Ev_thread_1")
    assert event.thread_ts == "1720000000.000100"
    assert event.message_text == "what about Facebook?"


# ===========================================================================
# Message without thread_ts — ignored
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_message_without_thread_ts_ignored():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_nothread_1",
        "event": {
            "type": "message",
            "user": "U123",
            "text": "standalone message",
            "ts": "1720000002.000100",
            "channel": "C123",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "message_without_thread"


# ===========================================================================
# Unsupported event type — ignored
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_unsupported_event_type_ignored():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event_id": "Ev_unsup_1",
        "event": {
            "type": "reaction_added",
            "user": "U123",
            "ts": "1720000003.000100",
            "channel": "C123",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unsupported_type"


# ===========================================================================
# Missing event_id — ignored
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_missing_event_id_ignored():
    client = Client()
    payload = {
        "team_id": "T123",
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "text": "no event id",
            "ts": "1720000004.000100",
            "channel": "C123",
        },
    }
    response = _post(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "missing_event_id"


# ===========================================================================
# Invalid JSON — HTTP 400
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_invalid_json_returns_400():
    client = Client()
    raw = b"not valid json {{{"
    response = _post_raw(client, raw)
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_json"


# ===========================================================================
# Invalid signature — HTTP 401
# ===========================================================================

@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_invalid_signature_returns_401():
    client = Client()
    response = _post_no_sig(client, {"type": "event_callback", "event_id": "Ev1"})
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_signature"


@pytest.mark.django_db
@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_wrong_signature_returns_401():
    client = Client()
    raw = json.dumps({"type": "event_callback", "event_id": "Ev1"}).encode()
    url = reverse("slack_bot:events")
    response = client.post(
        url,
        data=raw,
        content_type="application/json",
        HTTP_X_SLACK_REQUEST_TIMESTAMP=str(int(time.time())),
        HTTP_X_SLACK_SIGNATURE="v0=wrongsignature",
    )
    assert response.status_code == 401


# ===========================================================================
# Unit tests for events.py helpers
# ===========================================================================

def test_is_url_verification_true():
    from apps.slack_bot.events import is_url_verification
    assert is_url_verification({"type": "url_verification", "challenge": "x"}) is True


def test_is_url_verification_false():
    from apps.slack_bot.events import is_url_verification
    assert is_url_verification({"type": "event_callback"}) is False


def test_get_url_verification_challenge():
    from apps.slack_bot.events import get_url_verification_challenge
    assert get_url_verification_challenge({"challenge": "abc"}) == "abc"


def test_get_url_verification_challenge_missing():
    from apps.slack_bot.events import get_url_verification_challenge
    from apps.slack_bot.exceptions import SlackEventParseError
    with pytest.raises(SlackEventParseError):
        get_url_verification_challenge({})


def test_parse_slack_payload_valid():
    from apps.slack_bot.events import parse_slack_payload
    result = parse_slack_payload(b'{"type":"event_callback"}')
    assert result == {"type": "event_callback"}


def test_parse_slack_payload_invalid():
    from apps.slack_bot.events import parse_slack_payload
    from apps.slack_bot.exceptions import SlackEventParseError
    with pytest.raises(SlackEventParseError):
        parse_slack_payload(b"not json")
