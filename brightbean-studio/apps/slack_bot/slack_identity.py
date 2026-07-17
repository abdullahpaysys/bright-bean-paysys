"""Slack user identity resolution for access provisioning.

The provisioning layer uses this module as the only source of Slack profile
identity.  Tests can inject a fake client so no real Slack API is contacted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

import httpx
from django.conf import settings

from .slack_id_validation import is_valid_member_id

SLACK_USERS_INFO_URL = "https://slack.com/api/users.info"


class SlackIdentityErrorCode:
    INVALID_SLACK_USER_ID = "INVALID_SLACK_USER_ID"
    SLACK_USER_NOT_FOUND = "SLACK_USER_NOT_FOUND"
    SLACK_PROFILE_UNAVAILABLE = "SLACK_PROFILE_UNAVAILABLE"


class SlackIdentityError(Exception):
    """Controlled Slack identity lookup failure."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SlackUserIdentity:
    slack_user_id: str
    team_id: str
    email: str
    display_name: str = ""
    real_name: str = ""
    is_bot: bool = False
    is_deleted: bool = False
    is_guest: bool = False


class SlackIdentityClient(Protocol):
    def get_user(self, *, slack_user_id: str) -> SlackUserIdentity:
        ...


def get_slack_bot_token() -> str:
    return (
        getattr(settings, "SLACK_BOT_TOKEN", "")
        or os.environ.get("SLACK_BOT_TOKEN", "")
    ).strip()


class SlackWebIdentityClient:
    """Slack Web API implementation using ``users.info``."""

    def __init__(self, *, token: str | None = None, timeout: float = 10.0) -> None:
        self.token = token if token is not None else get_slack_bot_token()
        self.timeout = timeout

    def get_user(self, *, slack_user_id: str) -> SlackUserIdentity:
        slack_user_id = (slack_user_id or "").strip()
        if not is_valid_member_id(slack_user_id):
            raise SlackIdentityError(
                SlackIdentityErrorCode.INVALID_SLACK_USER_ID,
                "Invalid Slack member ID.",
            )
        if not self.token:
            raise SlackIdentityError(
                SlackIdentityErrorCode.SLACK_PROFILE_UNAVAILABLE,
                "Slack bot token is not configured.",
            )

        try:
            response = httpx.get(
                SLACK_USERS_INFO_URL,
                params={"user": slack_user_id},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise SlackIdentityError(
                SlackIdentityErrorCode.SLACK_PROFILE_UNAVAILABLE,
                "Slack profile lookup failed.",
            ) from exc

        if not isinstance(payload, dict) or not payload.get("ok"):
            error = str(payload.get("error", "")) if isinstance(payload, dict) else ""
            code = (
                SlackIdentityErrorCode.SLACK_USER_NOT_FOUND
                if error in {"user_not_found", "users_not_found"}
                else SlackIdentityErrorCode.SLACK_PROFILE_UNAVAILABLE
            )
            raise SlackIdentityError(code, "Slack user profile is unavailable.")

        user = payload.get("user") or {}
        if not isinstance(user, dict):
            raise SlackIdentityError(
                SlackIdentityErrorCode.SLACK_PROFILE_UNAVAILABLE,
                "Slack user profile is malformed.",
            )

        profile = user.get("profile") or {}
        if not isinstance(profile, dict):
            profile = {}

        return SlackUserIdentity(
            slack_user_id=str(user.get("id") or slack_user_id),
            team_id=str(user.get("team_id") or payload.get("team_id") or ""),
            email=str(profile.get("email") or "").strip(),
            display_name=str(profile.get("display_name") or user.get("name") or ""),
            real_name=str(profile.get("real_name") or user.get("real_name") or ""),
            is_bot=bool(user.get("is_bot")),
            is_deleted=bool(user.get("deleted") or user.get("is_deleted")),
            is_guest=bool(
                user.get("is_restricted")
                or user.get("is_ultra_restricted")
                or user.get("is_stranger")
            ),
        )
