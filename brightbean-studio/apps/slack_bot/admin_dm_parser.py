"""Deterministic parser for administrator DM access-grant commands.

No LLM is used — parsing is purely syntactic.  The parser recognises
common grant-related English phrases and extracts Slack Member IDs
from arbitrary text.

Legacy email pairing may still be parsed for compatibility, but the active
provisioning flow never trusts email text from the admin message.

Example::

    result = parse_grant_command("Give U08ABC123 access")
    if result.is_grant_intent:
        entries = result.entries       # [("U08ABC123", "user@company.com")]
        member_ids = result.member_ids # ["U08ABC123"]
        invalid_ids = result.invalid_ids # []

Bulk format with mixed email/no-email::

    Give access to:
    U08ABC123
    U08DEF456
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .slack_id_validation import (
    deduplicate_ids,
    is_valid_member_id,
)

# ---------------------------------------------------------------------------
# Grant-intent keywords (lowercased, checked as whole-word substrings)
# ---------------------------------------------------------------------------

_GRANT_TERMS: tuple[str, ...] = (
    "give access",
    "grant access",
    "add access",
    "allow",
    "whitelist",
    "approve",
    "access do",
    "ijazat do",
    "allow karo",
    "approve karo",
    "give",
    "grant",
    "add",
)

_REVOKE_TERMS: tuple[str, ...] = (
    "revoke access",
    "remove access",
    "delete access",
    "block access",
    "deny access",
    "disable access",
    "revoke",
    "remove",
    "block",
    "deny",
    "access hatao",
    "hata do",
    "rok do",
    "band karo",
)

# Build a single regex that matches any grant term as a word boundary phrase.
_GRANT_RE = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in _GRANT_TERMS) + r")\b",
    re.IGNORECASE,
)

_REVOKE_RE = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in _REVOKE_TERMS) + r")\b",
    re.IGNORECASE,
)

# Slack-like ID tokens — common Slack prefixes (T, U, W, C, G, D, B)
# followed by at least 2 uppercase alphanumeric characters.
_ID_RE = re.compile(r"\b[TUCGWDB][A-Z0-9]{2,}\b")

# Email regex — simple but sufficient for admin DM parsing.
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
)

# Pattern: <Member ID> [access] as <email>  (case-insensitive "as")
# Handles both "U08ABC123 as user@company.com" and
# "U08ABC123 access as user@company.com"
_ID_EMAIL_RE = re.compile(
    r"\b([UW][A-Z0-9]{2,})\s+(?:access\s+)?as\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantCommandEntry:
    """A single parsed grant entry: Member ID + optional email."""

    member_id: str
    email: str | None = None


@dataclass(frozen=True)
class GrantCommandResult:
    """Outcome of parsing an admin DM for a grant intent.

    Attributes:
        is_grant_intent: True if the message contains a recognised grant term.
        member_ids: Valid, deduplicated Slack Member IDs in order of appearance.
        invalid_ids: Invalid IDs found in the text (e.g. C..., G...).
        entries: List of (member_id, email|None) pairs, deduplicated.
        email_conflicts: Member IDs that appeared with conflicting emails.
    """

    is_grant_intent: bool
    member_ids: list[str] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)
    entries: list[GrantCommandEntry] = field(default_factory=list)
    email_conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdminAccessCommandResult:
    """Parsed admin access command.

    ``intent`` is ``"grant"``, ``"revoke"``, or ``""``.
    """

    intent: str
    member_ids: list[str] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)
    entries: list[GrantCommandEntry] = field(default_factory=list)
    email_conflicts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_grant_command(text: str) -> GrantCommandResult:
    """Parse *text* for an access-grant command.

    Returns a :class:`GrantCommandResult`.

    The parser:
    1. Checks whether *text* contains a recognised grant term.
    2. Extracts Member ID + email pairs (``ID as email``).
    3. Extracts standalone Member IDs (without email).
    4. Separates valid Member IDs from invalid ones.
    5. Deduplicates valid IDs while preserving order.
    6. Detects conflicting emails for the same Member ID.
    """
    if not text or not text.strip():
        return GrantCommandResult(is_grant_intent=False)

    is_grant = bool(_GRANT_RE.search(text))

    # --- Extract ID + email pairs first ---
    paired: dict[str, str] = {}  # member_id → email
    paired_ids: set[str] = set()
    for match in _ID_EMAIL_RE.finditer(text):
        mid = match.group(1)
        email = match.group(2).strip()
        if mid not in paired:
            paired[mid] = email
            paired_ids.add(mid)
        elif paired[mid].lower() != email.lower():
            # Conflict — different emails for same ID
            # Will be reported in email_conflicts
            pass

    # --- Extract all candidate ID tokens ---
    raw_ids = _ID_RE.findall(text)

    valid: list[str] = []
    invalid: list[str] = []
    for raw_id in raw_ids:
        if is_valid_member_id(raw_id):
            valid.append(raw_id)
        else:
            invalid.append(raw_id)

    # Deduplicate valid IDs while preserving order.
    valid = deduplicate_ids(valid)
    invalid = deduplicate_ids(invalid)

    # --- Build entries list (member_id, email|None) ---
    # Track emails per ID for conflict detection
    emails_by_id: dict[str, set[str]] = {}
    for match in _ID_EMAIL_RE.finditer(text):
        mid = match.group(1)
        email = match.group(2).strip().lower()
        if mid not in emails_by_id:
            emails_by_id[mid] = set()
        emails_by_id[mid].add(email)

    email_conflicts: list[str] = []
    for mid, emails in emails_by_id.items():
        if len(emails) > 1:
            email_conflicts.append(mid)

    entries: list[GrantCommandEntry] = []
    for mid in valid:
        email = paired.get(mid)
        entries.append(GrantCommandEntry(member_id=mid, email=email))

    return GrantCommandResult(
        is_grant_intent=is_grant,
        member_ids=valid,
        invalid_ids=invalid,
        entries=entries,
        email_conflicts=email_conflicts,
    )


def parse_admin_access_command(text: str) -> AdminAccessCommandResult:
    """Parse an admin DM into a grant or revoke command."""
    grant = parse_grant_command(text)
    is_revoke = bool(text and _REVOKE_RE.search(text))

    if is_revoke:
        return AdminAccessCommandResult(
            intent="revoke",
            member_ids=grant.member_ids,
            invalid_ids=grant.invalid_ids,
            entries=[
                GrantCommandEntry(member_id=mid)
                for mid in grant.member_ids
            ],
            email_conflicts=[],
        )

    if grant.is_grant_intent:
        return AdminAccessCommandResult(
            intent="grant",
            member_ids=grant.member_ids,
            invalid_ids=grant.invalid_ids,
            entries=grant.entries,
            email_conflicts=grant.email_conflicts,
        )

    return AdminAccessCommandResult(intent="")


# ---------------------------------------------------------------------------
# Usage message
# ---------------------------------------------------------------------------

USAGE_MESSAGE = (
    "I could not find a valid Slack Member ID.\n\n"
    "Example:\n"
    "Give U08ABC123 access\n\n"
    "Revoke example:\n"
    "Revoke U08ABC123 access\n\n"
    "Bulk example:\n"
    "Give access to:\n"
    "U08ABC123\n"
    "U08DEF456"
)
