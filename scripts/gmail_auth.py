"""Run (or refresh) the one-time Gmail OAuth consent — no DB, no ingestion.

    uv run python -m scripts.gmail_auth

Opens a local-browser consent screen on first run and caches the refresh
token at GMAIL_TOKEN_PATH; a later run just confirms the cached token is
still valid (silently refreshing it if expired). Doesn't touch the
`sources`/`documents` tables or the (still-stubbed) menu parser — it only
proves the OAuth wiring and a live API round-trip work, via a search query
that shouldn't match anything real.
"""

from __future__ import annotations

import asyncio

from acamar.adapters.gmail.client import GoogleGmailClient

_PROBE_QUERY = "subject:acamar-oauth-probe-does-not-exist"


async def run() -> None:
    client = GoogleGmailClient()
    emails = await client.search(_PROBE_QUERY, max_results=1)
    print(f"OAuth OK — Gmail API reachable. {len(emails)} messages matched (expected 0).")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
