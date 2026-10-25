"""`Enricher` backed by a local Ollama model, using structured (JSON) output."""

from __future__ import annotations

import ollama

from acamar.adapters import cloud_run_auth
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.enricher import ContentSummary, DocumentEnrichment, EnrichmentError

_ORG_GUIDE = """- "district": the school district's board/superintendent office (CUSD-level
  policy, board meetings, district-wide notices).
- "feef": fundraising, festivals, game passes, volunteer sign-ups, waivers for
  fundraiser events — anything from the Educational Enrichment Fund / PTA-style group.
- "classroom": one teacher's mail — homework, a classroom project, a test,
  a field trip, help requests for one class.
- "faria_admin": the school's own front office — attendance, pick-up policy,
  parking, health/safety notices, the school (not the district) speaking.
- "other": anything that doesn't clearly fit the above, or you're unsure."""

_PROMPT = f"""You are helping a parent triage school email for a daily dashboard.
Given the email below, respond with the requested fields.

- headline: a short (<=8 words) assistant-style title, e.g. "Fall Festival
  tonight, 5-9pm" — never just repeat the raw subject line verbatim.
- summary: two to three factual sentences — enough for a parent to decide
  whether to act, not just that something is happening. Include what it
  actually is (not just the event name), and any cost, location, or what
  they're being asked to do (sign up, pay, bring something), when stated.
  Skip filler like "an email was sent about..." — start with the substance.
- is_relevant: default to true. A date, a deadline, a task (sign a waiver,
  RSVP, a form due), or news about the child's school is ALWAYS relevant —
  true even if it's also promotional (a fundraiser with a real event date is
  relevant). Only mark false for genuine noise: a mass newsletter with no
  date/task/action for this family, an ad, a test/automated email, a generic
  "thank you" with nothing to do.
- organization: pick the single best fit —
{_ORG_GUIDE}
- event_date: if the content clearly states a specific date for the MAIN
  thing this email is about (the event/announcement it exists to make) —
  check the subject AND the body, including text inside an image
  caption/alt description — fill it in (YYYY-MM-DD). This is what a
  subject-only scan misses.
  Do NOT use a date that belongs to a different, secondary detail
  mentioned along the way (e.g. an unrelated reply-by/return-by date
  buried in a closing paragraph) if the main thing itself has no stated
  date — leave event_date null in that case, even if the email is
  otherwise full of dates elsewhere. Example: an email announcing
  "parent-teacher conferences are open, sign up for a slot at this link"
  with no date for the conferences themselves, but also saying "return the
  intake forms by tomorrow" — that "tomorrow" is about the forms, not the
  conferences; event_date should be null here, not tomorrow's date.
  When a relative weekday is stated ("next Wednesday", "this Friday"),
  compute the actual date carefully from the Received date's real day of
  the week — do not guess or assume it's soon.
  Otherwise omit it / set it to null. Do not guess a date that isn't
  clearly stated.
- holiday: if (and only if) the email clearly names a specific date school
  is closed (a holiday, a staff/no-school day), fill in its label and date.
  Otherwise omit it / set it to null. Do not guess a date that isn't stated.

Received: {{received_at}} (use this year for any date mentioned with no year stated)

Subject: {{subject}}

Body:
{{body}}
"""


_SUMMARY_PROMPT = """You are writing a short, clean dashboard card for a
family calendar app, describing a local community event. The source is a
scraped city webpage, not an email — its raw text can include leftover site
navigation, registration/sign-up boilerplate, FAQ fragments, or image
filenames mixed in with the real description. Ignore all of that; describe
only the actual event.

- headline: a short (<=8 words) assistant-style title, e.g. "Pumpkin
  carving and pool games" — don't just repeat the event name verbatim.
- summary: one to two factual sentences describing what the event actually
  is, and its time/location/cost if stated in the text below. Skip
  registration instructions, filenames, and generic marketing language
  ("don't miss out!", "a fun time for all!").

Page content (starts with the event's own name):
{body}
"""
# No separate "Event name: {{title}}" field (2026-10-02, found by review) —
# `doc.content` for a cupertino_events document already leads with the
# title (`f"{{title}} — {{body}}"`, see parser.py), so a separate field
# just duplicated it in every real call, wasting prompt budget for no
# benefit and occasionally nudging the model to echo the name verbatim
# despite being told not to, since it now saw it twice.


class OllamaEnricher:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        client: ollama.AsyncClient | None = None,  # inject in tests
    ) -> None:
        settings = get_settings()
        self._model = model or settings.ollama_summary_model
        self._base_url = base_url or settings.ollama_base_url
        self._injected_client = client

    async def _client_for_this_call(self) -> ollama.AsyncClient:
        # Per call, same as OllamaEmbedder: on GCP the ingestion job calls the
        # private Ollama service, which needs a fresh identity token each time.
        if self._injected_client is not None:
            return self._injected_client
        return await cloud_run_auth.ollama_client(self._base_url)

    async def enrich(self, doc: RawDocument) -> DocumentEnrichment:
        received_at = doc.published_at.date().isoformat() if doc.published_at else "unknown"
        prompt = _PROMPT.format(subject=doc.title, body=doc.content, received_at=received_at)
        try:
            client = await self._client_for_this_call()
            response = await client.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                format=DocumentEnrichment.model_json_schema(),
            )
            content = response.message.content
        except Exception as exc:
            # Any transport/model failure, AND a malformed response object
            # itself (e.g. `.message` absent — found by review, 2026-10-02:
            # this used to be read after the try/except had already closed,
            # so it would have raised a raw AttributeError instead of the
            # documented EnrichmentError, which IngestionService doesn't
            # catch — one bad response would have aborted the whole run
            # instead of costing just this document its polish).
            raise EnrichmentError(f"ollama chat ({self._model}): {exc}") from exc

        if not content:
            raise EnrichmentError(f"ollama chat ({self._model}) returned no content")
        try:
            return DocumentEnrichment.model_validate_json(content)
        except Exception as exc:
            # malformed/non-conforming JSON from a small model — treat like any
            # other model failure, not a bug in this adapter
            raise EnrichmentError(
                f"ollama chat ({self._model}) returned unparseable enrichment: {exc}"
            ) from exc

    async def summarize(self, doc: RawDocument) -> ContentSummary:
        prompt = _SUMMARY_PROMPT.format(body=doc.content)
        try:
            client = await self._client_for_this_call()
            response = await client.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                format=ContentSummary.model_json_schema(),
            )
            content = response.message.content
        except Exception as exc:
            raise EnrichmentError(f"ollama chat ({self._model}): {exc}") from exc

        if not content:
            raise EnrichmentError(f"ollama chat ({self._model}) returned no content")
        try:
            return ContentSummary.model_validate_json(content)
        except Exception as exc:
            raise EnrichmentError(
                f"ollama chat ({self._model}) returned unparseable summary: {exc}"
            ) from exc
