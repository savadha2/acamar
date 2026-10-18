# GCP vs. AWS deployment — architecture proposal (2026-10-10)

Status: **proposal, not implemented.** Merges backlog items 28 ("containerize
+ get the app deployment-ready on GCP") and 46 ("compare GCP vs. AWS") into
one decision — item 28 had assumed GCP without ever actually weighing it
against the alternative; this is that comparison, plus a concrete target
architecture and rollout plan for whichever way it lands. Nothing below is
built. Written per this project's standing convention that design changes
get proposed and reviewed before anything is built — ends with explicit
open decisions that need a go-ahead before any of this is built.

---

## 1. Today's baseline

Everything runs bare-metal/local, by hand, on one Mac:

| Component | Today |
|---|---|
| API | `uvicorn --reload` by hand, no supervisor |
| Household dashboard | `streamlit run streamlit_app.py` by hand |
| Pilot Cockpit (admin) | `streamlit run cockpit_app.py --server.port 8502` by hand |
| Ingestion (faria_events, gmail_school) | local `launchd`, `run_daily_ingestion.sh`, 6×/day |
| Ingestion (cupertino_events) | local `launchd`, `run_cupertino_events_ingestion.sh`, 1×/day |
| Postgres + pgvector | local install |
| LLM (`llama3` via Ollama) | local Ollama.app, Metal GPU (~19× over the Homebrew CPU build) |
| Secrets | `.env` + `secrets/gmail_*.json`, filesystem only |

No Dockerfile or `docker-compose.yml` exists yet (confirmed — item 8 is
still open). No migration tooling exists (item 17, still open). This
proposal is the first time all of that has to become real at once, not a
docs exercise in isolation.

---

## 2. The axis that actually discriminates: LLM / GPU serving

This is the one place GCP and AWS are *not* roughly equivalent, so it gets
the detailed treatment the others don't need.

**Confirmed directly (not assumed), October 2026:**

- **Cloud Run GPU (NVIDIA L4) is generally available**, no quota request
  needed, billed per-second with **scale-to-zero** — a Cloud Run service
  can hold zero GPU instances when idle and spin one up on request. Google
  publishes an [official tutorial for this exact stack](https://docs.cloud.google.com/run/docs/tutorials/gpu-gemma-with-ollama)
  — Ollama serving an open model, on Cloud Run, with GPU. GA regions as of
  late 2026 include `us-central1`, `us-east4`, `europe-west1`,
  `europe-west4`, and `asia-southeast1`.
- **AWS Fargate does not support GPU workloads, at all, in any form, as of
  October 2026.** The only way to attach a GPU to an AWS container workload
  is ECS with the **EC2** launch type (G5/G6 instances) or raw EC2 — which
  means provisioning, patching, and capacity-planning real instances again,
  not a serverless container. There is no AWS equivalent of "scale-to-zero
  GPU container."

**What this means concretely**: on GCP, the *entire* stack — API, both
Streamlit apps, Ollama, and the scheduled ingestion job — can run on one
compute model (Cloud Run + Cloud Run Jobs) without ever provisioning a VM
or managing a node pool. On AWS, the stateless services (API, dashboards)
fit Fargate/App Runner fine, but Ollama specifically forces a second,
heavier compute model (EC2 or ECS-on-EC2) back into the picture — a split
architecture for the same outcome, and a real regression from "no
supervisor" simplicity for a single-operator household project (the exact
thing CLAUDE.md's own gotcha already flags as this project's current
operational reality).

**Cost shape, not a quote**: Cloud Run GPU (L4, non-zonal) runs about
$0.67/hr while warm, $0 while scaled to zero. A household Q&A tool used a
handful of times a day, with scale-to-zero, could plausibly cost single-digit
dollars a month on GPU-seconds alone. The tradeoff is cold start: the first
query after idle time pays for GPU provisioning + model load into VRAM —
plausibly 10–30+ seconds. That's a real cost, but it lands inside a latency
norm this project has **already accepted**: P5 item 22 (confidence-gated
model cascade) is explicitly gated on "if the 20–30s `/query` round trip
becomes an actual felt problem" — it hasn't been built because that
threshold hasn't been hit yet. A GPU cold start fits inside, not past, an
already-tolerated latency band. (If `min-instances=1` is used instead to
kill cold starts entirely, that's the ~$480/mo always-on number — a real
option, just a materially different cost, and one AWS can't even offer
serverlessly as a contrast point.)

**Recommendation: GCP, primarily on this axis.** Everything else in this
doc is close to a wash between providers; this one isn't.

---

## 3. Postgres + pgvector

Both clouds fully support it now — confirmed directly, not assumed:

| | GCP | AWS |
|---|---|---|
| Managed option | Cloud SQL for PostgreSQL, or AlloyDB | RDS for PostgreSQL, or Aurora PostgreSQL |
| `pgvector` | Yes, standard extension | Yes, **pgvector 0.8.0**, HNSW indexing supported |
| Notes | AlloyDB has its own enhanced `vector` extension + a `ScaNN`-based index (`alloydb_scann`) — more powerful, more expensive, built for a scale well past one household's document count | Aurora PostgreSQL's pgvector 0.8.0 release specifically improved HNSW build/query performance |

`schema.sql`'s existing HNSW index is directly supported on either side —
this doesn't drive the provider choice. **Recommendation: Cloud SQL for
PostgreSQL** (not AlloyDB) — the smallest viable managed Postgres tier,
resolved by consistency with §2's GCP pick, not by this axis having its own
winner. AlloyDB's extra vector tooling is overkill for this corpus size
(low thousands of documents, growing slowly).

**Not yet verified, flagged rather than assumed**: whether Cloud SQL's
smallest shared-core tier has enough RAM headroom to build and hold an
HNSW index without it getting evicted or build time ballooning — HNSW
is memory-hungry relative to a plain B-tree index, and "smallest viable
tier" needs that confirmed against the real tier's memory spec during
§11 phase 3, not assumed from "it's a small corpus" alone.

---

## 4. Container orchestration & the kill switch — not actually a GCP-vs-AWS question

CLAUDE.md's own gotcha already names the real risk: the Pilot Cockpit's
hard kill switch (`POST /admin/shutdown`, sends the API process `SIGTERM`)
is "irreversible" today *specifically because* nothing supervises the
process — nothing races to relaunch it. **Any** managed container
orchestrator breaks that assumption, on either cloud: a Cloud Run service,
a GKE Deployment, an ECS Service, or App Runner will all restart a
container that exits for any reason — including a clean self-`SIGTERM` —
because that's the entire point of a managed "service." This is structural
to "managed," not a provider difference, so it doesn't belong in the GCP
vs. AWS comparison — but it does need an explicit decision before
deploying as-is, which this section resolves.

**Recommended fix, shaped for Cloud Run specifically (since §2 picks
GCP)**: stop having the shutdown endpoint signal its own process. Instead,
call the **Cloud Run Admin API** to scale the API service's instance count
to zero. This actually preserves the *original* design intent better than
today's `SIGTERM` does: it's a real stop (not just a crashed-and-restarted
container), and it's genuinely not reversible from the cockpit UI itself —
undoing it means a human going to the Cloud Run console or running
`gcloud run services update`, not clicking anything in the cockpit. The
existing soft pause (503 on every non-admin route) needs no change at all
— it's already orchestrator-agnostic.

**The plumbing, worked out 2026-10-17:**

- **Who makes the call — a refinement, not the original one-liner.** Not
  "the API service calls the Admin API on itself" — a service asking the
  control plane to stop itself mid-request can race its own HTTP response
  back to the cockpit. Instead, **the Cockpit's own backend makes the
  call, from the outside, targeting the API service** — Streamlit apps run
  real server-side Python on every interaction, so the cockpit's shutdown
  button handler can call the Cloud Run Admin API directly. This also maps
  onto "irreversible from the cockpit" more literally: the cockpit reaches
  *out* to the control plane, it doesn't ask the thing it's killing to kill
  itself.
- **Identity**: a dedicated service account attached to the Cockpit's
  Cloud Run service (not the default Compute Engine SA). No key file to
  manage — Cloud Run attaches it automatically, and Google's auth
  libraries pick it up via Application Default Credentials with zero
  extra code.
- **Permission, least-privilege**: a custom IAM role containing only
  `run.services.update` (+`run.services.get` to check current state),
  bound to that service account **scoped to the API service resource
  only** — not the broad `roles/run.admin`. The cockpit's kill switch
  shouldn't be able to touch any other Cloud Run service in the project,
  itself included.
- **The actual call**: `PATCH` the API service's scaling config
  (`maxInstanceCount: 0`) via the [Cloud Run Admin API
  v2](https://docs.cloud.google.com/run/docs/reference/rest) — confirmed
  current, Oct 2026: in-flight requests are allowed to finish, every new
  request to the service 503s, and the service is disabled, not deleted
  (its config survives, so re-enabling later doesn't mean redeploying).
- **Dependency choice, flagged not assumed**: the full `google-cloud-run`
  SDK, or a lean raw `PATCH` via `google-auth`'s `AuthorizedSession` (no
  new SDK dependency). Recommend the lean path — consistent with this
  project's established taste for skipping a heavier dependency when a
  thin one covers it (Open-Meteo over a weather SDK, numbered SQL files
  over Alembic).
- **Resume stays deliberately unplumbed.** Not a missing feature — the
  cockpit should have *no* button that undoes a kill. A human runs
  `gcloud run services update acamar-api --max-instances=<N>` (or uses
  Cloud Console) by hand — state-changing infrastructure commands are run
  by a person, not automated. That's not just this project's usual
  preference here — it *is* the security design for this control.

Alternative, if that's more machinery than wanted: scope-reduce and drop
the hard kill switch's self-restart-proofing guarantee once deployed,
keeping pause as the only real control. Flagged as an open decision in
§12, not assumed.

---

## 5. Secrets

| | GCP Secret Manager | AWS Secrets Manager |
|---|---|---|
| Pricing | ~$0.06 / active secret-version / month + access ops | $0.40 / secret / month + $0.05 / 10k API calls |

The real secret count here is small — `gmail_credentials.json`,
`gmail_token.json`, `COCKPIT_TOKEN`, the Postgres connection string. At
that count the cost difference is a rounding error (low single-digit
dollars either way). **Recommendation: Secret Manager**, resolved by
consistency with §2, not by this axis.

---

## 6. Scheduled ingestion

Maps directly onto the two real, separate `launchd` jobs that exist today:

| Today | GCP equivalent | AWS equivalent |
|---|---|---|
| `run_daily_ingestion.sh`, 6×/day (`com.acamar.ingest-faria-events.plist`) | Cloud Scheduler → Cloud Run Job | EventBridge Scheduler → ECS Fargate `RunTask`, or Lambda (real ingestion runs finish in well under Lambda's 15-min cap, per the Cupertino run's own logged ~178s) |
| `run_cupertino_events_ingestion.sh`, 1×/day | Cloud Scheduler → Cloud Run Job | same as above |

Both clouds handle this fine — Fargate doesn't need GPU for ingestion, so
§2's constraint doesn't apply here. **Recommendation: Cloud Scheduler +
Cloud Run Jobs**, again resolved by consistency with §2 (one platform, one
mental model) rather than this axis having an independent winner. Two
separate Scheduler jobs + two separate Job definitions, matching the two
separate plists exactly — `manual_menu` stays upload-triggered, unaffected
either way.

**A real prerequisite found 2026-10-17, not previously known**: the
`gmail_school` Cloud Run Job can't actually run unattended yet. Its OAuth
app sits in Google Cloud Console's "Testing" publishing status, which caps
refresh tokens at exactly 7 days — confirmed, not theorized, from the
real local incident history (item 48/49). A non-interactive Cloud
Scheduler job can't complete a browser consent flow every 7 days.
**Fix, cheaper than first assumed**: flip the consent screen's publishing
status to "In production" in Google Cloud Console — this alone removes
the 7-day cap, with **no verification review required** for continued
personal/single-user use (only an "unverified app" warning on the now-rare
real consent events). A Console action for the user, not blocked on
anything else here — doesn't need to wait for phase 6 to be done, just
needs to be done before phase 6 is trusted to run unattended.

---

## 7. CI/CD (item 7) — the full local-change-to-deployed mechanism

Item 7 has been sitting open as general infra hygiene (lint → mypy →
tests, on push). It stops being optional the moment there's a container
image that needs building, pushing, *and actually deployed* before a local
change is live — and **the original version of this section stopped at
"pushing to Artifact Registry," never specified the deploy step itself**.
Caught 2026-10-17, reviewing this doc end to end: that's a real gap, not a
detail — without it, nothing describes how a tested local change ever
reaches Cloud Run. Filled in now:

**The full pipeline, one GitHub Actions workflow, gated top to bottom:**

1. **Local loop** (unchanged by any of this): edit code, `docker compose up
   --build` (or restart one service), test against local Postgres/Ollama —
   §13/§14's phase 1 stack *is* this loop.
2. **Push/merge to `main`** triggers the workflow.
3. **Test gate**: `ruff check`, `mypy`, `pytest` — item 7's original scope,
   unchanged. A failure here stops the pipeline; nothing builds or deploys.
4. **Build**: the one multi-stage `Dockerfile` (§14), all four targets.
5. **Tag by git SHA**, not just `latest` — `latest` alone gives no way to
   tell Cloud Run "go back to exactly what was running an hour ago."
6. **Push** all four tagged images to Artifact Registry.
7. **Deploy**: `gcloud run deploy <service> --image=<sha-tag>` for `api`,
   `dashboard`, `cockpit`; update the two Cloud Run Jobs' image reference
   for `ingestion` (jobs aren't "deployed" the same way services are, but
   still need the new image wired in).
8. **Rollback, if a deploy goes bad**: Cloud Run keeps prior revisions —
   `gcloud run services update-traffic SERVICE --to-revisions=PRIOR=100`
   shifts traffic back without rebuilding anything. Worth a one-line runbook
   note once this is live, not just tribal knowledge.

**Auth**: [Workload Identity
Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation)
so GitHub Actions authenticates as a GCP service account without ever
holding a long-lived key — that SA needs `run.services.update` scoped to
just the four ACAMAR services (+ the two Jobs), the same least-privilege
shape as the kill-switch SA in §4, not broad project-level deploy rights.

**Recommendation: GitHub Actions** over Cloud Build natively — keeps the
CI/CD definition portable if the cloud choice is ever revisited (§10).

**Genuinely undecided, not assumed — new decision 7 in §12**: does every
merge to `main` auto-deploy straight to the one real environment, or is
there a manual approval step first? This project has no staging
environment anywhere in this proposal, and at single-household scale
auto-deploy is the lower-ceremony default — but it's a real fork, not
picked silently here.

---

## 8. Migration tooling (item 17) — this deploy is the trigger it was waiting for

Item 17's own standing note says "adopt at first cloud deploy or first
expensive-to-rebuild data" — and separately logs a real near-miss
(2026-10-02: an un-migrated `documents.url` column took the whole dashboard
down) as evidence the gate is real, not theoretical. This proposal *is*
that first cloud deploy. **Recommendation: the minimum viable version**,
not full Alembic — numbered SQL files (`db/migrations/0001_*.sql`,
`0002_*.sql`, ...) + a small runner script that tracks which have been
applied in a one-row-per-migration table. One household DB, one operator,
but now one you can't `ALTER TABLE` by hand on a whim the way the local
Postgres instance allows today — enough process to not repeat the
`documents.url` incident against a database that isn't sitting on the same
machine as the terminal you're typing in.

---

## 9. Recommended target architecture

```
                      ┌─────────────────────────────┐
  Cloud Scheduler ───▶│  Cloud Run Jobs              │
  (6×/day, 1×/day)    │  faria+gmail ingestion       │───┐
                      │  cupertino_events ingestion   │   │
                      └─────────────────────────────┘   │
                                                          ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐
│ Cloud Run     │    │ Cloud Run     │    │ Cloud SQL             │
│ household     │───▶│ FastAPI API   │───▶│ PostgreSQL + pgvector │
│ dashboard     │    │ (Cloud Run)   │    │ (HNSW)                │
└──────────────┘    └──────┬───────┘    └──────────────────────┘
┌──────────────┐           │
│ Cloud Run     │───────────┤
│ Pilot Cockpit │           ▼
│ (admin)       │    ┌──────────────────┐
└──────────────┘    │ Cloud Run (GPU)   │
                      │ Ollama + llama3   │
                      │ internal-only      │
                      │ ingress, scale-to-0 │
                      └──────────────────┘

  Secret Manager: gmail creds/token, COCKPIT_TOKEN, DB URL — mounted
  into every service above, not baked into images.
```

The Ollama service gets Cloud Run's **internal-only ingress** setting (not
publicly reachable — it has no auth of its own, unlike every other service
here which already sits behind `subscribers`/`cockpit_token`).

**Corrected 2026-10-17** — reaching it isn't as simple as "over a VPC
connector," confirmed by checking current docs rather than assuming: a
`*.run.app` URL resolves to a public IP by default, so even with
**Direct VPC egress** enabled on the API service (the modern replacement
for a Serverless VPC Access connector), a plain call to Ollama's `.run.app`
URL still routes over the public internet path and gets blocked by the
internal-ingress setting. Actually reaching it needs a private DNS zone in
the API's VPC with a wildcard record for `*.run.app` pointing at Google's
internal range (`199.36.153.8–11`), so the API's own DNS resolution sends
that traffic over the VPC instead of out to the internet. Two real pieces,
not one — worth building and testing this specific hop in isolation during
§11 phase 5, not assuming it works because Direct VPC egress is enabled.

The Cockpit's kill-switch call (§4) is a **separate** connection, not shown
in the diagram above: Cockpit → Cloud Run Admin API (`run.googleapis.com`,
a public Google API, not a service in this VPC at all) → the API service's
own scaling config. No VPC/DNS plumbing needed for that path — it's a
regular authenticated HTTPS call to a Google-managed control-plane
endpoint, same category as the Cloud SQL/Secret Manager access every
service already does.

---

## 10. Why not AWS — stated plainly, not overclaimed

AWS is a perfectly good cloud in general. It loses *here* for one specific,
confirmed reason: no serverless/scale-to-zero path for a GPU-backed
container, which forces a split compute model for one workload (Ollama)
that GCP can run on the same platform as everything else. If the LLM
strategy ever changes — e.g. swapping self-hosted Ollama for a hosted API
model (Bedrock, or Anthropic/OpenAI directly) instead of running `llama3`
yourself — §2 is exactly the axis that would stop discriminating, and the
recommendation here could reasonably flip. Said plainly so this doesn't
read as more certain than it is: this is "GCP wins given today's
self-hosted-Ollama architecture," not "GCP wins, full stop, forever."

---

## 11. Phased rollout plan

1. **Containerize** — one multi-stage `Dockerfile` (shared dependency
   layer, four build targets: API/dashboard/cockpit/ingestion — §14) +
   `docker-compose.yml` + `.dockerignore` for local parity (closes item 8,
   scoped bigger than its original "local dev only" framing).
2. **Migration tooling** (§8) — build the minimum-viable runner *before*
   touching a cloud database with it.
3. **Cloud SQL** — provision, apply schema via the new migration runner,
   confirmed empty-but-ready before anything points at it.
4. **Cloud Run: API, dashboard, cockpit** — deployed pointing at Cloud SQL;
   Ollama still local temporarily (a tunnel, or just finish ingestion/query
   smoke tests against a still-local Ollama) to decouple this step from §2.
5. **Cloud Run GPU: Ollama** — deploy, wire internal ingress + VPC
   connector, cut the API over to it, confirm `/query` end-to-end against
   the real cloud stack.
6. **Cloud Scheduler + Cloud Run Jobs** — stand up both ingestion jobs,
   confirm a real run, then (and only then) disable the local `launchd`
   jobs.
7. **Secret Manager** — migrate `.env`/`secrets/*.json` off the local
   filesystem; redeploy every service reading from it instead.
8. **Kill switch redesign** (§4) — build + test the Cloud Run Admin API
   version; confirm it still behaves as a real, hard-to-accidentally-undo
   stop before trusting it the way the local version was trusted.
9. **Decommission local** — stop the local API/Streamlit/Ollama/Postgres/
   cron once the cloud stack has run clean for a real stretch, not on day
   one of cutover.

---

## 12. Open decisions — need your go-ahead before any of this is built

1. ~~Confirm GCP over AWS.~~ **DECIDED 2026-10-16: GCP.** See §13 for the
   concrete first steps this unlocks.
2. **Confirm Cloud SQL over AlloyDB.** Recommended: **Cloud SQL** (§3).
3. **Kill switch**: rebuild on the Cloud Run Admin API (§4's main
   recommendation), or scope-reduce to pause-only once deployed? Recommended:
   **rebuild** — it's a small amount of real work for a control this
   project has already invested real design effort in twice.
4. **Migration tooling**: the numbered-SQL-files-plus-runner approach (§8),
   or go straight to Alembic? Recommended: **numbered files + runner** —
   consistent with item 17's existing "not worth the heavier tool yet"
   judgment, just enough process to be safe against a database you can't
   hand-patch anymore.
5. **CI**: GitHub Actions (§7), or Cloud Build natively? Recommended:
   **GitHub Actions**.
6. **Any monthly cost ceiling** this should be designed against? §2's
   numbers are order-of-magnitude, not a quote — worth running through the
   GCP pricing calculator for real once the shape above is confirmed,
   before anything is provisioned.
7. **Auto-deploy every merge to `main`, or a manual approval gate first?**
   (§7, added 2026-10-17 — this question didn't exist until the deploy
   mechanism itself was specified.) Recommended: **auto-deploy** — no
   staging environment exists anywhere in this proposal, and at
   single-household scale a manual gate is ceremony without a second
   environment for it to protect against.

Decisions 2–7 are still open. Nothing in this proposal is built yet — but
§13 below doesn't need any of decisions 2–7 resolved first.

---

## 13. First session — concrete steps, ~15 minutes each

Decision 1 only unlocks §11's **phase 1** (containerize) — it doesn't touch
Postgres, secrets, CI, or the kill switch, so none of decisions 2–7 block
starting here. Each step below is sized to review or act on in about 15
minutes, and marked who it's for.

**Can happen anytime, in parallel with everything below (you — state-changing, account-level):**

0. **GCP project groundwork.** Create or pick a project, link billing, enable
   the APIs this will eventually need: `run.googleapis.com`,
   `sqladmin.googleapis.com`, `secretmanager.googleapis.com`,
   `artifactregistry.googleapis.com`, `cloudscheduler.googleapis.com`,
   `compute.googleapis.com` (for GPU quota). Review-and-run, e.g.:
   ```
   gcloud projects create acamar-prod   # or pick an existing project
   gcloud billing projects link acamar-prod --billing-account=<ID>
   gcloud services enable run.googleapis.com sqladmin.googleapis.com \
     secretmanager.googleapis.com artifactregistry.googleapis.com \
     cloudscheduler.googleapis.com compute.googleapis.com \
     iam.googleapis.com \
     --project=acamar-prod
   ```
   `iam.googleapis.com` added 2026-10-17 — not needed by phase 1 itself,
   but by the custom least-privilege IAM roles §4 (kill switch) and §7
   (deploy) both call for later; cheaper to enable once now than to notice
   it's missing mid-phase-8.

   Also worth doing in this same step: request Cloud Run GPU (L4) quota in
   a supported region (§2) if it isn't already granted — this can take
   longer than 15 minutes to be *approved*, so starting the request early
   avoids it blocking phase 1 of the real rollout (§11) later.

**Phase 1 — containerize (me, ~15 min each, pure local files — nothing cloud-side touched). Revised 2026-10-17 to match §14's shared-base-image decision — was five separate `Dockerfile.*` files, now one multi-stage file:**

1. `Dockerfile` — `base` stage (`python:3.13-slim`, `uv sync`), plus four
   named targets (`api`, `dashboard`, `cockpit`, `ingestion`) built off it
   via `--target`, each just a different `CMD`. The `ingestion` target
   becomes both Cloud Run Jobs in §11 phase 6, parameterized by
   `--source <id>` at job-execution time rather than baked in.
2. `.dockerignore` — excludes `.venv`, `secrets/` (bind-mounted, not baked
   in), `logs/`, `.git`, test caches.
3. `.env.docker` — just the handful of values that differ inside the
   compose network (`database_url`, `ollama_base_url`, `api_base_url` —
   see §14's table); everything else still comes from the existing `.env`.
4. `docker-compose.yml` — services `postgres` (`pgvector/pgvector:pg17`),
   `api`, `dashboard`, `cockpit` (each built from `Dockerfile` with the
   matching `target:`), `ingestion` as a profile/`run`-only service (not
   always-up — matches its real one-shot nature), and a one-shot `seed`
   service. Closes item 8, scoped to what phase 1 actually needs, not the
   "local dev only" framing it was originally filed under.

**Checkpoint (you, ~15 min — review/run, not a cloud action, fully local):**

5. `docker compose up`, confirm: Postgres comes up with the schema applied
   (via the `seed` service), the API's `/health` responds, both Streamlit
   apps load, and `docker compose run ingestion --source <id>` completes
   and writes real rows. Report back what broke, if anything — Dockerfiles
   written blind to a container runtime usually need at least one real
   fix pass.

Phases 2–9 (§11) start after step 5 is clean — each is a bigger lift than
15 minutes and depends on decisions 2–6, so they're deliberately not
pre-chunked here before there's a working local container stack to build
them on top of.

---

## 14. Local/cloud parity: the plumbing (2026-10-17)

**Core principle: one image, config varies by environment.** This is
already the shape the app is in — `Settings` (pydantic-settings) already
reads everything from env vars, so nothing containerized needs
rearchitecting, just different *values* fed into the same settings across
three contexts.

| Setting | Today (bare-metal) | Local (`docker compose`) | Cloud Run (GCP) |
|---|---|---|---|
| `database_url` | `postgresql://localhost:5432/acamar` | `postgresql://...@postgres:5432/acamar` (compose service name) | Unix socket at `/cloudsql/PROJECT:REGION:INSTANCE`, via `--add-cloudsql-instances` — not a TCP host:port |
| `ollama_base_url` | `http://localhost:11434` | `http://host.docker.internal:11434` — reaches the Mac's own Ollama.app from inside a container | Internal URL of the Cloud Run GPU Ollama service (§9), over a VPC connector |
| `api_base_url` (what the dashboards call) | `http://localhost:8000` | `http://api:8000` (compose service name) | the API's Cloud Run service URL |
| Secrets (`gmail_*`, `cockpit_token`, DB password) | `.env` + `secrets/*.json` on disk | same files, bind-mounted into the containers at the same relative path — no code change, `Settings`' default paths already point there | Secret Manager, bound as env vars or mounted secret volumes — `Settings` doesn't know the difference either way |

**Two pieces that are different in kind, not just a different hostname —
flagged plainly so they aren't discovered mid-build:**

1. **Database transport.** Local is a normal TCP connection. Cloud
   Run↔Cloud SQL is a Unix domain socket Cloud Run provisions for you via
   `--add-cloudsql-instances` — auto-encrypted, no separate proxy process
   to run yourself (confirmed current as of October 2026: [Cloud SQL →
   Cloud Run connection docs](https://docs.cloud.google.com/sql/docs/postgres/connect-run)).
   The `DATABASE_URL` DSN shape itself changes (a `?host=/cloudsql/...`
   query param, not a `host:port` pair) — worth confirming the exact
   query-param syntax against whatever this project's `adapters/db` layer's
   Postgres client expects before wiring it in phase 3, not assumed from
   the socket path alone.
2. **Ollama reachability.** **Don't containerize Ollama for local dev** —
   Docker Desktop on Mac can't pass Metal GPU acceleration through to a
   container, so putting Ollama in `docker-compose.yml` would silently lose
   the ~19x speedup this project already fought for (§1). Ollama stays
   exactly as it runs today — the native Ollama.app, outside Docker
   entirely — and the containerized API/ingestion just reach *out* to it
   via `host.docker.internal`, same as they reach it today with no Docker
   in the picture at all. In the cloud, the equivalent role is played by
   the separate Cloud Run GPU service from §9 — a structurally different
   arrangement (one process the containers call out to vs. a peer service),
   not just a different URL plugged into the same shape.

**Build shape**: one shared base image (the `uv sync` dependency layer,
identical for API/dashboard/cockpit since they share one `pyproject.toml`)
with four thin entrypoints, rather than four fully independent Dockerfiles
each repeating the dependency install — less to rebuild, no drift between
them. `docker compose` builds from source locally for fast iteration; the
same Dockerfiles get built once and pushed to Artifact Registry for the
Cloud Run deploy in §11 phase 4 — never two different build definitions
for "local" vs. "cloud."

**New file this adds to phase 1** (§13): a docker-specific env file
(`.env.docker`, or inlined in compose's own `environment:` block) separate
from the existing `.env` — `database_url`/`ollama_base_url`/`api_base_url`
genuinely need different values inside the compose network than bare-metal
`.env` has today, so it can't be reused unchanged. Everything else in
`.env` (model names, feature flags, tokens) carries over as-is.
