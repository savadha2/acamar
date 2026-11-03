"""Strip people's names, email addresses and phone numbers out of text.

Backlog item 51 (2026-11-02, user decision): Gmail-derived text is redacted
at ingestion, *before* the LLM sees it and before anything is stored — names
never reach the database, the embeddings, or the summaries. Placeholders are
bracketed (`[name]`, `[email]`, `[phone]`) so a reader can tell something was
deliberately hidden.

Detection is "option B": a titled-name pattern (Mrs. X, Dr. Y, …) plus
spaCy's medium English NER model (`en_core_web_md`, ~40 MB) for PERSON
entities. Medium, not small: on the real false positives found in the
household's mail (Disaster Drill, Passcode, Teacher Grants, Mac, …) the
small model hid 7 of 11 non-names and missed a name; medium hid 0 of 11 and
caught all 8 test names (2026-11-02). The model catches first names,
full names and signature lines the pattern can't. Nothing leaves the machine;
the model runs locally. Neither is perfect (a name can slip through, a
non-name can be hidden); the allowlist below exists because the real data
hit exactly those cases: "Martin Luther King Jr. Day" is tagged PERSON by the
model, and "Parent Teacher Conference" looks like a titled name to a naive
pattern. Revisit the approach if it causes real fallout (user's call).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from spacy.language import Language

NAME = "[name]"
EMAIL = "[email]"
PHONE = "[phone]"

# Backslash allowed in the local part: markdown escapes ("Kim\\_Doe@…") would
# otherwise leave the name half of the address behind.
_EMAIL_PATTERN = re.compile(r"[\w.+\\-]+@[\w-]+(?:\.[\w-]+)+")
# A name right before an angle-bracketed address — reply headers ("On Tue,
# Jane Doe <jane@x.org> wrote:") and From lines. The model misses many names
# here; the shape alone is reliable.
_NAME_WORD = r"\b(?!(?:AM|PM)\b)[A-Z][\w'\u2019-]*\.?"  # initials ok; never "PM"
_NAME_BEFORE_EMAIL_PATTERN = re.compile(
    rf"({_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}})\s*<\s*[\w.+\\-]+@"
)
_PHONE_PATTERN = re.compile(
    r"(?<![\w-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"
)
# A title and ONE capitalized word: "Mrs. Chalmers". Not two words — "Mrs.
# Chalmers Room 20" must not swallow "Room"; full names ("Ms. Jane Doe") are
# covered by the NER spans merging with this one.
_TITLED_NAME_PATTERN = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Miss|Dr|Coach)\.?\s+[A-Z][\w'\u2019-]+")

DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "Martin Luther King Jr. Day",
    "Martin Luther King Jr.",
    "Martin Luther King",
    "MLK",
    "Parent Teacher Conference",
    "Parent-Teacher Conference",
    "Faria",
    "FEEF",
    "CEEF",
    "CUSD",
    "ELOP",
    "ELO-P",
    "ParentSquare",
    "Halloween",
)
"""Phrases never redacted, matched case-insensitively. Extend from real data."""

Span = tuple[int, int]


class TextRedactor(Protocol):
    """What `IngestionService` needs: text in, redacted text out."""

    def redact(self, text: str) -> str:
        """Return `text` with people's names, emails and phone numbers replaced."""
        ...


class Redactor:
    """Titled-name pattern + spaCy PERSON entities + emails + phone numbers.

    ``person_spans`` is injectable for tests (default: the spaCy model, loaded
    once per process). ``extra_names`` are always hidden — for a name the model
    keeps missing (``settings.redact_extra_names``).
    """

    def __init__(
        self,
        *,
        person_spans: Callable[[str], list[Span]] | None = None,
        extra_names: Sequence[str] = (),
        allowlist: Sequence[str] = DEFAULT_ALLOWLIST,
    ) -> None:
        self._person_spans = person_spans or _spacy_person_spans
        self._extra = [
            re.compile(rf"\b{re.escape(n.strip())}\b", re.IGNORECASE)
            for n in extra_names
            if n.strip()
        ]
        self._allow = [re.compile(re.escape(a), re.IGNORECASE) for a in allowlist]

    def redact(self, text: str) -> str:
        """Return `text` with names, emails and phone numbers replaced."""
        if not text:
            return text
        protected = _spans(self._allow, text)
        found: list[tuple[int, int, str]] = []
        found += [(s, e, EMAIL) for s, e in _spans([_EMAIL_PATTERN], text)]
        found += [(s, e, PHONE) for s, e in _spans([_PHONE_PATTERN], text)]
        names = _spans([_TITLED_NAME_PATTERN, *self._extra], text)
        names += [m.span(1) for m in _NAME_BEFORE_EMAIL_PATTERN.finditer(text)]
        names += [_whole_words(span, text) for span in self._person_spans(text)]
        found += [(s, e, NAME) for s, e in names if not _overlaps((s, e), protected)]
        return _replace(text, found)


def _spans(patterns: Iterable[re.Pattern[str]], text: str) -> list[Span]:
    return [m.span() for p in patterns for m in p.finditer(text)]


def _whole_words(span: Span, text: str) -> Span:
    """Widen a span to whole words, so a model hit can't leave "[name]g" behind."""
    start, end = span
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    return start, end


def _overlaps(span: Span, others: list[Span]) -> bool:
    return any(span[0] < e and s < span[1] for s, e in others)


def _replace(text: str, found: list[tuple[int, int, str]]) -> str:
    """Replace spans right to left; overlapping hits collapse into one placeholder."""
    merged: list[tuple[int, int, str]] = []
    for start, end, label in sorted(found):
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_label = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_label)
        else:
            merged.append((start, end, label))
    for start, end, label in reversed(merged):
        text = text[:start] + label + text[end:]
    return text


@cache
def _nlp() -> Language:
    # Imported lazily: the API imports this module too (menu upload runs
    # ingestion), and loading spaCy at import time would slow its cold start.
    import spacy

    # Only the NER component is needed; skipping the rest keeps it fast.
    return spacy.load(
        "en_core_web_md", disable=["parser", "lemmatizer", "tagger", "attribute_ruler"]
    )


_NOT_NAMES = frozenset(
    {
        # The small model tags capitalized words at the start of a line or
        # sentence as PERSON, and stretches a real name over the next word
        # ("Mrs. Chalmers Room 20" -> "Chalmers Room"). Found on real text;
        # these are trimmed off the edges of a PERSON span, or drop it whole.
        "email", "e-mail", "questions", "question", "thanks", "thank", "please",
        "contact", "note", "dear", "hi", "hello", "reminder", "join", "sign",
        "register", "volunteer", "volunteers", "parents", "parent", "families",
        "room", "class", "grade", "teacher", "teachers", "principal", "school",
        "office", "team", "staff", "lunch", "breakfast", "menu",
        "up", "ups", "grades", "contest", "season", "kinder", "mac",
        "disaster", "drill", "passcode", "zoom", "grants", "election",
        # vaccine names the model tagged PERSON in a real immunization notice
        "polio", "ipv", "hep", "hepatitis", "varicella", "flu", "influenza",
        # Outlook HTML conditional-comment junk inside email bodies: "[if gte mso 9]"
        "mso", "gte",
        # our own placeholders, tokenized as "[", "name", "]"
        "name", "phone",
    }
)


def _is_acronym(word: str) -> bool:
    return word.isupper() and word.isalpha()


def _spacy_person_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for ent in _nlp()(text).ents:
        if ent.label_ != "PERSON":
            continue
        tokens = list(ent)
        while tokens and (tokens[0].lower_ in _NOT_NAMES or not tokens[0].text[:1].isalpha()):
            tokens.pop(0)
        while tokens and (tokens[-1].lower_ in _NOT_NAMES or not tokens[-1].text[:1].isalnum()):
            tokens.pop()
        if all(_is_acronym(t.text) for t in tokens):
            continue  # "RSVP", "PTA", "HEAT WARNING": never a person
        if any(t.lower_ in _NOT_NAMES for t in tokens):
            continue  # a known non-name word still inside: "Disaster Drill", not a person
        if tokens:
            spans.append((tokens[0].idx, tokens[-1].idx + len(tokens[-1].text)))
    return spans


def build_redactor() -> Redactor:
    """The production redactor, with `settings.redact_extra_names` applied."""
    from acamar.config import get_settings

    return Redactor(extra_names=get_settings().redact_extra_names)
