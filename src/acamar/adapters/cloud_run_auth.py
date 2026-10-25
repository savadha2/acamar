"""Private Cloud Run → Cloud Run calls to the project's Ollama service.

The Ollama service is deployed without ``--allow-unauthenticated`` (Ollama
has no auth of its own), so every caller on GCP must attach a Google-signed
identity token. Shared by `OllamaEmbedder` and `OllamaEnricher` — extracted
from the embedder (2026-10-24) when the ingestion jobs started calling the
same service for `llama3` enrichment.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import ollama

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)


def is_cloud_run_host(url: str) -> bool:
    """Whether `url` points at a Cloud Run service (a `*.run.app` host).

    Gates the identity-token fetch below — must stay False for
    `localhost`/bare-metal/`docker compose`, or local dev would try (and
    hang, then fail) to reach a metadata server that doesn't exist there.
    """
    host = urlparse(url).hostname
    return host is not None and host.endswith(".run.app")


async def identity_token(audience: str) -> str:
    """A Google-signed ID token for calling another Cloud Run service privately.

    Fetched fresh per call from the GCP metadata server. Only reachable, and
    only ever called, when actually running on GCP compute (see
    `is_cloud_run_host`).
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _METADATA_IDENTITY_URL,
            params={"audience": audience},
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.text


async def ollama_client(base_url: str) -> ollama.AsyncClient:
    """An `ollama.AsyncClient` for `base_url`, with an identity token if it's Cloud Run.

    Build one per call, not once at construction — a token fetched up front
    would go stale on a long-lived process.
    """
    headers: dict[str, str] = {}
    if is_cloud_run_host(base_url):
        token = await identity_token(base_url)
        headers["Authorization"] = f"Bearer {token}"
    return ollama.AsyncClient(host=base_url, headers=headers)
