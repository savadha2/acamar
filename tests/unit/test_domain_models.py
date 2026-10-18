"""RawDocument validation and behaviour."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument


def _doc(**overrides: object) -> RawDocument:
    base: dict[str, object] = {
        "external_id": "abc",
        "title": "Fun Run",
        "content": "Fun Run — all day on Friday.",
        "domain": Domain.SCHOOL,
        "category": Category.EVENT,
    }
    return RawDocument(**{**base, **overrides})


@pytest.mark.parametrize("field", ["external_id", "title", "content"])
def test_required_string_fields_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        _doc(**{field: ""})


def test_invalid_domain_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _doc(domain="playground")


def test_is_immutable() -> None:
    doc = _doc()
    with pytest.raises(ValidationError):
        doc.title = "changed"  # type: ignore[misc]


def test_optional_fields_default_to_none() -> None:
    doc = _doc()
    assert doc.event_date is None
    assert doc.published_at is None
    assert doc.student_ref is None
    assert doc.headline is None
    assert doc.summary is None
    assert doc.organization is None
    assert doc.is_relevant is True


def test_event_date_accepts_iso_string_and_coerces() -> None:
    assert _doc(event_date="2026-09-04").event_date == date(2026, 9, 4)


def test_round_trips_through_model_dump() -> None:
    doc = _doc(event_date=date(2026, 9, 4))
    assert RawDocument(**doc.model_dump()) == doc


def test_embedding_text_does_not_duplicate_a_leading_title() -> None:
    doc = _doc(title="Book Fair", content="Book Fair — all day on Friday.")
    assert doc.embedding_text == "Book Fair — all day on Friday."


def test_embedding_text_prepends_title_when_absent_from_content() -> None:
    doc = _doc(title="Book Fair", content="An all-day event in the library.")
    assert doc.embedding_text == "Book Fair\n\nAn all-day event in the library."
