"""OllamaEnricher — the ollama client is faked, structured JSON output validated."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from acamar.adapters.enrichment.ollama_enricher import OllamaEnricher
from acamar.domain.enums import Category, Domain, Organization
from acamar.domain.models import RawDocument
from acamar.ports.enricher import EnrichmentError

_DOC = RawDocument(
    external_id="<abc@mail.gmail.com>",
    title="Fall Festival - Unlimited Game Pass",
    content="Buy an unlimited game pass for the Fall Festival on Friday.",
    domain=Domain.GMAIL,
    category=Category.EVENT,
)


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _enricher(client: _FakeClient) -> OllamaEnricher:
    return OllamaEnricher(client=client, model="test-chat")  # type: ignore[arg-type]


_VALID_JSON = (
    '{"headline": "Fall Fest game pass", "summary": "Buy an unlimited game pass.", '
    '"is_relevant": true, "organization": "feef", "holiday": null}'
)


async def test_returns_parsed_structured_enrichment() -> None:
    client = _FakeClient(_FakeResponse(_VALID_JSON))
    result = await _enricher(client).enrich(_DOC)
    assert result.headline == "Fall Fest game pass"
    assert result.is_relevant is True
    assert result.organization == Organization.FEEF
    assert result.holiday is None


async def test_requests_a_json_schema_format() -> None:
    client = _FakeClient(_FakeResponse(_VALID_JSON))
    await _enricher(client).enrich(_DOC)
    assert "format" in client.calls[0]
    assert client.calls[0]["model"] == "test-chat"


async def test_event_date_field_parses_when_present() -> None:
    payload = (
        '{"headline": "Chuck E. Cheese Night", "summary": "20% of sales to the school.", '
        '"is_relevant": true, "organization": "feef", "event_date": "2026-09-18"}'
    )
    client = _FakeClient(_FakeResponse(payload))
    result = await _enricher(client).enrich(_DOC)
    assert result.event_date == date(2026, 9, 18)


async def test_received_at_is_included_so_the_model_has_a_year_anchor() -> None:
    client = _FakeClient(_FakeResponse(_VALID_JSON))
    doc = _DOC.model_copy(update={"published_at": datetime(2026, 9, 14, 18, 5, tzinfo=UTC)})
    await _enricher(client).enrich(doc)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "2026-09-14" in prompt


async def test_holiday_field_parses_when_present() -> None:
    payload = (
        '{"headline": "No school Friday", "summary": "Staff day.", '
        '"is_relevant": true, "organization": "faria_admin", '
        '"holiday": {"label": "Staff Learning Day", "event_date": "2026-09-25"}}'
    )
    client = _FakeClient(_FakeResponse(payload))
    result = await _enricher(client).enrich(_DOC)
    assert result.holiday is not None
    assert result.holiday.label == "Staff Learning Day"


async def test_transport_failure_becomes_enrichment_error() -> None:
    client = _FakeClient(error=RuntimeError("connection refused"))
    with pytest.raises(EnrichmentError, match="connection refused"):
        await _enricher(client).enrich(_DOC)


async def test_empty_response_content_raises() -> None:
    client = _FakeClient(_FakeResponse(""))
    with pytest.raises(EnrichmentError, match="no content"):
        await _enricher(client).enrich(_DOC)


async def test_malformed_json_becomes_enrichment_error() -> None:
    client = _FakeClient(_FakeResponse("not valid json"))
    with pytest.raises(EnrichmentError, match="unparseable"):
        await _enricher(client).enrich(_DOC)


async def test_json_missing_required_fields_becomes_enrichment_error() -> None:
    client = _FakeClient(_FakeResponse('{"headline": "only this"}'))
    with pytest.raises(EnrichmentError, match="unparseable"):
        await _enricher(client).enrich(_DOC)


# ---------------------------------------------------------------------------
# summarize() — the narrow headline/summary-only path (2026-10-02,
# cupertino_events). Separate prompt/schema from enrich() above — a scraped
# city webpage isn't an email, and this method never asks for or returns
# event_date/category/organization at all.
# ---------------------------------------------------------------------------

_CITY_DOC = RawDocument(
    external_id="monster-mash",
    title="Monster Mash",
    content="Monster Mash — 5 to 8 p.m. Register today! Monster_Mash_1.jpg Monster_Mash_2.jpg",
    domain=Domain.CITY,
    category=Category.EVENT,
)

_VALID_SUMMARY_JSON = (
    '{"headline": "Halloween party at Quinlan", '
    '"summary": "A kids Halloween event with games, trick-or-treating, and crafts."}'
)


async def test_summarize_returns_parsed_content_summary() -> None:
    client = _FakeClient(_FakeResponse(_VALID_SUMMARY_JSON))
    result = await _enricher(client).summarize(_CITY_DOC)
    assert result.headline == "Halloween party at Quinlan"
    assert "trick-or-treating" in result.summary


async def test_summarize_requests_a_json_schema_format() -> None:
    client = _FakeClient(_FakeResponse(_VALID_SUMMARY_JSON))
    await _enricher(client).summarize(_CITY_DOC)
    assert "format" in client.calls[0]


async def test_summarize_prompt_includes_the_event_title_and_content() -> None:
    client = _FakeClient(_FakeResponse(_VALID_SUMMARY_JSON))
    await _enricher(client).summarize(_CITY_DOC)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Monster Mash" in prompt
    assert "Register today" in prompt  # the raw noisy content, for the model to filter


async def test_summarize_transport_failure_becomes_enrichment_error() -> None:
    client = _FakeClient(error=RuntimeError("connection refused"))
    with pytest.raises(EnrichmentError, match="connection refused"):
        await _enricher(client).summarize(_CITY_DOC)


async def test_summarize_empty_response_content_raises() -> None:
    client = _FakeClient(_FakeResponse(""))
    with pytest.raises(EnrichmentError, match="no content"):
        await _enricher(client).summarize(_CITY_DOC)


async def test_summarize_malformed_json_becomes_enrichment_error() -> None:
    client = _FakeClient(_FakeResponse("not valid json"))
    with pytest.raises(EnrichmentError, match="unparseable"):
        await _enricher(client).summarize(_CITY_DOC)


async def test_summarize_json_missing_required_fields_becomes_enrichment_error() -> None:
    # Missing analogue of test_json_missing_required_fields_becomes_
    # enrichment_error above, found by review (2026-10-02) — valid JSON
    # that's missing a required ContentSummary field hits a different
    # (pydantic-validation) branch than malformed JSON syntax does.
    client = _FakeClient(_FakeResponse('{"headline": "only this"}'))
    with pytest.raises(EnrichmentError, match="unparseable"):
        await _enricher(client).summarize(_CITY_DOC)


async def test_summarize_blank_summary_becomes_enrichment_error() -> None:
    # Regression guard for ContentSummary's new min_length=1 (2026-10-02,
    # found by review) — a technically-schema-conforming blank string used
    # to pass through silently and write an empty card.
    client = _FakeClient(_FakeResponse('{"headline": "A title", "summary": ""}'))
    with pytest.raises(EnrichmentError, match="unparseable"):
        await _enricher(client).summarize(_CITY_DOC)
