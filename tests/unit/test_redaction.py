"""`Redactor` — names, emails and phone numbers out; known non-names kept.

Uses the real spaCy model (small, local, ~0.3 s to load once). Sample text is
invented, shaped like real school email; the allowlist cases are the real
false positives found on the household's own data (backlog item 51).
"""

from __future__ import annotations

import pytest

from acamar.ingestion.redaction import EMAIL, NAME, PHONE, Redactor


@pytest.fixture(scope="module")
def redactor() -> Redactor:
    return Redactor()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mrs. Chalmers is collecting slips.", "[name] is collecting slips."),
        ("Talk to Dr. Okafor first.", "Talk to [name] first."),
        ("Coach Rivera will run practice.", "[name] will run practice."),
        ("Jennifer Lopez will lead the session.", "[name] will lead the session."),
        ("Thanks, Sarah", "Thanks, [name]"),
    ],
)
def test_people_are_replaced_with_a_placeholder(
    redactor: Redactor, text: str, expected: str
) -> None:
    assert redactor.redact(text) == expected


def test_email_addresses_are_replaced(redactor: Redactor) -> None:
    assert redactor.redact("Questions? Email jane.doe@cusd.org today.") == (
        f"Questions? Email {EMAIL} today."
    )


@pytest.mark.parametrize(
    "phone", ["(408) 555-0142", "408-555-0142", "408.555.0142", "+1 408 555 0142"]
)
def test_phone_numbers_are_replaced(redactor: Redactor, phone: str) -> None:
    assert redactor.redact(f"Call {phone} to RSVP.") == f"Call {PHONE} to RSVP."


@pytest.mark.parametrize(
    "text",
    [
        "No school on Martin Luther King Jr. Day.",  # spaCy tags this PERSON
        "Sign up for the Parent Teacher Conference.",  # looks like "Teacher X"
        "FEEF, CUSD and Faria notes for Falcon Week.",
        "Please RSVP by Friday; the PTA will confirm.",  # spaCy tagged RSVP PERSON
        "No school on 2026-11-11. Zip 95014. Room 20.",  # numbers that aren't phones
    ],
)
def test_known_non_names_and_numbers_survive(redactor: Redactor, text: str) -> None:
    assert redactor.redact(text) == text


def test_a_word_after_a_name_is_not_swallowed(redactor: Redactor) -> None:
    # Real model behavior: it stretched "Chalmers" over "Room".
    assert redactor.redact("Mrs. Chalmers Room 20 trip") == "[name] Room 20 trip"


def test_a_capitalized_word_at_the_start_is_not_a_name(redactor: Redactor) -> None:
    assert NAME not in redactor.redact("Email the office with questions.")


def test_redacting_twice_changes_nothing(redactor: Redactor) -> None:
    once = redactor.redact(
        "Mrs. Chalmers and Jennifer Lopez: jane@x.org, 408-555-0142. Thanks, Sarah"
    )
    assert redactor.redact(once) == once


def test_empty_text_is_returned_as_is(redactor: Redactor) -> None:
    assert redactor.redact("") == ""


def test_extra_names_are_always_hidden() -> None:
    # A name the model misses can be listed explicitly (settings.redact_extra_names).
    redactor = Redactor(person_spans=lambda _text: [], extra_names=["zubair"])
    assert redactor.redact("Ask Zubair at pickup.") == f"Ask {NAME} at pickup."


def test_an_allowlisted_phrase_wins_over_a_name_hit() -> None:
    def everything_is_a_person(text: str) -> list[tuple[int, int]]:
        return [(0, len(text))]

    redactor = Redactor(person_spans=everything_is_a_person, allowlist=["Faria"])
    assert redactor.redact("Faria") == "Faria"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "On Tue, Sep 8, 2026 at 6:40 PM Jane Q Public <jane@x.org> wrote:",
            "On Tue, Sep 8, 2026 at 6:40 PM [name] <[email]> wrote:",
        ),
        ("From: J. Doe <j@x.org>", "From: [name] <[email]>"),
    ],
)
def test_a_name_right_before_an_address_is_hidden_but_not_am_pm(
    redactor: Redactor, text: str, expected: str
) -> None:
    # Reply headers: the model misses many names here; the shape is reliable.
    assert redactor.redact(text) == expected


def test_a_markdown_escaped_address_is_hidden_whole(redactor: Redactor) -> None:
    assert redactor.redact("contact [Kim\\_Doe@cusd.org](mailto:x)") == (
        "contact [[email]](mailto:x)"
    )


@pytest.mark.parametrize(
    "text",
    [
        # Real false positives of the small model, fixed by moving to medium.
        "Volunteer for the Disaster Drill on Oct 15.",
        "Call 408-555-0142. Disaster Drill on Oct 15.",  # flagged in this context
        "Join on Zoom, Meeting ID: 854 5009 8771 and Passcode: 927412.",
        "CEEF provides Teacher Grants that allow educators",
        "Required vaccines include DTaP, Polio, Hep B, Varicella, MMR.",
        "on an iPhone, iPad, Mac, or Apple Watch",
    ],
)
def test_real_school_mail_phrases_are_not_names(redactor: Redactor, text: str) -> None:
    assert NAME not in redactor.redact(text)
