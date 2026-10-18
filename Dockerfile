# ACAMAR — one multi-stage Dockerfile, four build targets (api / dashboard /
# cockpit / ingestion), not four separate Dockerfiles — see
# docs/gcp-aws-deployment-proposal.md §14 "Build shape" for why: the four
# components share one pyproject.toml, so one dependency-install layer
# reused across all of them means less to rebuild and no drift between
# them. `docker compose` builds from this file locally for fast iteration;
# the same file gets built once per target and pushed to Artifact Registry
# for the real Cloud Run deploy (§7) — never two different build
# definitions for "local" vs. "cloud".
#
# Ollama is deliberately NOT one of these targets — see §14: Docker
# Desktop on Mac can't pass Metal GPU through to a container, so
# containerizing it locally would silently lose the ~19x speedup this
# project already measured. It stays the native Ollama.app; containers
# reach it via `host.docker.internal` (see .env.docker).

FROM python:3.13-slim AS base

# postgresql-client — just for `psql`, used by the local `seed` compose
# service to apply db/schema.sql (mirrors the exact `psql acamar -f
# db/schema.sql` step the README already documents for bare-metal setup,
# not a new mechanism). Not needed by api/dashboard/cockpit/ingestion at
# runtime — they only ever talk to Postgres via psycopg — but it's cheap
# (~10MB) and keeping one shared base simpler was worth more than trimming
# it out of the three targets that don't use it.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# uv pinned to the exact version already in use for local dev (0.12.10,
# confirmed via `uv --version` before writing this), not `:latest` — same
# "pin exact versions" discipline this project applies everywhere else.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, separate from the source copy below, so editing
# application code doesn't invalidate this (slow) layer on every rebuild.
# --no-install-project: install only the third-party dependencies here,
# not the local `acamar` package itself — its source isn't copied in yet
# at this point. Real bug caught by actually running this, not assumed:
# without --no-install-project, `uv sync` silently succeeds but never
# registers `acamar` in the venv (nothing to install from yet), and every
# `import acamar...` fails at runtime with ModuleNotFoundError — the
# dependencies all work fine, which is what made this easy to miss
# without actually building and running the image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
# --locked (not a bare `uv sync`): fails loudly if uv.lock and
# pyproject.toml have drifted, instead of silently re-resolving and
# producing a different lock than the one actually committed — this
# project has hit a real stale-uv.lock gotcha before (manual_menu's
# pdfplumber/python-multipart, 2026-09-25), confirmed resolved now by
# checking uv.lock directly, but --locked keeps that class of drift from
# becoming a silent Docker-build-time surprise again.

# Application code. Everything the four targets below actually need:
# the package itself, the scripts/ CLI entrypoints (ingestion, seed,
# gmail_auth), and db/ (schema.sql — used by the local `seed` compose
# service only; the real Cloud SQL migration path is the numbered-SQL
# runner, §8, not this file).
COPY src/ src/
COPY scripts/ scripts/
COPY db/ db/

# Second sync, now that src/ exists — fast, since every third-party
# dependency is already installed from the layer above; this one just
# registers the local `acamar` package itself into the venv.
# --no-editable: a container image should be a sealed artifact, not an
# editable link back to a source tree — and a real bug, found by actually
# running this, not assumed: a plain editable install here left a
# dist-info with an *empty* RECORD and no .pth/finder file at all, so
# `import acamar` failed despite the package "being installed" by every
# metadata check. --no-editable installs a real copy into site-packages,
# sidestepping whatever caching path produced that broken editable link.
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

# ---------------------------------------------------------------------------
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "acamar.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
FROM base AS dashboard
EXPOSE 8501
CMD ["streamlit", "run", "src/acamar/dashboard/streamlit_app.py", \
     "--server.address=0.0.0.0", "--server.port=8501"]

# ---------------------------------------------------------------------------
FROM base AS cockpit
EXPOSE 8502
CMD ["streamlit", "run", "src/acamar/dashboard/cockpit_app.py", \
     "--server.address=0.0.0.0", "--server.port=8502"]

# ---------------------------------------------------------------------------
FROM base AS ingestion
# No default CMD — the source id is supplied at `docker run`/Cloud Run Job
# execution time (`docker compose run ingestion --source <id>`), matching
# this connector's real one-shot-per-source nature, not baked into the
# image. This same target becomes both Cloud Run Jobs (§11 phase 6),
# parameterized the same way.
ENTRYPOINT ["python", "-m", "scripts.run_ingestion"]
