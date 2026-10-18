"""Pure-logic tests for `dashboard/streamlit_app.py` — no Streamlit runtime
needed, same rationale as the gap this file previously had zero coverage
for (docs/gap-analysis-2026-09-26.md finding 4, about `cockpit_app.py`;
`streamlit_app.py` had the identical gap, unaddressed until now). Covers
only the functions added/changed 2026-09-29
(docs/p2-5-and-attention-card-implementation-spec.md) — not a full sweep
of every pre-existing function in this module.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from acamar.dashboard.streamlit_app import (
    _attention_card,
    _attention_tier,
    _day_card_html,
    _food_chip_html,
    _food_items,
    _hex,
    _item_icon,
    _linked,
    _meal_section_html,
    _menu_items_label,
    _row_html,
    _todays_meals_html,
    _urgency_color,
    _weather_box_html,
)


def test_hex_formats_an_rgb_tuple_as_lowercase_hex() -> None:
    assert _hex((220, 38, 38)) == "#dc2626"


def test_urgency_color_still_works_after_being_rewritten_to_use_hex() -> None:
    # Regression guard for the _hex() extraction — _urgency_color's own
    # observable output must be unchanged.
    assert _urgency_color(0) == "#dc2626"


def test_attention_tier_is_today_at_zero_days() -> None:
    label, accent = _attention_tier(0)
    assert label == "TODAY"
    assert accent == "#dc2626"  # same red as _urgency_color(0)


def test_attention_tier_is_soon_for_one_to_three_days() -> None:
    for days in (1, 2, 3):
        label, accent = _attention_tier(days)
        assert label == "SOON"
        assert accent == "#eab308"  # amber, distinct from TODAY's red


def test_attention_tier_defaults_to_soon_neutral_when_days_is_none() -> None:
    # Defensive-only branch — _attention_card is only ever called with
    # already-urgent (0-3 day) items in practice, but must not crash if
    # that assumption is ever violated.
    label, accent = _attention_tier(None)
    assert label == "SOON"
    assert accent == "#aaaaaa"


def test_attention_card_uses_the_tier_label_and_accent_not_a_flat_fill() -> None:
    item = {"title": "Picture Day", "detail": "Bring a form", "date": "2026-09-29"}
    html = _attention_card(item, as_of=date(2026, 9, 29))

    assert "TODAY" in html
    assert "#dc2626" in html  # the accent color, driving border/background/label
    assert "Picture Day" in html
    assert "Bring a form" in html


def test_food_items_returns_the_full_list_for_a_plain_comma_list() -> None:
    # Breakfast's real shape — no "Option N -" fragments at all, every
    # chunk survives unchanged.
    assert (
        _food_items("Banana Strawberry, Yogurt Smoothie, Fresh Fruits, Milk")
        == "Banana Strawberry, Yogurt Smoothie, Fresh Fruits, Milk"
    )


def test_food_items_drops_option_labels_but_keeps_every_food_for_the_real_lunch_shape() -> None:
    # Real lunch content string (sources.manual_menu.parser's 3-option
    # format) — every "Option N - Category" fragment must be dropped, but
    # every actual food item kept, not just the first.
    items = (
        "Option 1 - Meat, Chicken Sandwich, Option 2 - Vegetarian, Veggie Burger, "
        "Option 3 - Cold Deli, Turkey and Cheese, Sandwich"
    )

    assert (
        _food_items(items)
        == "Chicken Sandwich, Veggie Burger, Turkey and Cheese, Sandwich"
    )


def test_food_items_falls_back_to_the_whole_text_if_every_chunk_is_a_label() -> None:
    assert _food_items("Option 1 - Meat") == "Option 1 - Meat"


def test_menu_items_label_strips_the_meal_on_date_prefix() -> None:
    # Real shape services.dashboard._detail produces for a menu doc (no
    # summary, no " — " separator, falls through to the full content
    # string): "{Meal} on {weekday}, {month} {day}, {year}: {items}".
    item = {
        "detail": "Breakfast on Tuesday, September 29, 2026: Banana Strawberry, Yogurt Smoothie",
    }

    assert _menu_items_label(item) == "Banana Strawberry, Yogurt Smoothie"


def test_menu_items_label_falls_back_to_the_full_detail_if_no_colon_separator() -> None:
    item = {"detail": "no colon here"}

    assert _menu_items_label(item) == "no colon here"


def test_food_chip_html_wraps_one_food_as_its_own_chip() -> None:
    assert _food_chip_html("Chicken Sandwich") == (
        '<span class="acamar-food-chip">Chicken Sandwich</span>'
    )


def test_weather_box_html_shows_temp_and_advice_stacked() -> None:
    weather = {"temp_high_f": 88.3, "condition": "clear", "advice": ["sun"]}

    html = _weather_box_html(weather)

    assert "acamar-weather-box-temp" in html
    assert "88°F" in html
    assert "acamar-weather-box-advice" in html
    assert "Hat + sunscreen" in html  # advice as plain text, no separate icon badge
    assert "☀️" in html  # the condition icon


def test_weather_box_html_joins_multiple_advice_codes() -> None:
    weather = {"temp_high_f": 42.0, "condition": "rain likely", "advice": ["umbrella", "warm"]}

    html = _weather_box_html(weather)

    assert "Umbrella, Warm" in html


def test_weather_box_html_omits_the_advice_line_when_none_applies() -> None:
    weather = {"temp_high_f": 65.0, "condition": "clear", "advice": []}

    html = _weather_box_html(weather)

    assert "65°F" in html
    assert "acamar-weather-box-advice" not in html  # no advice line at all, not an empty one


def test_meal_section_html_renders_one_discrete_chip_per_food_item() -> None:
    item = {
        "title": "Breakfast Menu — Tuesday",
        "detail": "Breakfast on Tuesday: Banana Strawberry, Yogurt Smoothie, Milk",
        "category": "menu",
    }

    html = _meal_section_html(item)

    assert html.count("acamar-food-chip\"") == 3  # 3 items -> 3 separate chips
    assert "BREAKFAST" in html  # uppercased meal-name label
    assert "Banana Strawberry, Yogurt Smoothie" not in html  # not one joined string


def test_day_card_html_puts_date_and_weather_in_the_top_row() -> None:
    # 2026-09-29: the day card is date+weather only now — meals moved out
    # to their own section below the ask box (_todays_meals_html), so
    # this fixture's "menu" data is deliberately irrelevant/ignored here.
    fresh = {
        "as_of": "2026-09-29",
        "weather": {"temp_high_f": 88.3, "condition": "clear", "advice": ["sun"]},
        "menu_fallback_date": None,
        "menu": [{"title": "Breakfast Menu — Tuesday", "detail": "irrelevant"}],
    }

    html = _day_card_html(fresh)

    assert "acamar-day-card-vdivider" in html
    assert "Tue, Sep 29" in html
    assert "TODAY" in html
    assert "88°F" in html
    assert "acamar-meal-section" not in html  # meals never render inside the card anymore


def test_day_card_html_still_renders_date_only_if_weather_is_absent() -> None:
    fresh: dict[str, Any] = {"as_of": "2026-09-29", "weather": None}

    html = _day_card_html(fresh)

    assert "Tue, Sep 29" in html
    assert "acamar-day-card-vdivider" not in html  # no weather -> no vertical divider either


def test_todays_meals_html_shows_a_labeled_chip_row_per_meal() -> None:
    data = {
        "menu_fallback_date": None,
        "menu": [
            {
                "title": "Breakfast Menu — Tuesday",
                "detail": "Breakfast on Tuesday: Banana Strawberry, Yogurt Smoothie",
                "category": "menu",
            },
            {
                "title": "Lunch Menu — Tuesday",
                "detail": "Lunch on Tuesday: Chicken Sandwich, Veggie Burger",
                "category": "menu",
            },
        ],
    }

    html = _todays_meals_html(data)

    assert html.count("acamar-meal-section") == 2  # one section per meal
    assert "BREAKFAST" in html
    assert "LUNCH" in html
    # Full item lists, each as its own chip, not a comma-joined line.
    assert "Banana Strawberry" in html
    assert "Yogurt Smoothie" in html
    assert "Chicken Sandwich" in html
    assert "Veggie Burger" in html


def test_todays_meals_html_is_empty_for_a_fallback_future_date() -> None:
    # Deliberate: no "no menu today" message either — see this function's
    # own docstring on the behavior this removed.
    data = {
        "menu_fallback_date": "2026-10-01",  # not today's own menu
        "menu": [{"title": "Lunch Menu — Thursday", "detail": "Lunch on Thursday: Pizza"}],
    }

    assert _todays_meals_html(data) == ""


def test_todays_meals_html_is_empty_when_there_is_no_menu_at_all() -> None:
    # Pre-existing mypy gap fixed in passing (2026-10-02): `mypy` was being
    # run as `mypy src/` all session, silently skipping `tests/`/`scripts/`
    # despite pyproject.toml's own `files = ["src", "tests", "scripts"]` and
    # CLAUDE.md's documented bare `uv run mypy` invocation — this empty-list
    # literal needed an explicit annotation only the bare invocation caught.
    data: dict[str, Any] = {"menu_fallback_date": None, "menu": []}

    assert _todays_meals_html(data) == ""


# ---------------------------------------------------------------------------
# _linked / _item_icon / _row_html — cupertino_events additions (2026-10-02):
# a tree icon, a green left-border, and a hyperlink to the real event page.
# Not a full sweep of _row_html/_item_icon (pre-existing, untested before
# this) — just what changed.
# ---------------------------------------------------------------------------


def test_linked_wraps_in_an_anchor_when_url_present() -> None:
    html = _linked("Monster Mash", "https://www.cupertino.gov/x/Monster-Mash")

    assert html.startswith("<a ")
    assert 'href="https://www.cupertino.gov/x/Monster-Mash"' in html
    assert 'target="_blank"' in html
    assert html.endswith("Monster Mash</a>")


def test_linked_returns_plain_text_when_no_url() -> None:
    assert _linked("Book Fair", None) == "Book Fair"


def test_linked_escapes_the_url() -> None:
    html = _linked("x", 'https://example.test/?a=1&b="x"')
    assert '"x"' not in html.split("href=")[1].split(">")[0]  # the quote got escaped out


def test_item_icon_city_recreation_gets_the_tree_icon() -> None:
    item = {"category": "event", "organization": "city_recreation"}

    assert _item_icon(item) == "🌳"


def test_item_icon_school_calendar_is_unaffected() -> None:
    item = {"category": "event", "organization": "school_calendar"}

    assert _item_icon(item) == "🎓"


def test_row_html_city_recreation_gets_the_green_accent_not_the_category_blue() -> None:
    item = {
        "title": "Monster Mash",
        "detail": "Halloween event",
        "date": "2026-10-24",
        "category": "event",
        "organization": "city_recreation",
    }
    html = _row_html(item, as_of=date(2026, 10, 2))

    assert "#22c55e" in html
    assert "#3b82f6" not in html  # the plain-EVENT blue must NOT also appear


def test_row_html_non_city_event_keeps_the_existing_category_blue() -> None:
    item = {
        "title": "Book Fair",
        "detail": "Buy a book",
        "date": "2026-09-14",
        "category": "event",
        "organization": "school_calendar",
    }
    html = _row_html(item, as_of=date(2026, 9, 9))

    assert "#3b82f6" in html
    assert "#22c55e" not in html


def test_row_html_links_the_title_when_the_item_has_a_url() -> None:
    item = {
        "title": "Monster Mash",
        "detail": "Halloween event",
        "date": "2026-10-24",
        "category": "event",
        "organization": "city_recreation",
        "url": "https://www.cupertino.gov/x/Monster-Mash",
    }
    html = _row_html(item, as_of=date(2026, 10, 2))

    assert 'href="https://www.cupertino.gov/x/Monster-Mash"' in html


def test_row_html_title_is_plain_text_without_a_url() -> None:
    item = {
        "title": "Book Fair",
        "detail": "Buy a book",
        "date": "2026-09-14",
        "category": "event",
        "organization": "school_calendar",
    }
    html = _row_html(item, as_of=date(2026, 9, 9))

    assert "<a " not in html


def test_attention_card_includes_the_item_icon() -> None:
    # Real gap found by review (2026-10-02): this card never called
    # _item_icon at all — a city event that becomes urgent lost its tree
    # icon the moment it moved from "Coming up" into "Needs your attention".
    item = {
        "title": "Monster Mash",
        "detail": "Halloween event",
        "date": "2026-09-29",
        "category": "event",
        "organization": "city_recreation",
    }
    html = _attention_card(item, as_of=date(2026, 9, 29))

    assert "🌳" in html


def test_attention_card_accent_stays_urgency_colored_not_org_colored() -> None:
    # The icon fix above must not also repurpose this card's accent border
    # — that's deliberately urgency (TODAY/SOON), not category/organization.
    item = {
        "title": "Monster Mash",
        "detail": "Halloween event",
        "date": "2026-09-29",
        "category": "event",
        "organization": "city_recreation",
    }
    html = _attention_card(item, as_of=date(2026, 9, 29))

    assert "#dc2626" in html  # TODAY red, from _attention_tier
    assert "#22c55e" not in html  # NOT the city-recreation green


def test_row_html_holiday_category_keeps_its_own_color_even_with_an_organization() -> None:
    # Real gap found by review (2026-10-02): organization was checked before
    # category with no carve-out, so a hypothetical city holiday/menu
    # document (not reachable today, but nothing structurally prevented it)
    # would get the right icon but the wrong border color.
    item = {
        "title": "No School",
        "detail": "Staff day",
        "date": "2026-09-29",
        "category": "holiday",
        "organization": "city_recreation",
    }
    html = _row_html(item, as_of=date(2026, 9, 9))

    # Checked on the --acamar-accent border specifically, not the whole
    # blob — the far-out urgency-gradient day-count text legitimately also
    # renders in this same green (_URGENCY_GREEN), for an unrelated reason.
    assert '--acamar-accent:#ec4899' in html  # holiday pink
    assert '--acamar-accent:#22c55e' not in html  # not the city-recreation green
