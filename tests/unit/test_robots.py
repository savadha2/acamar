"""adapters.robots — the `*`/`$`-wildcard-aware robots.txt matcher.

Written 2026-10-01 specifically because `urllib.robotparser` was found NOT
to implement this: a `Disallow` line ending in `*` (cupertino.gov's real,
verbatim shape for every one of its per-event disallow rules) is silently a
complete no-op under the stdlib parser — confirmed directly, not assumed
(see this module's own docstring and docs/cupertino-events-source-design.md).
These tests pin down the behavior that regression would otherwise reopen.
"""

from __future__ import annotations

from acamar.adapters.robots import parse_robots_txt

UA = "ACAMAR/test"


def test_plain_prefix_disallow_still_works_like_before() -> None:
    # The simple, no-wildcard shape faria_events' own robots.txt uses —
    # must keep working exactly as it did under urllib.robotparser.
    rules = parse_robots_txt("User-agent: *\nDisallow: /about-us/\n", user_agent=UA)

    assert rules.can_fetch("/about-us/school-calendar") is False
    assert rules.can_fetch("/other-page") is True


def test_trailing_wildcard_disallow_blocks_the_whole_subtree() -> None:
    # The real, confirmed-live shape: cupertino.gov disallows
    # /Parks-Recreation/Events/Monster-Mash* — urllib.robotparser would
    # percent-encode the literal "*" and never match any real path at all.
    rules = parse_robots_txt(
        "User-agent: *\nDisallow: /Parks-Recreation/Events/Monster-Mash*\n", user_agent=UA
    )

    assert rules.can_fetch("/Parks-Recreation/Events/Monster-Mash") is False
    assert rules.can_fetch("/Parks-Recreation/Events/Monster-Mash/registration") is False
    assert rules.can_fetch("/Parks-Recreation/Events/Tree-Lighting") is True


def test_mid_pattern_wildcard_matches_any_sequence() -> None:
    rules = parse_robots_txt("User-agent: *\nDisallow: /a*z\n", user_agent=UA)

    assert rules.can_fetch("/a-anything-in-between-z") is False
    assert rules.can_fetch("/az") is False  # `*` matches zero characters too
    assert rules.can_fetch("/a-but-no-trailing-letter") is True


def test_trailing_dollar_anchors_the_end_of_path() -> None:
    rules = parse_robots_txt("User-agent: *\nDisallow: /exact$\n", user_agent=UA)

    assert rules.can_fetch("/exact") is False
    assert rules.can_fetch("/exact-but-longer") is True


def test_longest_matching_rule_wins_allow_can_override_disallow() -> None:
    # Standard robots.txt precedence: specificity, not file order. A
    # narrower Allow carves an exception out of a broader Disallow.
    rules = parse_robots_txt(
        "User-agent: *\nDisallow: /private/\nAllow: /private/public-page\n", user_agent=UA
    )

    assert rules.can_fetch("/private/secret") is False
    assert rules.can_fetch("/private/public-page") is True


def test_no_matching_rule_is_allowed() -> None:
    rules = parse_robots_txt("User-agent: *\nDisallow: /blocked/\n", user_agent=UA)

    assert rules.can_fetch("/totally/unrelated") is True


def test_empty_robots_txt_allows_everything() -> None:
    rules = parse_robots_txt("", user_agent=UA)

    assert rules.can_fetch("/anything") is True


def test_crawl_delay_is_parsed() -> None:
    rules = parse_robots_txt("User-agent: *\nCrawl-delay: 20\n", user_agent=UA)

    assert rules.crawl_delay == 20.0


def test_missing_crawl_delay_is_none() -> None:
    rules = parse_robots_txt("User-agent: *\nDisallow:\n", user_agent=UA)

    assert rules.crawl_delay is None


def test_malformed_crawl_delay_is_ignored_not_a_crash() -> None:
    rules = parse_robots_txt("User-agent: *\nCrawl-delay: soon\n", user_agent=UA)

    assert rules.crawl_delay is None


def test_comments_and_blank_lines_are_ignored() -> None:
    text = "# this is a comment\nUser-agent: *\n\nDisallow: /blocked/  # inline note\n"
    rules = parse_robots_txt(text, user_agent=UA)

    assert rules.can_fetch("/blocked/x") is False


def test_falls_back_to_the_wildcard_group_when_no_agent_specific_group_exists() -> None:
    rules = parse_robots_txt("User-agent: *\nDisallow: /blocked/\n", user_agent="SomeOtherBot/1")

    assert rules.can_fetch("/blocked/x") is False


def test_agent_specific_group_is_used_over_the_wildcard_one() -> None:
    text = (
        "User-agent: *\nDisallow: /for-everyone/\n"
        "\n"
        "User-agent: ACAMAR\nDisallow: /only-for-acamar/\n"
    )
    rules = parse_robots_txt(text, user_agent="ACAMAR")

    # The ACAMAR-specific group is used wholesale instead of the `*` group —
    # it does NOT see /for-everyone/ at all, matching real crawler semantics
    # (an agent-specific group replaces the wildcard one, not merges with it).
    assert rules.can_fetch("/only-for-acamar/x") is False
    assert rules.can_fetch("/for-everyone/x") is True


def test_explicit_empty_agent_group_means_nothing_disallowed_not_fall_through() -> None:
    # Real bug found by review (2026-10-02): `User-agent: ACAMAR` with zero
    # directives under it is a legitimate real-world idiom meaning "nothing
    # restricted for you specifically" — it must NOT be conflated with "no
    # agent-specific group exists" and silently inherit the (here, much
    # stricter) wildcard group instead.
    text = "User-agent: *\nDisallow: /\n\nUser-agent: ACAMAR\n"
    rules = parse_robots_txt(text, user_agent="ACAMAR")

    assert rules.can_fetch("/anything") is True
