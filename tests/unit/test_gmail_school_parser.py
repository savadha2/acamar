"""parse_school_email — best-effort subject-date extraction + content cleanup.

Fixtures mirror the real shape observed 2026-09-15 (ParentSquare digests,
direct teacher mail) without using captured personal content.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from acamar.config import get_settings
from acamar.domain.enums import Category, Domain
from acamar.ports.mail_client import RawEmail
from acamar.sources.gmail_school.parser import parse_school_email


def _email(**overrides: object) -> RawEmail:
    base: dict[str, object] = {
        "message_id": "<abc123@mail.gmail.com>",
        "subject": "Some event",
        # A real ParentSquare broadcast sender — the parser only turns these
        # into documents. Override explicitly to test the guardrail itself.
        "sender": "donotreply+abc123@parentsquare.com",
        "received_at": datetime(2026, 9, 14, 18, 5, tzinfo=UTC),
        "text_body": "Body text.",
    }
    base.update(overrides)
    return RawEmail(**base)


def test_full_date_in_subject_becomes_an_event() -> None:
    email = _email(subject="Community Board Office Hour - September 21, 2026 6:00-7:00 pm")
    docs = parse_school_email(email)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.category == Category.EVENT
    assert doc.event_date == date(2026, 9, 21)
    assert doc.domain == Domain.GMAIL
    assert doc.external_id == "<abc123@mail.gmail.com>"


def test_abbreviated_month_with_year_is_parsed() -> None:
    email = _email(subject="FEEF Board Meeting - Sep 9, 2026")
    docs = parse_school_email(email)
    assert docs[0].event_date == date(2026, 9, 9)
    assert docs[0].category == Category.EVENT


def test_weekly_digest_date_range_uses_the_end_day_and_email_year() -> None:
    # Real subjects use an en dash character, not a hyphen — this is the
    # actual character, not a typo, so it's worth its own regression test.
    email = _email(
        subject="Faria Falcon 07 Sep – 11 Sep",  # noqa: RUF001 - real en dash, not a typo
        received_at=datetime(2026, 9, 9, 13, 6, tzinfo=UTC),
    )
    docs = parse_school_email(email)
    assert docs[0].event_date == date(2026, 9, 11)
    assert docs[0].category == Category.EVENT


def test_weekly_digest_date_range_accepts_a_plain_hyphen_too() -> None:
    email = _email(subject="Faria Falcon 07 Sep - 11 Sep")
    docs = parse_school_email(email)
    assert docs[0].event_date == date(2026, 9, 11)


def test_day_and_month_with_no_year_is_not_guessed() -> None:
    email = _email(subject="Faria School Site Council Meeting - 16 Sep")
    docs = parse_school_email(email)
    assert docs[0].event_date is None
    assert docs[0].category == Category.REMINDER


def test_relative_date_is_not_guessed() -> None:
    email = _email(subject="Parking Permit for Friday")
    docs = parse_school_email(email)
    assert docs[0].event_date is None
    assert docs[0].category == Category.REMINDER


def test_no_date_anywhere_still_produces_a_document() -> None:
    email = _email(subject="Sweat Challenge", text_body="Join the fundraiser!")
    docs = parse_school_email(email)
    assert len(docs) == 1
    assert docs[0].title == "Sweat Challenge"
    assert docs[0].content == "Join the fundraiser!"


def test_parentsquare_footer_is_stripped() -> None:
    email = _email(
        text_body=(
            "The real content goes here.\n\n"
            "To see the post in ParentSquare, please visit the following link: "
            "https://www.parentsquare.com/feeds/12345?s=verylongtoken"
        )
    )
    doc = parse_school_email(email)[0]
    assert "parentsquare.com" not in doc.content
    assert doc.content == "The real content goes here."


def test_nbsp_and_markdown_bold_noise_is_cleaned() -> None:
    email = _email(
        text_body="on **Monday, September**** &nbsp;21 ****, 2026, from 6:00-7:00 pm**"
    )
    doc = parse_school_email(email)[0]
    assert "&nbsp;" not in doc.content
    assert "**" not in doc.content


def test_empty_body_falls_back_to_subject() -> None:
    email = _email(subject="Thursday Morning Help", text_body=None)
    doc = parse_school_email(email)[0]
    assert doc.content == "Thursday Morning Help"


# ---------------------------------------------------------------------------
# privacy guardrail — conversational mail is skipped, one-way mail is kept
# regardless of sender (direct teacher mail is exactly as valuable as
# ParentSquare's — only excluded when it's part of a back-and-forth)
# ---------------------------------------------------------------------------


def test_direct_one_way_teacher_email_is_kept() -> None:
    # Not a reply, not self-sent — this is real content, must NOT be skipped
    # just because it bypassed ParentSquare.
    email = _email(subject="Parking Permit for Friday", sender="chalmers_alexandra@cusdk8.org")
    assert len(parse_school_email(email)) == 1


def test_reply_from_the_teacher_is_skipped() -> None:
    email = _email(subject="Re: 9/9 Wed Test", sender="chalmers_alexandra@cusdk8.org")
    assert parse_school_email(email) == []


def test_forward_is_skipped() -> None:
    email = _email(subject="Fwd: Some announcement")
    assert parse_school_email(email) == []


def test_reply_prefix_match_is_case_insensitive() -> None:
    email = _email(subject="RE: 9/9 Wed Test")
    assert parse_school_email(email) == []


def test_reply_prefix_must_be_at_the_start_not_mid_subject() -> None:
    email = _email(subject="Please Re: schedule pickup for Friday")
    assert len(parse_school_email(email)) == 1


def test_self_sent_is_skipped_even_without_a_reply_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOUSEHOLD_EMAIL_ADDRESSES", "sharath.avadh@gmail.com")
    get_settings.cache_clear()
    email = _email(subject="9/9 Wed Test", sender="Sharath Avadhanam <sharath.avadh@gmail.com>")
    assert parse_school_email(email) == []


def test_non_household_sender_is_not_self_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOUSEHOLD_EMAIL_ADDRESSES", "sharath.avadh@gmail.com")
    get_settings.cache_clear()
    email = _email(subject="Sweat Challenge", sender="donotreply+x@parentsquare.com")
    assert len(parse_school_email(email)) == 1


def test_long_body_is_truncated() -> None:
    email = _email(text_body="x" * 5000)
    doc = parse_school_email(email)[0]
    assert len(doc.content) <= 2001  # cap + ellipsis
    assert doc.content.endswith("…")


def test_missing_subject_falls_back_to_placeholder_title() -> None:
    email = _email(subject="")
    doc = parse_school_email(email)[0]
    assert doc.title == "(no subject)"
