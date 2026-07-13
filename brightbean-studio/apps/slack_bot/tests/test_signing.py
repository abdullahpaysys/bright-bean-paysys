"""Tests for Slack request signature verification."""

from __future__ import annotations

import time

from django.test import override_settings

from apps.slack_bot.signing import (
    build_slack_signature,
    verify_slack_request,
)

SECRET = "test_secret"


def _make_body() -> bytes:
    return b'{"type":"event_callback","event_id":"Ev1"}'


def _valid_timestamp() -> str:
    return str(int(time.time()))


# --- 1. Valid signature passes ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_valid_signature_passes():
    body = _make_body()
    ts = _valid_timestamp()
    sig = build_slack_signature(SECRET, ts, body)
    assert verify_slack_request(body, ts, sig, signing_secret=SECRET) is True


# --- 2. Invalid signature fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_invalid_signature_fails():
    body = _make_body()
    ts = _valid_timestamp()
    assert verify_slack_request(body, ts, "v0=deadbeef", signing_secret=SECRET) is False


# --- 3. Missing signature fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_missing_signature_fails():
    body = _make_body()
    ts = _valid_timestamp()
    assert verify_slack_request(body, ts, None, signing_secret=SECRET) is False
    assert verify_slack_request(body, ts, "", signing_secret=SECRET) is False


# --- 4. Missing timestamp fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_missing_timestamp_fails():
    body = _make_body()
    sig = build_slack_signature(SECRET, "1700000000", body)
    assert verify_slack_request(body, None, sig, signing_secret=SECRET) is False
    assert verify_slack_request(body, "", sig, signing_secret=SECRET) is False


# --- 5. Stale timestamp fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_stale_timestamp_fails():
    body = _make_body()
    now = int(time.time())
    stale_ts = str(now - 600)  # 600 seconds ago, > 300 window
    sig = build_slack_signature(SECRET, stale_ts, body)
    assert verify_slack_request(
        body, stale_ts, sig, signing_secret=SECRET, now=now
    ) is False


# --- 6. Future timestamp outside replay window fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_future_timestamp_fails():
    body = _make_body()
    now = int(time.time())
    future_ts = str(now + 600)  # 600 seconds in future
    sig = build_slack_signature(SECRET, future_ts, body)
    assert verify_slack_request(
        body, future_ts, sig, signing_secret=SECRET, now=now
    ) is False


# --- 7. Signature uses raw body exactly ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_signature_uses_raw_body():
    body_a = b'{"event_id":"Ev1"}'
    body_b = b'{"event_id":"Ev2"}'
    ts = _valid_timestamp()
    sig_for_a = build_slack_signature(SECRET, ts, body_a)
    # sig_for_a should NOT verify body_b
    assert verify_slack_request(
        body_b, ts, sig_for_a, signing_secret=SECRET
    ) is False
    # sig_for_a should verify body_a
    assert verify_slack_request(
        body_a, ts, sig_for_a, signing_secret=SECRET
    ) is True


# --- 8. Bad signature format (no v0= prefix) fails ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_bad_signature_format_fails():
    body = _make_body()
    ts = _valid_timestamp()
    assert verify_slack_request(
        body, ts, "deadbeef", signing_secret=SECRET
    ) is False


# --- 9. Custom replay window respected ---

@override_settings(SLACK_SIGNING_SECRET=SECRET)
def test_custom_replay_window():
    body = _make_body()
    now = int(time.time())
    ts = str(now - 100)  # 100 seconds ago
    sig = build_slack_signature(SECRET, ts, body)
    # Within 200-second window
    assert verify_slack_request(
        body, ts, sig, signing_secret=SECRET, replay_window_seconds=200, now=now
    ) is True
    # Outside 50-second window
    assert verify_slack_request(
        body, ts, sig, signing_secret=SECRET, replay_window_seconds=50, now=now
    ) is False


# --- 10. Empty secret fails ---

def test_empty_secret_fails():
    body = _make_body()
    ts = _valid_timestamp()
    sig = build_slack_signature(SECRET, ts, body)
    assert verify_slack_request(body, ts, sig, signing_secret="") is False
