"""One-time scrub: redact names/emails/phones already stored in Gmail documents.

Backlog item 51 (2026-11-02): new Gmail text is redacted at ingestion; this
cleans what was stored before that — title, content, headline and summary
of every Gmail-derived row, soft-deleted ones included — and recomputes its
embedding from the redacted text, so no vector is derived from a name either.

    uv run python -m scripts.redact_stored          # dry run: counts + samples
    uv run python -m scripts.redact_stored --apply  # write it

``DATABASE_URL`` picks the database (local Postgres, or Cloud SQL through
``cloud-sql-proxy``). The dry run never prints original text — only redacted
results, where an over-eager hit shows up as e.g. "[name] Week" — so names
don't leak into a terminal or log while tuning the allowlist.

Same `build_redactor()` as ingestion, so a re-fetched email's redacted
content matches the scrubbed row and its enrichment is reused, not redone.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

from acamar.adapters.db.pool import connection_pool
from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.ingestion.redaction import EMAIL, NAME, PHONE, build_redactor

_FIELDS = ("title", "content", "headline", "summary")
_EMBED_BATCH = 32


async def run(*, apply: bool, samples: int) -> None:
    redactor = build_redactor()
    async with connection_pool() as pool, pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT id, external_id, category, title, content, headline, summary "
                "FROM documents WHERE domain = 'gmail' ORDER BY ingested_at"
            )
        ).fetchall()

        changed: list[dict[str, Any]] = []
        for row in rows:
            new = {f: redactor.redact(row[f]) if row[f] else row[f] for f in _FIELDS}
            if any(new[f] != row[f] for f in _FIELDS):
                changed.append({**row, "new": new})

        added = {p: 0 for p in (NAME, EMAIL, PHONE)}
        for doc in changed:
            for p in added:
                added[p] += sum(doc["new"][f].count(p) - doc[f].count(p) for f in _FIELDS if doc[f])
        print(f"Gmail documents scanned: {len(rows)}; would change: {len(changed)}")
        print("placeholders added: " + ", ".join(f"{p} {n}" for p, n in added.items()))
        _print_contexts(changed, samples)

        if not apply:
            print("\nDry run — nothing written. Re-run with --apply to scrub.")
            return

        embedder = OllamaEmbedder()
        for i in range(0, len(changed), _EMBED_BATCH):
            batch = changed[i : i + _EMBED_BATCH]
            texts = [_embedding_text(doc) for doc in batch]
            vectors = await embedder.embed(texts)
            async with conn.transaction():
                for doc, vector in zip(batch, vectors, strict=True):
                    await conn.execute(
                        "UPDATE documents SET title = %s, content = %s, headline = %s, "
                        "summary = %s, embedding = %s, updated_at = now() WHERE id = %s",
                        (*(doc["new"][f] for f in _FIELDS), vector, doc["id"]),
                    )
        print(f"\nScrubbed {len(changed)} documents (text + embeddings).")


def _embedding_text(doc: dict[str, Any]) -> str:
    """Exactly what ingestion embeds — `RawDocument.embedding_text` on the new text."""
    return RawDocument(
        external_id=doc["external_id"],
        title=doc["new"]["title"],
        content=doc["new"]["content"],
        domain=Domain.GMAIL,
        category=Category(doc["category"]),
    ).embedding_text


def _print_contexts(changed: list[dict[str, Any]], limit: int) -> None:
    """Redacted text only: each placeholder with ~35 characters either side."""
    contexts: list[str] = []
    for doc in changed:
        for f in ("headline", "summary", "title", "content"):
            text = doc["new"][f] or ""
            for m in re.finditer(r"\[(?:name|email|phone)\]", text):
                snippet = text[max(0, m.start() - 35) : m.end() + 35].replace("\n", " ")
                if snippet not in contexts:
                    contexts.append(snippet)
    print(f"\nSample of redacted text ({min(limit, len(contexts))} of {len(contexts)}):")
    for snippet in contexts[:limit]:
        print(f"  …{snippet}…")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--samples", type=int, default=60, help="redacted snippets to show")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply, samples=args.samples))


if __name__ == "__main__":
    main()
