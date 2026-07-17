"""PostgreSQL-only test settings for Phase 1C concurrency validation.

This module is intentionally strict: it is for disposable test databases only
and must not accept production database configuration by accident.
"""

from __future__ import annotations

import os

if os.environ.get("DATABASE_URL"):
    raise RuntimeError("DATABASE_URL must not be set for Phase 1C PostgreSQL tests")

os.environ["DATABASE_URL"] = "postgres://brightbean_test:unused@127.0.0.1:5432/brightbean_phase1c_test"
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("ZAI_API_KEY", "test-zai-key")

from .test import *  # noqa: F401, F403

os.environ.pop("DATABASE_URL", None)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for PostgreSQL test settings")
    return value


_db_host = os.environ.get("PHASE1C_POSTGRES_HOST", "127.0.0.1").strip()
_db_port = os.environ.get("PHASE1C_POSTGRES_PORT", "5432").strip()
_db_name = _required_env("PHASE1C_POSTGRES_DB")
_db_user = _required_env("PHASE1C_POSTGRES_USER")
_db_password = _required_env("PHASE1C_POSTGRES_PASSWORD")

if "test" not in _db_name.lower():
    raise RuntimeError("PostgreSQL test database name must contain 'test'")

if _db_host.lower() not in {"127.0.0.1", "localhost", "postgres"}:
    raise RuntimeError("PostgreSQL test host must be local or the CI postgres service")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _db_host,
        "PORT": _db_port,
        "NAME": _db_name,
        "USER": _db_user,
        "PASSWORD": _db_password,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "connect_timeout": 5,
        },
    },
}

# Keep all integrations deterministic and fake in CI.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "xoxb-test-token")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "test-signing-secret")
SLACK_ALLOWED_TEAM_ID = os.environ.get("SLACK_ALLOWED_TEAM_ID", "TTEAM01")
SLACK_ALLOWED_EMAIL_DOMAINS = os.environ.get("SLACK_ALLOWED_EMAIL_DOMAINS", "example.com")

ADMIN_LLM_CHAT_ENABLED = False
ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "test-zai-key")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "test-anthropic-key")
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
