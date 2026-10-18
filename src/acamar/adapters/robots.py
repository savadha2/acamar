"""A small robots.txt parser with real `*`/`$` wildcard support.

**Why this exists instead of `urllib.robotparser`** (confirmed, not assumed,
while building `cupertino_events` — see
docs/cupertino-events-source-design.md): the stdlib's `RobotFileParser` does
not implement the `*` (match any sequence) / `$` (end of path) wildcard
extension every modern crawler respects as the de facto standard. Worse than
simply ignoring `*` — it percent-encodes a literal `*` in a `Disallow` line to
`%2A` when storing it, so a rule like `Disallow: /Events/Monster-Mash/*`
(cupertino.gov's own real robots.txt, verbatim) never matches *any* real
request path, silently making that rule a complete no-op:

    >>> from urllib.robotparser import RobotFileParser
    >>> rp = RobotFileParser()
    >>> rp.parse(["User-agent: *", "Disallow: /Events/Monster-Mash/*"])
    >>> rp.can_fetch("x", "https://e.test/Events/Monster-Mash/registration")
    True  # wrong — the real site disallows this

This parser only handles what this project's two real scraped sites actually
use: a single applicable rule group (confirmed: neither `faria.cusdk8.org`
nor `cupertino.gov` has agent-specific rules, only `User-agent: *`), plain
`Disallow`/`Allow`/`Crawl-delay` directives, and `*`/trailing-`$` wildcards.
It is not a general-purpose robots.txt implementation — it doesn't need to be.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field


@dataclass
class RobotsRules:
    """Parsed rules for one user-agent group."""

    rules: list[tuple[str, bool]] = field(default_factory=list)
    """(pattern, allowed) pairs, in file order."""
    crawl_delay: float | None = None

    def can_fetch(self, path: str) -> bool:
        """True unless the *longest* matching rule is a Disallow.

        Standard robots.txt precedence is by specificity, not by which rule
        was written first: among every rule whose pattern matches `path`,
        the one with the longest pattern string wins — this is what lets a
        narrower `Allow` carve an exception out of a broader `Disallow` (or
        vice versa). A `path` matching no rule at all is allowed.
        """
        best: tuple[int, bool] | None = None
        for pattern, allowed in self.rules:
            if _pattern_matches(pattern, path) and (best is None or len(pattern) > best[0]):
                best = (len(pattern), allowed)
        return best is None or best[1]


def parse_robots_txt(text: str, *, user_agent: str) -> RobotsRules:
    """Parse `text` for the group matching `user_agent`, falling back to `*`.

    Found by review (2026-10-02): a truthy `or` here would conflate "no
    agent-specific group at all" with "an explicit, empty agent-specific
    group" (a real, legitimate robots.txt idiom meaning "nothing
    restricted for you specifically") — both are `[]`-shaped, but only the
    first should fall back to `*`. `in` + indexing (not `.get(..., [])`)
    keeps an explicit empty group empty rather than silently inheriting a
    stricter wildcard group.
    """
    groups = _group_lines(text)
    agent = user_agent.lower()
    group = groups[agent] if agent in groups else groups.get("*", [])
    rules: list[tuple[str, bool]] = []
    crawl_delay: float | None = None
    for directive, value in group:
        if directive == "disallow" and value:
            rules.append((value, False))
        elif directive == "allow" and value:
            rules.append((value, True))
        elif directive == "crawl-delay":
            # malformed value -> no delay asserted, not a crash
            with contextlib.suppress(ValueError):
                crawl_delay = float(value)
    return RobotsRules(rules=rules, crawl_delay=crawl_delay)


def _group_lines(text: str) -> dict[str, list[tuple[str, str]]]:
    """Map lowercased user-agent token -> its (directive, value) lines.

    Groups are separated by `User-agent:` lines per the real grouping rule
    (consecutive `User-agent:` lines before any directive share one group;
    a `User-agent:` line appearing *after* directives starts a new group) —
    not just "split on blank lines," which isn't a reliable separator in
    real-world robots.txt files.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    current_agents: list[str] = []
    seen_directive_since_ua = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if seen_directive_since_ua:
                current_agents = []
                seen_directive_since_ua = False
            current_agents.append(value.lower())
            groups.setdefault(value.lower(), [])
            continue
        if not current_agents:
            continue  # directive before any User-agent line -> ignore
        seen_directive_since_ua = True
        for agent in current_agents:
            groups[agent].append((directive, value))
    return groups


def _pattern_matches(pattern: str, path: str) -> bool:
    """True if `path` matches a robots.txt `pattern` (`*` and trailing `$` supported)."""
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    if anchored_end:
        regex += r"\Z"
    return re.match(regex, path) is not None
