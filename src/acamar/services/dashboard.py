"""Builds the "today" view: menu, events, reminders, holidays for the near future."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from acamar.domain.enums import Category, Organization
from acamar.domain.models import Document
from acamar.ports.clock import Clock
from acamar.ports.repositories import DocumentRepository
from acamar.ports.weather import WeatherError, WeatherProvider
from acamar.services.weather import weather_advice

_DEFAULT_WINDOW_DAYS = 7


class DashboardItem(BaseModel):
    """One card entry."""

    id: UUID
    """The underlying ``documents.id`` — needed to call the dismiss endpoint."""
    title: str
    detail: str
    date: date | None
    category: Category
    kind: Literal["info", "alert"] = "info"
    organization: Organization | None = None
    """Closed-taxonomy group for UI columns (``domain.enums.Organization``)."""
    url: str | None = None
    """The source's own page for this item, if it has one — lets the UI
    link out to it (see ``Document.url``'s own docstring)."""


class WeatherCard(BaseModel):
    """Today's forecast, if the fetch succeeded.

    ``None`` on `Dashboard` otherwise (no provider configured, or the fetch
    failed). See docs/weather-design-proposal.md.
    """

    temp_high_f: float
    temp_low_f: float
    condition: str
    advice: list[str]
    """0+ stable codes ("umbrella", "warm", "sun") from `weather_advice` —
    the UI maps each to an icon + short label, not a sentence.
    """


class Dashboard(BaseModel):
    """Everything the "today" screen needs. Empty lists render an empty state."""

    as_of: date
    window_days: int
    weather: WeatherCard | None = None
    menu: list[DashboardItem]
    menu_fallback_date: date | None = None
    """Set only when `menu` isn't for `as_of` — the nearest future date that
    had a live menu instead, shown so "no menu today" doesn't just look like
    a broken/empty dashboard. `None` with a non-empty `menu` means today's
    own menu was found; `None` with an empty `menu` means nothing exists
    upstream at all, today or later.
    """
    events: list[DashboardItem]
    reminders: list[DashboardItem]
    holidays: list[DashboardItem]


class DashboardService:
    """Assembles the "today" view from the document store.

    Only ``is_relevant`` documents are shown — that flag is a dashboard-only
    filter (set at ingestion time by the enrichment step); retrieval/search
    stays over the full corpus regardless. See
    docs/dashboard-redesign-proposal.md §2.3.
    """

    def __init__(
        self,
        *,
        documents: DocumentRepository,
        clock: Clock,
        weather: WeatherProvider | None = None,
    ) -> None:
        self._documents = documents
        self._clock = clock
        self._weather = weather

    async def today(self, *, window_days: int = _DEFAULT_WINDOW_DAYS) -> Dashboard:
        """Menu for today; events/reminders/holidays through ``window_days`` ahead.

        Events/reminders/holidays with no extracted ``event_date`` are
        excluded entirely (2026-09-16 — the enrichment date-extraction
        covers absolute and relative ("tonight", "this Friday") dates now,
        so what's left undated genuinely has none worth surfacing). Purely a
        display filter — nothing is deleted, the full corpus stays
        searchable for a future ``/query``.
        """
        today = self._clock.today()
        horizon = today + timedelta(days=window_days)
        undated_since = today - timedelta(days=window_days)
        menu = await self._documents.list_documents(
            category=Category.MENU, date_from=today, date_to=today
        )
        menu_fallback_date = None
        if not _relevant(menu):
            # Nothing for today (weekend, holiday, or just not uploaded yet)
            # — show the next day that actually has a menu, dated, rather
            # than a bare empty state. See the Pilot Cockpit design notes.
            menu_fallback_date = await self._documents.next_event_date(
                category=Category.MENU, after=today
            )
            if menu_fallback_date is not None:
                menu = await self._documents.list_documents(
                    category=Category.MENU,
                    date_from=menu_fallback_date,
                    date_to=menu_fallback_date,
                )
        events = await self._documents.list_documents(
            category=Category.EVENT,
            date_from=today,
            date_to=horizon,
            undated_since=undated_since,
        )
        reminders = await self._documents.list_documents(
            category=Category.REMINDER,
            date_from=today,
            date_to=horizon,
            undated_since=undated_since,
        )
        holidays = await self._documents.list_documents(
            category=Category.HOLIDAY,
            date_from=today,
            date_to=horizon,
            undated_since=undated_since,
        )
        return Dashboard(
            as_of=today,
            window_days=window_days,
            weather=await self._weather_card(),
            menu=[_item(doc) for doc in _relevant(menu)],
            menu_fallback_date=menu_fallback_date,
            events=[_item(doc) for doc in _dated_and_relevant(events)],
            reminders=[_item(doc, kind="alert") for doc in _dated_and_relevant(reminders)],
            holidays=[_item(doc, kind="alert") for doc in _dated_and_relevant(holidays)],
        )

    async def _weather_card(self) -> WeatherCard | None:
        """``None`` when no provider is configured, or the fetch failed.

        Weather is a nice-to-have overlay, never something that should break
        the rest of the dashboard (docs/weather-design-proposal.md §5).
        """
        if self._weather is None:
            return None
        try:
            forecast = await self._weather.today()
        except WeatherError:
            return None
        return WeatherCard(
            temp_high_f=forecast.temp_high_f,
            temp_low_f=forecast.temp_low_f,
            condition=forecast.condition,
            advice=weather_advice(forecast),
        )


def _relevant(docs: list[Document]) -> list[Document]:
    return [doc for doc in docs if doc.is_relevant]


def _dated_and_relevant(docs: list[Document]) -> list[Document]:
    return [doc for doc in docs if doc.is_relevant and doc.event_date is not None]


def _item(doc: Document, *, kind: Literal["info", "alert"] = "info") -> DashboardItem:
    return DashboardItem(
        id=doc.id,
        title=doc.headline or doc.title,
        detail=_detail(doc),
        date=doc.event_date,
        category=doc.category,
        kind=kind,
        organization=doc.organization,
        url=doc.url,
    )


def _detail(doc: Document) -> str:
    # The ingestion-time LLM summary is the normal path; this is only a net
    # for rows ingested before `summary` existed, or a source that predates
    # the enricher being wired in.
    if doc.summary:
        return doc.summary
    # `content` reads "<school> event: <title> — <when>."; the part after the
    # em-dash is the useful line for a card. Fall back to the whole string.
    _, separator, tail = doc.content.partition(" — ")
    return (tail if separator else doc.content).rstrip(".")
