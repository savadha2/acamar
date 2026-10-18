"""DashboardService — windowing and card shaping, fake repo."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from acamar.domain.enums import Category, Domain, Organization
from acamar.domain.models import Document
from acamar.ports.weather import WeatherForecast
from acamar.services.dashboard import DashboardService
from tests.fakes import FakeDocumentRepository, FakeWeatherProvider, FrozenClock

TODAY = date(2026, 9, 9)
CLOCK = FrozenClock(datetime(2026, 9, 9, 8, 0, tzinfo=UTC))


def _doc(
    category: Category,
    title: str,
    event_date: date | None,
    *,
    summary: str | None = None,
    headline: str | None = None,
    is_relevant: bool = True,
    organization: Organization | None = None,
    published_at: datetime | None = None,
    url: str | None = None,
) -> Document:
    return Document(
        id=uuid4(),
        source_id=uuid4(),
        domain=Domain.SCHOOL,
        category=category,
        student_ref=None,
        title=title,
        content=f"Faria Elementary School event: {title} — all day on {event_date}.",
        headline=headline,
        summary=summary,
        is_relevant=is_relevant,
        organization=organization,
        external_id=title,
        event_date=event_date,
        published_at=published_at,
        url=url,
    )


@pytest.fixture
def service() -> DashboardService:
    stored = [
        _doc(Category.EVENT, "FEEF Meeting", TODAY),
        _doc(Category.EVENT, "Book Fair", date(2026, 9, 14)),
        _doc(Category.EVENT, "Past Thing", date(2026, 9, 1)),
        _doc(Category.EVENT, "Far Future", date(2026, 12, 1)),
        _doc(Category.REMINDER, "Permission slip due", date(2026, 9, 11)),
    ]
    return DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)


async def test_events_within_the_window_only(service: DashboardService) -> None:
    board = await service.today(window_days=7)
    titles = [item.title for item in board.events]
    assert titles == ["FEEF Meeting", "Book Fair"]  # past + far-future excluded


async def test_reminders_are_marked_alert(service: DashboardService) -> None:
    board = await service.today()
    assert [r.title for r in board.reminders] == ["Permission slip due"]
    assert all(r.kind == "alert" for r in board.reminders)


async def test_menu_is_empty_when_no_menu_docs(service: DashboardService) -> None:
    board = await service.today()
    assert board.menu == []
    assert board.menu_fallback_date is None


async def test_menu_shows_today_with_no_fallback_when_todays_menu_exists() -> None:
    stored = [_doc(Category.MENU, "Lunch Menu — today", TODAY)]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert [m.title for m in board.menu] == ["Lunch Menu — today"]
    assert board.menu_fallback_date is None


async def test_menu_falls_back_to_the_next_available_date() -> None:
    # Nothing for TODAY (weekend/holiday/not uploaded that far) — the next
    # dated menu should surface instead, labeled with its own date.
    stored = [_doc(Category.MENU, "Lunch Menu — next", date(2026, 9, 14))]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert [m.title for m in board.menu] == ["Lunch Menu — next"]
    assert board.menu_fallback_date == date(2026, 9, 14)


async def test_menu_fallback_picks_the_nearest_future_date_not_the_farthest() -> None:
    stored = [
        _doc(Category.MENU, "Lunch Menu — far", date(2026, 9, 20)),
        _doc(Category.MENU, "Lunch Menu — near", date(2026, 9, 11)),
    ]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.menu_fallback_date == date(2026, 9, 11)
    assert [m.title for m in board.menu] == ["Lunch Menu — near"]


async def test_menu_fallback_ignores_a_past_only_menu() -> None:
    # A menu that only ever had past days (never re-ingested since) must not
    # resurface as "next available" — it isn't.
    stored = [_doc(Category.MENU, "Lunch Menu — past", date(2026, 9, 1))]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.menu == []
    assert board.menu_fallback_date is None


async def test_holidays_within_the_window_are_returned_as_alerts() -> None:
    stored = [_doc(Category.HOLIDAY, "Staff Learning Day", date(2026, 9, 12))]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert [h.title for h in board.holidays] == ["Staff Learning Day"]
    assert board.holidays[0].kind == "alert"


async def test_not_relevant_documents_are_excluded_from_every_list() -> None:
    stored = [
        _doc(Category.EVENT, "Noise", TODAY, is_relevant=False),
        _doc(Category.REMINDER, "Also noise", date(2026, 9, 11), is_relevant=False),
    ]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events == []
    assert board.reminders == []


async def test_dismissed_item_is_excluded_even_before_its_due_date() -> None:
    doc = _doc(Category.EVENT, "Fall Festival", date(2026, 9, 14))
    repo = FakeDocumentRepository([doc])
    await repo.dismiss(doc.id)  # marked "done" while still in the future window
    service = DashboardService(documents=repo, clock=CLOCK)
    board = await service.today()
    assert board.events == []


async def test_a_holiday_extracted_from_an_irrelevant_parent_still_shows() -> None:
    # The two mechanisms (relevance filtering, holiday extraction) are
    # independent — a low-relevance newsletter can still yield a holiday doc
    # that's itself relevant. See docs/dashboard-redesign-proposal.md §2.3.
    stored = [
        _doc(Category.REMINDER, "September Newsletter", None, is_relevant=False),
        _doc(Category.HOLIDAY, "Staff Learning Day", date(2026, 9, 12), is_relevant=True),
    ]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.reminders == []
    assert [h.title for h in board.holidays] == ["Staff Learning Day"]


async def test_title_prefers_the_stored_headline_when_present() -> None:
    stored = [_doc(Category.EVENT, "TOMORROW is the Fest!!", TODAY, headline="Fest tomorrow")]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events[0].title == "Fest tomorrow"


async def test_title_falls_back_to_raw_title_without_a_headline(
    service: DashboardService,
) -> None:
    board = await service.today()
    assert "FEEF Meeting" in [e.title for e in board.events]


async def test_organization_is_passed_through() -> None:
    stored = [_doc(Category.EVENT, "Board Meeting", TODAY, organization=Organization.DISTRICT)]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events[0].organization == Organization.DISTRICT


async def test_url_is_passed_through() -> None:
    url = "https://www.cupertino.gov/Parks-Recreation/Events/Monster-Mash"
    stored = [_doc(Category.EVENT, "Monster Mash", TODAY, url=url)]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events[0].url == url


async def test_url_is_none_when_the_source_has_none() -> None:
    stored = [_doc(Category.EVENT, "Book Fair", TODAY)]  # no url= given -> None
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events[0].url is None


async def test_detail_is_the_text_after_the_em_dash(service: DashboardService) -> None:
    board = await service.today()
    feef = next(e for e in board.events if e.title == "FEEF Meeting")
    assert feef.detail == f"all day on {TODAY}"


async def test_detail_prefers_the_stored_summary_when_present() -> None:
    stored = [_doc(Category.EVENT, "FEEF Meeting", TODAY, summary="Short LLM gloss.")]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today()
    assert board.events[0].detail == "Short LLM gloss."


async def test_undated_reminder_is_excluded_regardless_of_how_recent() -> None:
    # No event_date at all (best-effort Gmail parse/enrichment found none) —
    # excluded outright (2026-09-16), not just when old. The extraction now
    # covers absolute and relative dates; what's still undated genuinely has
    # none worth surfacing.
    stored = [
        _doc(
            Category.REMINDER,
            "Old Fundraiser",
            None,
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        _doc(
            Category.REMINDER,
            "Fresh Fundraiser",
            None,
            published_at=datetime(2026, 9, 8, tzinfo=UTC),  # yesterday — still excluded
        ),
    ]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today(window_days=7)
    assert board.reminders == []


async def test_undated_reminder_with_no_published_at_is_also_excluded() -> None:
    stored = [_doc(Category.REMINDER, "Mystery Reminder", None, published_at=None)]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today(window_days=7)
    assert board.reminders == []


async def test_dated_reminder_still_shows() -> None:
    stored = [_doc(Category.REMINDER, "Permission Slip", date(2026, 9, 11))]
    service = DashboardService(documents=FakeDocumentRepository(stored), clock=CLOCK)
    board = await service.today(window_days=7)
    assert [r.title for r in board.reminders] == ["Permission Slip"]


async def test_window_days_is_echoed(service: DashboardService) -> None:
    assert (await service.today(window_days=3)).window_days == 3
    assert (await service.today()).as_of == TODAY


async def test_weather_is_none_when_no_provider_configured(service: DashboardService) -> None:
    # `service` fixture builds DashboardService with no `weather=` kwarg.
    board = await service.today()
    assert board.weather is None


async def test_weather_card_is_populated_from_the_provider() -> None:
    forecast = WeatherForecast(
        temp_high_f=64.0,
        temp_low_f=50.0,
        precipitation_probability=60.0,
        condition="rain likely",
    )
    service = DashboardService(
        documents=FakeDocumentRepository([]),
        clock=CLOCK,
        weather=FakeWeatherProvider(forecast),
    )
    board = await service.today()
    assert board.weather is not None
    assert board.weather.temp_high_f == 64.0
    assert board.weather.condition == "rain likely"
    assert "umbrella" in board.weather.advice


async def test_weather_fetch_failure_degrades_to_no_card() -> None:
    # A broken forecast API must not break the rest of the dashboard.
    stored = [_doc(Category.EVENT, "FEEF Meeting", TODAY)]
    service = DashboardService(
        documents=FakeDocumentRepository(stored),
        clock=CLOCK,
        weather=FakeWeatherProvider(fail=True),
    )
    board = await service.today()
    assert board.weather is None
    assert [e.title for e in board.events] == ["FEEF Meeting"]
