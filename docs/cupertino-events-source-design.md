# Cupertino Parks & Rec events — source design

> **Superseded in part, 2026-10-25.** The connector now scrapes the city's
> Event Calendar listing
> (`/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar`, paged
> via `?dlv_PR%20Events%20Listing%20-%20Custom=(pageindex=N)`) instead of
> the index page + each event's detail page described below. Why: the index
> scrape only followed `/Events/<slug>` links one level deep, but the real
> dated pages sit one level deeper (`/Events/McClellan-Events/<event>`,
> `/Events/Tree-Lighting/Tree-Lighting-Event`), so it stored 4 events while
> the calendar listed 8. The listing gives an explicit day/month/year (the
> year-inference logic in §4 is gone), a clean description (so the LLM
> summary-only enrichment is no longer used for this source), venue and
> link — no detail page is fetched. It runs from the household Mac, into
> both local Postgres and Cloud SQL, because the site's Akamai WAF 403s
> Google Cloud egress (confirmed 2026-10-24, every URL). Parser/connector
> docstrings and tests reflect the current design; the sections below are
> the original build's history.

Status: **built 2026-10-01**, on explicit "Execute" after this design was
reviewed. Four critical forks were reviewed and resolved with the user before
writing the original version of this doc; their answers are reflected
throughout. §10 covers what changed between "designed" and "built" — most
notably a real, confirmed robots.txt-matching bug found during the build that
wasn't anticipated in the original design at all.

## 0. Why this doc exists, and what changed from the original spec

The original ask was:

> Index URL: `https://www.cupertino.gov/Parks-Recreation/Events`
> Pattern: collect `<a href>` links under that page, filter for
> `/Parks-Recreation/Events/<event-name>` paths, then fetch each
> Date regex: `r'(Monday|Tuesday|...|Sunday),\s+\w+ \d+, \d{4}'`
> `external_id`: the URL slug (e.g. `pumpkin-pool-ooza`)

Before writing pseudocode against that spec as given, it was checked against the
**real site** — `robots.txt` and five real pages fetched live — the same
discipline `faria_events` and `manual_menu` were both built under ("inspect the
real page structure first, don't guess selectors"; "built against two real
samples, not guessed at"). Three things changed as a result:

1. **`robots.txt` disallows several individual event paths** that the index page
   links to. A naive "fetch every matching link" connector would violate it on
   a real chunk of events. See §2.
2. **The date regex needs its year made optional.** A real live page states the
   date as `"Friday, December 4"` — no year anywhere on the page. `\d{4}` as
   given would never match this page at all. See §4.
3. **Several events currently have no parseable date at all** (the page says
   "will return in [future year]," with no specific day). The design has to
   treat that as *skip this event this run*, not an error and not a fabricated
   date. See §4.

## 1. Resolved design decisions

| # | Decision | Resolution |
|---|---|---|
| 1 | New `Domain` enum value | **`Domain.CITY`** |
| 2 | New `Organization` enum value | **`Organization.CITY_RECREATION`** (new, deterministic — same precedent as `SCHOOL_CALENDAR`) |
| 3 | Shared robots/retry/throttle logic vs. duplicate | **Extract a shared `PoliteFetcher`**; refactor `FariaEventsConnector` to use it too |
| 4 | `documents.content` strategy | **Loose: trimmed real page prose**, not a templated Faria-style sentence |

**Open note, not yet asked — flag if you disagree:** `Category.EVENT` is
recommended over `Category.ACTIVITY` for these documents (§3 has the full
reasoning: every scraped item is a specific-dated one-off occurrence, which is
`EVENT`'s literal definition). Worth knowing before building: the Router's own
shipped prompt (`reasoning/nodes/router.py`, `_CATEGORY_GUIDE`) already names
*"Parks & Rec"* as its example for `Category.ACTIVITY` — written before this
source existed, evidently anticipating something like it. That wording was
probably imagining ongoing camp/program registrations rather than a one-off
public-events calendar, but it's a direct enough textual hit that it's called
out explicitly rather than silently overridden.

## 2. Real findings from live verification (2026-10-01)

**`robots.txt`** — `User-agent: *`, no `Crawl-delay`. The index page itself and
most individual events are allowed. Explicitly **disallowed**, confirmed from
the real file:

```
/Parks-Recreation/Events/50-Golf-Tournament/50-Golf-Tournament/*
/Parks-Recreation/Events/Big-Bunny-5K/*
/Parks-Recreation/Events/Holiday-Events/*
/Parks-Recreation/Events/Monster-Mash/*
/Parks-Recreation/Events/Pooch-Plunge/*
/Parks-Recreation/Events/Sponsorship-Opportunities
```

Disallowed paths aren't predictable from the event's title or from the index
page alone (e.g. "Tree Lighting" is a holiday event but its real URL —
`/Parks-Recreation/Events/Tree-Lighting` — is *not* nested under
`/Holiday-Events/` and so isn't covered by that rule). **This means the robots
check has to be a real per-URL `can_fetch()` call against each event's actual
URL, not a guess based on the event name, and not just the one check Faria does
against its single calendar URL.**

**The index page lists ~27 events but shows no dates, times, or locations for
any of them** — confirmed by direct inspection. Every event's schedule can only
be learned by fetching its own detail page. There is no way to pre-filter
"upcoming" vs. "past/off-season" events before fetching each one.

**Pagination — unverified, flag for whoever builds this:** the ~27 links found
are everything a single fetch of the index page surfaced. Whether the real page
paginates or lazy-loads more events below that isn't confirmed from here — view
the real page source (not just rendered text) before assuming 27 is the whole
list, same "don't guess, verify" discipline as everything else in this doc.

**A live, scheduled event's date can omit the year.** `Tree-Lighting`'s real
page states the date as literally `"Friday, December 4"` — month and day only.
`Fourth-of-July`'s page, by contrast, names the next instance as `"July 4,
2027"` (because the current year's has already passed and the next one is far
enough out that the site apparently felt the year was needed) — so the shape is
real but **inconsistent across pages**; the parser must handle a date with or
without a trailing year.

**Several events have no live date yet.** `Pumpkin-Pool-ooza`,
`Movies-in-the-Park`, and `Fourth-of-July` (as of 2026-10-01) all read "will
return in [a future year], details available [some months out]" — no specific
day anywhere on the page. These must *not* produce a document this run.

**Bold-text fragments on these pages are mostly not dates at all.** On
`Movies-in-the-Park`, the bold/emphasized fragments near the top are FAQ
headings and one-liners ("Movies start at 8 p.m.", "Can I bring my dog?"), not
a clean date/time/location triplet. The date regex is genuinely needed as a
*discriminator* among noisy bold text, exactly as the original spec intended —
but the tag actually carrying the date (`<strong>`, `<b>`, a heading, or
something else) was not confirmed from a markdown-converted fetch and **must be
confirmed from real HTML source** before writing the BeautifulSoup selector —
same rule `faria_events`' own docstring states ("inspect the real page
structure first, don't guess selectors").

## 3. Schema / enum changes

```python
# domain/enums.py

class Domain(StrEnum):
    SCHOOL = "school"
    DAYCARE = "daycare"
    GMAIL = "gmail"
    FINANCIAL = "financial"
    PRODUCT_MANUAL = "product_manual"
    CITY = "city"  # NEW — municipal/community sources; this is the first one


class Category(StrEnum):
    EVENT = "event"       # <- these documents use this (see "Open note" above)
    MENU = "menu"
    REMINDER = "reminder"
    ACTIVITY = "activity"
    HOLIDAY = "holiday"
    # no new value needed


class Organization(StrEnum):
    DISTRICT = "district"
    FEEF = "feef"
    CLASSROOM = "classroom"
    FARIA_ADMIN = "faria_admin"
    SCHOOL_CALENDAR = "school_calendar"
    CITY_RECREATION = "city_recreation"  # NEW — deterministic, same precedent as SCHOOL_CALENDAR
    OTHER = "other"
```

`db/schema.sql` needs **no column changes** — `sources.domain`/`category` and
`documents.domain`/`category`/`organization` are already free `TEXT`, just
update the inline comment on `sources.domain` (`'school', 'daycare', 'gmail',
'financial', 'product_manual'`) to add `'city'`, purely documentation.

`sources.school_id` stays `NOT NULL` and this new source's row still points at
the one real Faria `schools` row — not a new design decision, this is the
**existing precedent** (`gmail_school`/`gmail_menu` are domain `gmail`, not
`school`, and already do exactly this; single-household/single-school schema,
see CLAUDE.md). Flagged here so it isn't mistaken for an oversight.

### 3.1 `IngestionService._enrich` — the organization-assignment bug this would otherwise hit

**Real, already-logged bug this new source would walk straight into if
unfixed:** `ingestion/service.py`'s non-Gmail branch currently is:

```python
organization = Organization.SCHOOL_CALENDAR if doc.category == Category.EVENT else None
```

This checks `category` only, not which connector/domain produced the document —
it was already fixed once (2026-09-25) to stop mislabeling `manual_menu` as
`SCHOOL_CALENDAR`, by narrowing to `category == EVENT`. This new, *also*
`category == EVENT`, *also* non-Gmail source would immediately regress the
exact same bug: every Cupertino event would get stamped `SCHOOL_CALENDAR`,
which is simply wrong.

**Fix, generalized this time instead of patched a third time:** replace the
inline ternary with an explicit `(Domain, Category) -> Organization` mapping,
so the next non-Gmail source doesn't need a fourth special case:

```python
# ingestion/service.py

_DETERMINISTIC_ORGANIZATION: dict[tuple[Domain, Category], Organization] = {
    (Domain.SCHOOL, Category.EVENT): Organization.SCHOOL_CALENDAR,
    (Domain.CITY, Category.EVENT): Organization.CITY_RECREATION,
}

# inside _enrich's non-Gmail branch:
organization = _DETERMINISTIC_ORGANIZATION.get((doc.domain, doc.category))
```

Behaviourally identical to today for every existing source
(`manual_menu` still gets `None`, Faria still gets `SCHOOL_CALENDAR`) — this is
a refactor-for-extensibility, not a behavior change, so it needs no new test
beyond a regression test asserting the existing two sources' outcomes are
unchanged, plus one new test for the new mapping entry.

## 4. Date resolution — the core correctness-critical piece

```
FUNCTION extract_event_date(detail_page_text, candidate_bold_fragments, today) -> date | None:
    # Step 1: find the ONE fragment that's actually a date, among noisy FAQ text.
    DATE_PATTERN = regex(
        r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),'
        r'\s+([A-Z][a-z]+)\s+(\d{1,2})(?:,\s+(\d{4}))?\b'
    )
    # NOTE vs. original spec: the trailing ", \d{4}" is now OPTIONAL
    # (non-capturing group with `?`) — Tree-Lighting's real page has no year.

    match = first match of DATE_PATTERN across candidate_bold_fragments
            (fall back to the whole page text if no bold fragments matched —
             see the "confirm the real tag" flag in §2)
    IF no match:
        RETURN None   # "will return in <year>" pages land here — correct, not a bug

    weekday_name, month_name, day_num, year_str = match.groups()

    # Step 2: resolve the year when the page didn't state one.
    IF year_str is not None:
        candidate_date = date(int(year_str), month_name, day_num)
        IF candidate_date.weekday_name != weekday_name:
            LOG WARNING "stated weekday doesn't match stated date, skipping"
            RETURN None             # self-consistency check — never trust a
                                     # mismatched explicit date either
        RETURN candidate_date

    # No year stated (the Tree-Lighting shape). Try this year, then next year;
    # pick whichever (a) isn't already in the past and (b) actually falls on
    # the stated weekday. Same "weekday math as a confirming cross-check"
    # discipline ingestion/service.py already uses for Gmail narrative dates
    # (_resolve_relative_date), applied here to a *scraped*, not LLM, date.
    FOR candidate_year IN [today.year, today.year + 1]:
        candidate_date = date(candidate_year, month_name, day_num)
        IF candidate_date >= today AND candidate_date.weekday_name == weekday_name:
            RETURN candidate_date

    LOG WARNING "could not resolve a year matching the stated weekday"
    RETURN None   # skip rather than guess — same anti-fabrication posture as
                  # manual_menu's "PDF states a year but never a month" gotcha
```

Test this function in complete isolation (pure function, no HTTP, no
BeautifulSoup) with at least:

- `"Friday, December 4"`, `today=2026-10-01` → resolves to `2026-12-04` (it IS
  a Friday that year, and it's in the future).
- `"Friday, December 4"`, `today=2026-12-10` (past this year's occurrence) →
  resolves to `2027-12-04` *only if* Dec 4, 2027 is also a Friday — else
  `None`. **Confirm the real arithmetic before writing this test's expected
  value** — don't hand-wave a specific year/weekday pairing without checking
  a calendar.
- `"Saturday, July 4, 2027"` (year present, consistent) → `2027-07-04`.
- `"Tuesday, July 4, 2027"` (year present, but July 4 2027 is NOT a Tuesday —
  a deliberately inconsistent fixture) → `None`, logged, not silently trusted.
- No date-shaped text anywhere on the page (the real `Pumpkin-Pool-ooza`/
  `Movies-in-the-Park`/`Fourth-of-July` "will return in..." shape) → `None`.
- Multiple bold fragments, only one of which is a real date (the real
  `Movies-in-the-Park` FAQ shape) → the FAQ fragments are ignored, the date
  fragment (when present) is the one used.

## 5. Module layout

```
src/acamar/
  adapters/
    polite_http.py          # NEW — shared robots.txt + retry + throttle (decision #3)
  domain/enums.py            # + Domain.CITY, + Organization.CITY_RECREATION
  ingestion/service.py       # _enrich's organization mapping generalized (§3.1)
  sources/
    faria_events/
      connector.py           # REFACTORED to use adapters.polite_http.PoliteFetcher
    cupertino_events/        # NEW
      __init__.py
      connector.py
      parser.py
```

### 5.1 `adapters/polite_http.py` — extracted from `FariaEventsConnector`

Pulls out everything in today's `FariaEventsConnector` that isn't
Faria-specific: the robots.txt fetch-and-cache, `can_fetch()` checks,
exponential-backoff retry, and inter-request throttling. Faria-specific bits
(the calendar URL, parsing) stay in `FariaEventsConnector`.

```
CLASS PoliteFetcher:
    """Robots.txt-aware, throttled, retrying HTTP GET. Shared by every scraper
    connector (faria_events, cupertino_events, and whatever comes next)."""

    CONSTRUCTOR(user_agent, min_request_interval, max_attempts, client=None):
        store args; _robots_cache: dict[origin -> RobotFileParser | None] = {}
        # keyed by origin (scheme+host), NOT one single flag like Faria's
        # `_robots_checked` bool — this fetcher now serves requests to
        # potentially more than one origin over its lifetime (today: just
        # cupertino.gov across many paths; faria_events only ever has one
        # origin so this is a strict generalization, not a behavior change
        # for it).

    ASYNC METHOD get(url) -> httpx.Response:
        RAISES ConnectorError if robots.txt disallows `url`, or after
        max_attempts failed tries.
        """
        rules = AWAIT _rules_for_origin(url)
        IF rules is not None AND NOT rules.can_fetch(self.user_agent, url):
            RAISE ConnectorError(f"robots.txt disallows {url}")
        AWAIT _throttle()                 # same max(floor, crawl-delay) logic
        RETURN AWAIT _get_with_retry(url) # same backoff logic as today

    ASYNC METHOD _rules_for_origin(url) -> RobotFileParser | None:
        origin = scheme+host of url
        IF origin not in self._robots_cache:
            fetch {origin}/robots.txt once, parse it (or cache None if
            unreachable/404 — "fail open", same as today)
            self._robots_cache[origin] = parsed_rules_or_none
        RETURN self._robots_cache[origin]
```

`FariaEventsConnector.fetch()` becomes: construct one `PoliteFetcher` (or
accept one injected, for tests), call `fetcher.get(self._calendar_url)` once,
parse the response. **Its own test suite
(`tests/unit/test_faria_events_connector.py`) should not need new test
*cases* — same robots-disallow / missing-robots / unreachable-robots /
crawl-delay behaviors are being preserved, not changed — but every test that
currently mocks `FariaEventsConnector`'s internal robots/retry behavior
directly will need updating to go through `PoliteFetcher` instead.** This is
the real cost of decision #3 — budget time for touching this file's tests, not
just the connector itself.

### 5.2 `sources/cupertino_events/parser.py`

Two pure functions, no HTTP — mirrors `faria_events/parser.py`'s
HTML-in/`RawDocument`-out shape exactly.

```
FUNCTION parse_index_html(html) -> list[(title: str, url: str)]:
    """Every /Parks-Recreation/Events/<slug> link on the index page, with its
    link text as the title. Raises IndexStructureError if the page doesn't
    look like the real events index at all (mirrors CalendarStructureError —
    a site redesign must surface as a failed run, not a silently empty list).
    """
    soup = BeautifulSoup(html, "lxml")
    IF no <a href> matching "/Parks-Recreation/Events/" pattern found at all:
        RAISE IndexStructureError(...)
    FOR each <a> whose href matches r'/Parks-Recreation/Events/[^/]+/?$':
        (exclude the index page's own URL and any obviously-non-event
         utility link — confirm against real HTML whether any exist; none
         were evident from the rendered content checked for this doc)
        yield (link_text.strip(), absolute_url)
    # de-duplicate by url (same event sometimes linked twice in real sites —
    # confirmed true of Faria's markup; verify for this one too)


FUNCTION parse_event_detail_html(html, *, url, title) -> RawDocument | None:
    """None if no resolvable event_date is found (see extract_event_date, §4) —
    this is the expected, common case for an off-season event page, not an
    error.
    """
    soup = BeautifulSoup(html, "lxml")
    bold_fragments = [t.get_text(strip=True) for t in soup.select(<CONFIRM: "strong" | "b" | other>)]
    event_date = extract_event_date(soup.get_text(), bold_fragments, today=...)
       # NOTE: `today` must come from an injected Clock, not datetime.now() —
       # match this codebase's existing Clock-port convention
       # (adapters/clock.py) rather than a module-level time call.
    IF event_date is None:
        RETURN None

    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()  # "pumpkin-pool-ooza"
    body_text = trimmed, whitespace-collapsed main-content prose from the page
                (decision #4: loose, not a templated sentence) — exclude
                obvious nav/footer chrome; cap length (confirm a reasonable
                cap, e.g. a few hundred words, against a few real pages so
                content stays embedder-friendly without truncating the actual
                date/time/venue text that matters)

    RETURN RawDocument(
        external_id = slug,
        title = title,              # from the INDEX page's own link text —
                                     # already clean, confirmed from real
                                     # fetch; simpler and more reliable than
                                     # re-deriving it from the detail page's
                                     # own <title>/<h1> (likely carries a
                                     # site-wide suffix)
        content = f"{title} — {body_text}",
        domain = Domain.CITY,
        category = Category.EVENT,  # see "Open note" in §1
        event_date = event_date,
        student_ref = None,         # whole-household; not kid-specific —
                                     # same default as Faria's own events
    )
```

**`external_id` is stable across re-runs of the *same scheduled instance*,
exactly as intended — but also stable *across different years* of a recurring
event that reuses one evergreen URL** (confirmed real: `Tree-Lighting`'s page
is reused year to year, not re-issued at a new URL). Concretely: next
December, re-scraping `Tree-Lighting` will **overwrite** this year's row in
place (same `external_id = "tree-lighting"`) rather than insert a second row.
This is very likely exactly what's wanted for a forward-looking dashboard
(nobody needs last year's Tree Lighting once it's passed — same as how old
menu rows simply age out of the dashboard's date window), but it's a
different dedup granularity than `faria_events`' own `data-occur-id` (which
encodes the actual timestamp, so recurring events there naturally get a fresh
id each year). Flagged so this is an accepted, understood behavior rather than
a surprise found later.

### 5.3 `sources/cupertino_events/connector.py`

```
CLASS CupertinoEventsConnector:
    name = "cupertino_events"

    CONSTRUCTOR(index_url=None, fetcher: PoliteFetcher | None = None, clock: Clock | None = None):
        settings = get_settings()
        self._index_url = index_url or settings.cupertino_events_index_url
        self._fetcher = fetcher or PoliteFetcher(
            user_agent=settings.scraper_user_agent,
            min_request_interval=settings.scraper_min_request_interval_seconds,
            max_attempts=settings.scraper_max_attempts,
        )
        self._clock = clock or SystemClock()

    ASYNC METHOD fetch() -> list[RawDocument]:
        TRY:
            index_html = AWAIT self._fetcher.get(self._index_url)
        EXCEPT <fetcher's error>:
            RAISE ConnectorError(f"{self.name}: fetching the events index failed: ...")

        TRY:
            candidates = parse_index_html(index_html)   # list[(title, url)]
        EXCEPT IndexStructureError as exc:
            RAISE ConnectorError(f"{self.name}: parsing the events index failed: {exc}")

        documents = []
        FOR (title, url) IN candidates:
            # Per-event fault isolation — a robots-disallowed event, a 404, a
            # transient network error, or an unparseable detail page on ONE
            # event must never abort the other ~26. This is the exact lesson
            # already learned once in this codebase (P1.75 item 31: "one bad
            # staged menu PDF permanently breaks ingestion for every
            # meal/month" — same shape of bug, prevented here from the start
            # instead of fixed after the fact).
            TRY:
                detail_html = AWAIT self._fetcher.get(url)
            EXCEPT ConnectorError as exc:   # robots-disallowed OR fetch failure
                LOG INFO/WARNING(f"skipping {url}: {exc}")
                CONTINUE
            TRY:
                doc = parse_event_detail_html(detail_html, url=url, title=title)
            EXCEPT Exception as exc:
                LOG WARNING(f"skipping {url}: failed to parse: {exc}")
                CONTINUE
            IF doc is not None:
                documents.append(doc)
        RETURN documents
```

**No concurrency** — events are fetched sequentially, each gated by
`PoliteFetcher`'s own throttle. For ~27 candidate events at the default 5s
floor, that's a **multi-minute ingestion run** (sequential throttle delay
alone, before real request latency) — much slower than Faria's single-request
scrape. This is an intentional trade-off (politeness over speed, same ethos as
every other scraper in this codebase), not a bug, but worth knowing before
assuming ingestion stays fast: this source's run time will dominate a shared
cron tick if ever run back-to-back with other sources, so confirm the 6x/day
cron job either runs sources concurrently already or budget accordingly.

## 6. Pipeline wiring

```python
# ingestion/connector_factory.py

_REGISTRY: dict[tuple[Domain, Category], _Builder] = {
    ...,  # existing entries unchanged
    (Domain.CITY, Category.EVENT): lambda source: CupertinoEventsConnector(
        index_url=source.config.get("index_url"),
    ),
}
```

`IngestionService._should_reconcile` needs **no change** — it already defaults
to `True` (reconcile) unless `domain == Domain.GMAIL` or the source opts out
via `config["reconcile"] = False`. `CupertinoEventsConnector.fetch()` re-lists
the *entire current* set of linked events every run (same full-reassertion
shape as Faria's month scrape), so the default `reconcile=True` is correct
as-is: an event the city removes from the index naturally gets soft-deleted on
the next run, same as a cancelled Faria "No School" day.

## 7. Config / settings / seed

```python
# config.py, alongside faria_calendar_url
cupertino_events_index_url: str = "https://www.cupertino.gov/Parks-Recreation/Events"
```

```bash
# .env.example — no new secret, just documents the default is overridable
# CUPERTINO_EVENTS_INDEX_URL=
```

```python
# scripts/seed.py — mirrors _upsert_events_source exactly, new function
# _upsert_cupertino_events_source(conn, school_id) inserting/updating
# (school_id, Domain.CITY, Category.EVENT, config={}) — no special config
# keys needed (reconcile defaults True, which is correct here).
```

## 8. Test plan

**`tests/unit/test_cupertino_events_parser.py`** (pure, no HTTP — mirrors
`test_faria_events_parser.py` and `test_manual_menu_parser.py`'s "real
captured fixture" discipline):

- `parse_index_html` against a **real saved copy** of the index page's HTML
  (`tests/fixtures/cupertino_events/index.html`) — asserts the known real
  event count/titles/slugs found during this design's own verification, and
  that disallowed-by-robots events (`Monster-Mash`, `Pooch-Plunge`, etc.) are
  *still returned by the parser* (filtering by robots happens in the
  connector, not the parser — keep that separation explicit and tested).
- `parse_index_html` on HTML with no matching links at all → raises
  `IndexStructureError`.
- `parse_event_detail_html` against a **real saved copy** of a page with a
  concrete date (`tree_lighting.html`) → returns a `RawDocument` with the
  correct resolved `event_date`, `domain=CITY`, `category=EVENT`.
- `parse_event_detail_html` against a **real saved copy** of an off-season
  "will return in [year]" page (`pumpkin_pool_ooza.html` or
  `movies_in_the_park.html`) → returns `None`.
- `extract_event_date` unit tests — the full matrix in §4, independent of any
  HTML parsing.
- `external_id` is the lowercased URL slug, stable byte-for-byte across two
  parses of the same fixture.

**`tests/unit/test_cupertino_events_connector.py`** (HTTP mocked via `respx`,
mirrors `test_faria_events_connector.py`'s taxonomy, plus new cases this
connector specifically needs):

- Happy path: index + N allowed detail pages all mocked → N documents
  returned, `domain` tagged `city` on all of them.
- **New vs. Faria: a mix of allowed and robots-disallowed event URLs** →
  disallowed ones are silently skipped, allowed ones still come back — this is
  the one genuinely new behavior this connector has that Faria's never needed.
- **New vs. Faria: one event's detail-page fetch throws / 500s** → that one
  event is skipped, the rest of the run still succeeds (fault isolation,
  §5.3's whole reason for existing) — assert on `caplog` that it's logged, not
  silent.
- **New vs. Faria: one event's detail page parses but has no resolvable date**
  → silently excluded from the result, no error.
- Index page itself fails/disallowed → raises `ConnectorError` (this IS a
  whole-run failure, unlike a single event).
- Same crawl-delay / missing-robots-is-allowed / unreachable-robots-fails-open
  behaviors as Faria's suite, exercised through `PoliteFetcher` now instead of
  connector-internal code.

**`tests/contract/test_source_connector_contract.py`** — add
`TestCupertinoEventsConnectorContract(SourceConnectorContract)` alongside the
existing Faria one, wiring `working_connector` / `empty_connector` /
`failing_connector` against respx-mocked responses, per the existing pattern.

**`tests/unit/test_polite_fetcher.py`** (NEW file) — the robots/retry/throttle
test cases that currently live inside `test_faria_events_connector.py` move
here and get exercised directly against `PoliteFetcher`, **plus** a new case
Faria never needed: `PoliteFetcher` serving two different origins in the same
test (confirms the per-origin robots cache, §5.1, doesn't crosstalk) — not
exercised by either real connector yet but genuinely part of the new
abstraction's contract.

**`tests/unit/test_faria_events_connector.py`** — updated (not rewritten) to
construct/inject a `PoliteFetcher` instead of mocking internals directly; same
assertions, different setup.

**`tests/unit/test_ingestion_service.py`** — one new test for
`_DETERMINISTIC_ORGANIZATION`'s new `(Domain.CITY, Category.EVENT)` entry,
plus a regression test confirming the existing `(Domain.SCHOOL, Category.EVENT)`
→ `SCHOOL_CALENDAR` and `(Domain.SCHOOL, Category.MENU)` → `None` outcomes are
unchanged by the refactor (§3.1).

**Fixtures to capture before writing any of the above** (real files, not
synthesized — same rule every other fixture in this repo follows):

```
tests/fixtures/cupertino_events/
  index.html                    # the real /Parks-Recreation/Events page
  tree_lighting.html            # a real page WITH a resolvable date (no year)
  fourth_of_july.html           # a real page WITH a year-bearing "next instance" date
  pumpkin_pool_ooza.html        # a real page with NO resolvable date ("will return...")
```

## 9. Explicitly out of scope for this change

- Concurrency / parallel fetching of event pages (politeness > speed, §5.3).
- Any attempt to extract a structured location/venue field — decision #4
  (loose content) means location text just rides along inside the trimmed
  page prose, not a separate column.
- Guessing a coarse `event_date` (e.g. "July 2027" → `2027-07-01`) for an
  off-season page — explicitly rejected, §2/§4, same anti-fabrication stance
  as the rest of this codebase.
- Pagination handling beyond what's on the first fetch of the index page,
  until someone actually confirms (from real HTML) whether pagination exists.
- A new `Category` value for this source — `EVENT` was used, per §1's
  recommendation, taken as decided when the user said "Execute" without
  overriding it. `ACTIVITY` is still a one-line change
  (`src/acamar/sources/cupertino_events/parser.py`'s `Category.EVENT` ->
  `Category.ACTIVITY`, plus `_DETERMINISTIC_ORGANIZATION`'s key in
  `ingestion/service.py`) if that turns out to be the better fit.

## 10. What changed between "designed" and "built" (2026-10-01)

Built the same day, on "Execute." Everything in §1–§9 above was followed as
written, with two real additions found only once actual code was written and
tested against real-shaped input — exactly the kind of gap a design pass on
paper can't surface, only building and testing can.

**A second real bug, bigger than anything anticipated in the original
design**: `urllib.robotparser` (Python's stdlib, and what `FariaEventsConnector`
was already using) does not implement the `*`/`$` wildcard extension real
robots.txt files use. Confirmed directly while writing `CupertinoEventsConnector`'s
own tests: parsing `Disallow: /Parks-Recreation/Events/Monster-Mash*` (the
real site's own verbatim rule shape) and then checking a real Monster-Mash
URL against it returned "allowed" — wrong. Tracing it down: the stdlib
percent-encodes a literal `*` in a `Disallow` line to `%2A` when storing it,
so that rule could never have matched any real request path at all, making
every one of cupertino.gov's five wildcard-based disallow rules a silent
no-op. This wasn't a cupertino_events-only problem once found — it lives in
the *shared* `PoliteFetcher`, so `FariaEventsConnector` inherited the same
latent gap (dormant only because Faria's own robots.txt happens not to use
wildcards on the one path it fetches). **Fixed, not worked around**: wrote
`adapters/robots.py`, a small robots.txt parser with real `*`/`$` support,
and switched `PoliteFetcher` to it — `urllib.robotparser` is no longer used
anywhere in this codebase. 13 new tests (`test_robots.py`) pin down the
specific confirmed failure plus standard robots.txt precedence rules (longest
match wins, agent-specific groups override `*`, etc.) so this can't quietly
regress.

**Fixture fidelity, stated plainly**: direct `curl`/`httpx` access to
`cupertino.gov` from this build environment was blocked by the site's own
Akamai edge WAF (403, confirmed, regardless of User-Agent — now logged in
CLAUDE.md's "Known gotchas"). Real page *content* was still obtained and
verified through a different fetching path (preserving markdown emphasis
markers specifically to confirm `<strong>`/`<b>`-wrapped date/time/venue
fragments), but the checked-in test fixtures
(`tests/fixtures/cupertino_events/*.html`) are hand-reconstructed from that
verified content, not raw `curl` captures the way `faria_events`' and
`manual_menu`'s own fixtures are — each fixture file says so in its own
header comment. §8's original fixture list named `fourth_of_july.html` as the
"year-bearing date" example; built content showed its actual date text is a
**plain, non-bold paragraph** (not inside `<strong>`/`<b>`), so it never
produces a resolvable date at all — same off-season shape as
`movies_in_the_park.html`, which replaced it as a richer, better-verified
fixture for that case (it also confirms FAQ bold text isn't mistaken for a
date, the thing the regex exists to discriminate). No fixture with a real,
confirmed year-bearing `<strong>` date was found during this build — the
`extract_event_date` unit tests covering that shape use hand-picked,
calendar-verified dates (e.g. "Saturday, July 4, 2026" — confirmed a real
Saturday via `date.strftime`), not a captured page.

**Final state (end of the initial build)**: 405 tests passing (was 349),
ruff/mypy clean. Not yet run against live Postgres/a real ingestion cycle.

## 11. Round two (2026-10-02): real run, dashboard integration, a real outage

The initial build above was then actually run for real, and extended with
dashboard-facing features the first design pass hadn't scoped — all per the
user's own direct requests, not assumed.

**Real run #1** (seeded + `scripts.run_ingestion` against live Postgres/
Ollama/cupertino.gov): succeeded — 4 of ~30 candidate links resolved to a
real date (Big Bunny 5K, Teen Resource Fair, Na Leo Mokupuni ma Cupertino,
Monster Mash), `Sponsorship-Opportunities` correctly skipped by the robots
check, 178s runtime. Confirmed direct network access to cupertino.gov
*succeeded* this time, correcting §2/§10's WAF-block finding to "confirmed
inconsistent," not reliably blocked (now reflected in CLAUDE.md). Two new
real findings, flagged not fixed: individual events redirecting to entirely
different origins (`sfshakes.org`, `anc.apm.activecommunities.com`) that the
robots check never validates (it only ever checks the *original*
cupertino.gov URL, not where a redirect actually lands); and two non-event
utility links (`Parks-and-Recreation-Event-Calendar`, `Festival-Information`)
matching the same URL shape as a real event.

**"Why is this junk showing up" — a real user-reported bug, root-caused
precisely**: `services/dashboard.py`'s `_detail()` prefers `doc.summary` but
falls back to splitting raw `content` on `" — "`, a fallback shaped for
Faria's short, clean scraped content. Since City documents never got
`summary` populated, the full scraped paragraph (registration boilerplate,
image filenames and all) rendered directly on the household dashboard.

**Fixed via a design fork checked with the user before building** (three
options: full Gmail-style enrichment / a narrow summary-only LLM call /
scraper-side cleanup with no LLM — user picked the narrow option, reasoning:
the parser's own weekday-cross-validated `event_date` is more reliable than
an LLM re-read, so only the human-readable text needed synthesis):

- New `ports.enricher.ContentSummary` + `Enricher.summarize()` — its own
  prompt, written for scraped-webpage noise specifically (not email),
  returning only `headline`/`summary`.
- `IngestionService._enrich` gained a third treatment,
  `_SUMMARY_ONLY_DOMAINS = {Domain.CITY}` via new `_summarize_only`:
  `event_date`/`category`/`organization` always stay exactly what the
  parser set; only `headline`/`summary` come from the LLM. Same unchanged-
  content reuse-cache and already-past-event skip as Gmail's branch, so a
  daily re-scrape of an unchanged page doesn't re-pay for an LLM call.
- 21 new tests (`test_ollama_enricher.py`, `test_ingestion_service.py`).

**Also built the same round, all by explicit request:**

- **Utility-link exclusion**: the two confirmed-real non-event slugs from
  real run #1 are now an explicit denylist in `parser.py` (`_NON_EVENT_
  SLUGS`) — no structural way exists to tell them apart from a real event
  by URL shape alone, so a denylist, not a heuristic.
- **`documents.url`**: new generic, nullable column (any source may set it,
  not city-only) threaded through `schema.sql` → `RawDocument`/`Document`
  → the Postgres repo → `DashboardItem` → `streamlit_app.py`'s new
  `_linked()` helper (a dotted-underline link inheriting the card's own
  text color, opens in a new tab). The connector already captures the
  *specific* event page per document, not just the index — when asked
  whether a parent-site link would be an acceptable fallback, confirmed
  that fallback isn't needed here.
- **Dashboard filter + styling**: `st.pills` (checked it actually exists in
  the pinned `streamlit==1.63.0` before using it) for "All / 🟠 School /
  🟢 Parks & City", filtering on `organization == "city_recreation"` — no
  new `DashboardItem.domain` field needed. New `_ORGANIZATION_COLOR`
  override (`#22c55e`, matching the existing `_URGENCY_GREEN` token) so a
  city `EVENT` no longer shares Faria's plain-event blue border; new 🌳
  entry in `_TYPE_ICON`.

**A real outage, from the `url` column migration being deferred**: adding
`url` to `schema.sql` and the shared `_UPSERT`/`_COLUMNS` SQL without
migrating the *live* database in the same step took down `GET /dashboard`
entirely (every read goes through those shared columns) — a real user-
reported 500, not just a theoretical risk, and a bigger blast radius than
"the next ingestion write fails" (what was originally communicated). Fixed
live, by explicit request once the user said they were fully blocked:
`ALTER TABLE documents ADD COLUMN url TEXT` applied directly, confirmed via
`information_schema.columns`, `GET /dashboard` re-checked at 200. Second
finding from the same incident: the column existing doesn't retroactively
populate it (or `summary`) on already-ingested rows — `scripts.run_ingestion`
had to be run a second time for real before `url`/the new clean summaries
actually appeared anywhere, confirmed via the live `/dashboard` response
(real synthesized text: *"Join in for a Halloween event at the Quinlan
Community Center, featuring games, trick-or-treating, craft-making, and
pumpkin decorating..."*, headline rewritten to "Spooky Fun for Kids"). See
CLAUDE.md's gotchas and backlog item 17 for the durable lesson.

**Status at the end of round two**: a 5-dimension adversarial-review
workflow (correctness; dashboard UX/escaping; schema/migration completeness;
the new `summarize()` prompt's injection surface; cockpit/cron) was launched
against the full feature as it now stands — not yet returned. Confirmed
findings will be folded in as a further addendum once it reports.
