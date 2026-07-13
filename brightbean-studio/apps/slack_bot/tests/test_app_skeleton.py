"""Phase 2/3 test — verifies the Slack events endpoint is wired up."""

from django.test import Client
from django.urls import reverse


def test_slack_events_endpoint_rejects_missing_signature():
    """POST /slack/events/ without signature headers returns 401."""
    client = Client()
    url = reverse("slack_bot:events")
    response = client.post(url, data=b"{}", content_type="application/json")
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_signature"


def test_slack_events_url_resolves():
    """The URL name slack_bot:events should resolve without error."""
    url = reverse("slack_bot:events")
    assert url == "/slack/events/"
