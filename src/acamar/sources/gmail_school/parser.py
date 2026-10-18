"""Parses one school/ParentSquare email into a `RawDocument`.

Real captured mail (2026-09-15, under a ParentSquare-filtered label) is
free-form prose, not a fixed template like Faria's calendar grid or the menu
digest — dates are buried mid-sentence with HTML-to-text artifacts (stray
``&nbsp;``, doubled markdown bold). Reliably understanding prose like that is
an LLM-extraction job (the not-yet-built Router/Synthesis layer), not a
connector's. This parser is deliberately **best-effort, not exhaustive**:

- ``event_date`` is extracted only from the **subject line**, in one of two
  unambiguous shapes:
  - ``Month D, YYYY`` / ``Mon D, YYYY`` — e.g. "Community Board Office Hour -
    September 21, 2026 6:00-7:00 pm".
  - ``D Mon - D Mon`` (no year, dash or en dash) — the recurring
    weekly-digest pattern, e.g. "Faria Falcon 07 Sep - 11 Sep". The range's
    **end** day is used as ``event_date`` (the digest stays current through
    that day); the year comes from the email's own ``received_at``, not
    guessed. Known gap: a range spanning a year boundary (Dec to Jan) would
    get the wrong year — not worth the complexity for a school-week digest
    that never does that.
  No attempt is made at other relative dates ("Friday", "16 Sep" with no
  year and no range) or dates buried in the body.
- No date found → the email still becomes a document (never silently
  dropped), just filed as :class:`~acamar.domain.enums.Category.REMINDER`
  with ``event_date=None`` instead of :class:`Category.EVENT`.

**Privacy guardrail (2026-09-15, revised same day):** skip *conversational*
mail, not *direct* mail — the teacher's direct one-way announcements
("Parking Permit for Friday") are exactly as valuable as ParentSquare's, so
"not relayed through ParentSquare" turned out to be the wrong signal (tried
first, reverted). What actually needs excluding is a back-and-forth exchange
the household is participating in — private conversation, not a broadcast —
detected two ways, both cheap and needing no extra data capture:

- **Self-sent**: ``sender`` matches ``HOUSEHOLD_EMAIL_ADDRESSES``. The
  household never needs its own outgoing mail summarized back to it.
- **Reply/forward**: ``subject`` starts with ``Re:`` / ``Fwd:`` / ``Fw:``
  (case-insensitive). A reply — even the teacher's — is almost always
  personal back-and-forth, not a fresh announcement.

Together these explain the real example that prompted this: a household-sent
"9/9 Wed Test" plus the teacher's "Re: 9/9 Wed Test" reply, while leaving
genuine one-way teacher content untouched. More robust than a subject-prefix
guess would be real threading headers (``In-Reply-To``/``References`` or
Gmail's ``threadId``) — not captured by ``RawEmail`` today; worth adding if
this heuristic ever mis-fires in practice.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from acamar.config import get_settings
from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.ports.mail_client import RawEmail

_DATE_PATTERN = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_DATE_FORMATS = ("%B %d %Y", "%b %d %Y")

# "07 Sep - 11 Sep" (hyphen or a dash-like unicode separator, no year) — the
# weekly-digest subject shape. Captures the range's end (day, month).
_DATE_RANGE_PATTERN = re.compile(
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s*[-\u2013\u2014]\s*(\d{1,2})\s+([A-Za-z]{3,9})\b"
)
_RANGE_END_FORMATS = ("%d %b %Y", "%d %B %Y")

# Boilerplate ParentSquare appends after the real content; cut at the first one seen.
_FOOTER_MARKERS = (
    "To see the post in ParentSquare",
    "To reply to this email",
)
_MAX_CONTENT_CHARS = 2000

# A genuine reply/forward prefix at the *start* of the subject only — a
# subject that merely contains "re:" mid-string must not match.
_REPLY_OR_FORWARD_PATTERN = re.compile(r"^\s*(re|fwd?)\s*:", re.IGNORECASE)


def parse_school_email(email: RawEmail) -> list[RawDocument]:
    """Return one `RawDocument` for this email, or ``[]`` — never raises.

    ``[]`` means this is conversational mail (self-sent, or a reply/forward)
    — see the module docstring's "Privacy guardrail". Otherwise exactly one
    document: ``external_id`` is the bare Message-ID, unlike the menu digest,
    one email here is exactly one item, so no compositing is needed.
    """
    if _is_conversational(email):
        return []

    event_date = _extract_subject_date(email.subject, received_year=email.received_at.year)
    category = Category.EVENT if event_date is not None else Category.REMINDER
    return [
        RawDocument(
            external_id=email.message_id,
            title=email.subject or "(no subject)",
            content=_clean_content(email),
            domain=Domain.GMAIL,
            category=category,
            event_date=event_date,
            published_at=email.received_at,
        )
    ]


def _is_conversational(email: RawEmail) -> bool:
    if _REPLY_OR_FORWARD_PATTERN.match(email.subject or ""):
        return True
    household = _household_addresses()
    sender_lower = email.sender.lower()
    return any(addr in sender_lower for addr in household)


def _household_addresses() -> frozenset[str]:
    raw = get_settings().household_email_addresses
    return frozenset(addr.strip().lower() for addr in raw.split(",") if addr.strip())


def _extract_subject_date(subject: str, *, received_year: int) -> date | None:
    match = _DATE_PATTERN.search(subject)
    if match is not None:
        raw = f"{match.group(1)} {match.group(2)} {match.group(3)}"
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

    range_match = _DATE_RANGE_PATTERN.search(subject)
    if range_match is not None:
        end_day, end_month = range_match.group(1), range_match.group(2)
        raw = f"{end_day} {end_month} {received_year}"
        for fmt in _RANGE_END_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

    return None


def _clean_content(email: RawEmail) -> str:
    body = email.text_body or ""
    for marker in _FOOTER_MARKERS:
        cut = body.find(marker)
        if cut != -1:
            body = body[:cut]
    body = body.replace("&nbsp;", " ")
    body = re.sub(r"\*{2,}", "", body)  # markdown-bold noise from HTML->text
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > _MAX_CONTENT_CHARS:
        body = body[:_MAX_CONTENT_CHARS].rstrip() + "…"
    return body or email.subject or "(no content)"
