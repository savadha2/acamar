# GCP deployment sprint — Oct 17 → Oct 22 (2026-10-17)

Status: **execution plan, building now.** Deadline: live and verified on
GCP by Thu Oct 22 evening (Sat Oct 17 → Thu Oct 22 = 6 calendar days, ~5
full working days). Builds directly on
[docs/gcp-aws-deployment-proposal.md](gcp-aws-deployment-proposal.md) — no
new architecture decisions here, just sequencing an already-reviewed plan
against a hard date.

**Explicit requirement carried through every day below**: the local
bare-metal setup and the new `docker compose` stack stay fully intact and
usable for dev/test, before, during, and after this sprint. Nothing here
decommissions local anything — that's proposal §11 phase 9, deliberately
*not* in scope for Oct 22. "Ready" means the cloud stack is live and
verified, not that local is torn down.

---

## Proceeding on the stated recommendations, given the deadline

Proposal §12 left 6 decisions formally open, each with a recommendation
already written down. Re-litigating six small forks eats a day this sprint
doesn't have — **this plan proceeds on every stated recommendation by
default**: Cloud SQL (not AlloyDB), kill-switch rebuild on the Cloud Run
Admin API (not scope-reduce), numbered-SQL migrations (not Alembic),
GitHub Actions (not Cloud Build), auto-deploy on merge to `main` (no
staging gate), no specific cost ceiling set. Say so now if any of these
should flip — otherwise this is the plan.

## GPU quota — corrected 2026-10-17, smaller risk than first stated

Originally flagged as the sprint's single biggest risk, requiring a Day 0
manual request. **Corrected after checking Google's own current docs**: a
project's first Cloud Run GPU deployment auto-grants 3 L4 GPUs (no zonal
redundancy) with no request needed — this project only ever needs 1 at a
time. No Day 0 action required; just confirm the auto-grant actually lands
when Ollama is first deployed (Day 3), and only fall back to a manual
quota-increase request (IAM & Admin → Quotas & System Limits) if that
somehow doesn't happen. The explicit fallback below still stands as a
safety net, just a less likely one to need.

## Project: reuse `acamar-508716`, don't create a new one

Pulled from the existing `secrets/gmail_credentials.json`: the Gmail OAuth
client already lives in GCP project **`acamar-508716`**. Recommended:
**reuse this project for the whole deployment** rather than creating a
fresh `acamar-prod` — one project, billing almost certainly already
linked (the Gmail flow has been working), no cross-project IAM/secrets
wiring to set up. §13's project-groundwork commands below target this
project accordingly.

---

## Day 0 — Sat Oct 17 (today, remaining hours)

Zero-dependency, longest-lead-time items first, in parallel:

**You — account/console actions, can't be done from here:**
- ~~Install the `gcloud` CLI~~ **Done 2026-10-17, via the official
  installer, not Homebrew.** `brew install --cask gcloud-cli` hit a real
  blocker (`Unexpected method 'preflight_steps'` — stale local Homebrew
  tap metadata; `brew update` itself was blocked by a shallow git clone
  that would have taken several more minutes to unshallow). Switched to
  Google's own installer (`curl https://sdk.cloud.google.com | bash`,
  non-interactive) instead of spending more sprint time on Homebrew's tap
  health — installed clean to `~/google-cloud-sdk`, confirmed via
  `gcloud version` (589.0.0). One sub-step failed harmlessly (a separate
  system Python 3.14 installer needing `sudo`, not gcloud's own bundled
  Python, which installed fine and is what gcloud actually runs on).
  PATH/completion sourcing added to `~/.zshrc`. **Still needed: open a new
  terminal (or `source ~/.zshrc`) then `gcloud auth login`** — can't
  authenticate as you from here.
- Confirm billing is linked to `acamar-508716` (the existing project,
  reused per above) and enable 9 APIs on it (run/sqladmin/secretmanager/
  artifactregistry/cloudscheduler/compute/iam, plus **dns** and
  **vpcaccess** — added after review, needed Day 3 for Direct VPC egress +
  the private DNS zone to the internal Ollama service, missed in the
  first pass).
- Flip the Gmail OAuth consent screen Testing → Production (item 49), in
  `acamar-508716` specifically. **Corrected navigation, verified**: it's
  **Google Auth Platform → Audience** in the current console (the
  "APIs & Services → OAuth consent screen" page has been restructured) —
  click **Publish app**, confirm the **"Push to Production?"** dialog.
  Google's own docs confirm the payoff directly: *"you will not be
  required to reauthorize the connection weekly."*
- GPU quota: no action today — see the corrected note above.

**Me — pure local files, no cloud dependency. Done 2026-10-17, verified by
actually running it, not just writing it:**
- `Dockerfile` (multi-stage: `base` + `api`/`dashboard`/`cockpit`/`ingestion`
  targets), `docker-compose.yml`, `.dockerignore`, `.env.docker` (§13/§14).

**Three real bugs found and fixed by actually building and running this,
not caught by review alone:**
1. `uv sync` ran *before* `COPY src/`, so the `acamar` package's
   third-party deps installed fine but `acamar` itself never did —
   `ModuleNotFoundError` at runtime despite a clean build. Fixed with the
   standard two-phase pattern: `uv sync --no-install-project` on the
   manifest-only layer (cacheable), a second `uv sync` after `COPY src/`.
2. That second sync's default **editable** install left a dist-info with
   an empty `RECORD` and no `.pth`/finder file — "installed" by every
   metadata check, still `ModuleNotFoundError`. Fixed with `--no-editable`
   — also the more correct choice for a container image generally (a
   sealed artifact, not a link back to a mutable source tree).
3. `docker-compose.yml` originally reused ports 8000/8501/8502 — collided
   with the bare-metal stack the moment both were actually up at once,
   directly breaking this doc's own "retain local setup" requirement.
   Remapped to 18000/18501/18502 (Postgres was already on 5433, done right
   the first time — the other three weren't, caught by a real bind error,
   not anticipated).

**End-of-day checkpoint — met**: `docker compose up -d postgres api
dashboard cockpit` + `docker compose run --rm seed`, then real checks:
`/health` 200, dashboard 200, cockpit 200, and one real ingestion run
against the live `faria.cusdk8.org` + the native Ollama (reached via
`host.docker.internal`) — 6 fetched, 6 upserted, zero errors. Access the
dockerized stack at `localhost:18000`/`18501`/`18502`; bare-metal keeps
its original ports — both run simultaneously now, confirmed, not assumed.

## Day 1 — Sun Oct 18

**Me:**
- Numbered-SQL migration runner (§8) — `db/migrations/0001_initial.sql`
  (today's `schema.sql`, as a migration) + the tiny runner script.
- Re-run `scripts/gmail_auth.py` once (confirming item 49's fix actually
  took — token issued under the new policy, not just the Console flip).

**You:**
- Provision Cloud SQL for PostgreSQL (smallest viable tier) — command
  provided when we get here; confirm the HNSW-memory caveat from §3
  against the real tier spec before committing to it.
- Check GPU quota status; follow up if still pending.

**End-of-day checkpoint**: Cloud SQL live, schema applied via the new
runner, confirmed empty-but-ready (`\dt`, `\dx` against the real instance).

**Run one calendar day late (2026-10-19), against this slot — five more
real bugs found and fixed by actually provisioning this, not caught by
review alone:**
1. `db-g1-small`/`db-f1-micro` are **Enterprise**-edition-only machine
   types; this project's account defaults new Cloud SQL instances to
   **Enterprise Plus**, which rejects them outright. Fixed with an
   explicit `--edition=enterprise` on `gcloud sql instances create`.
2. `--no-assign-ip` alone leaves an instance with **no** connectivity
   path at all — it only disables the public IP, it doesn't enable
   private IP or PSC in its place. Fixed by dropping the flag: a public
   IP is safe here with no `--authorized-networks` added, since the
   Cloud SQL Auth Proxy authenticates via IAM/mutual TLS through the
   Cloud SQL Admin API, not via network-level IP exposure.
3. The Cloud SQL Auth Proxy's suggested download URL
   (`.../v2/latest/cloud-sql-proxy.darwin.arm64`) doesn't exist on the
   bucket — `latest` isn't a real path there. Fixed with a real pinned
   version (`v2.26.2`), confirmed live (HTTP 200) before handing it over
   a second time.
4. The proxy needs **Application Default Credentials**, a separate local
   credential from the `gcloud auth login` already done on Day 0 —
   failed with "could not find default credentials" until
   `gcloud auth application-default login` was run once. Also needed by
   other Google client libraries going forward, not just this proxy.
5. The real time-sink: the proxy's chosen local port (5433) collided
   with the **already-running Day 0 `docker compose` Postgres
   container** (`acamar-postgres-1`, up 46 hours, same host port) —
   every migration attempt was silently hitting local Docker's fixed
   `acamar`/`acamar` credentials instead of Cloud SQL, surfacing as
   repeated "password authentication failed" no matter how carefully the
   real Cloud SQL password was regenerated and matched. Not a password
   bug at all — fixed by moving the proxy to a genuinely free port
   (5434). A reminder that "retain the local stack" (this sprint's own
   explicit requirement) means its ports stay live and have to be
   designed around, not just its processes.

**End-of-day checkpoint — met**, confirmed against the real instance:
```
 public | documents         | table | acamar
 public | schema_migrations | table | acamar
 public | schools           | table | acamar
 public | sources           | table | acamar
 public | subscribers       | table | acamar

 vector    | 0.8.5 | public | vector data type and ivfflat and hnsw access methods
 uuid-ossp | 1.1   | public | generate universally unique identifiers (UUIDs)
```

## Day 2 — Mon Oct 19

**Me:**
- GitHub Actions workflow (§7): test gate (ruff/mypy/pytest) → build all 4
  targets → SHA-tag → push to Artifact Registry → deploy.

**You:**
- Workload Identity Federation setup (GitHub Actions ↔ GCP, no long-lived
  key) — exact steps provided when we get here. **Time-box this**: if it's
  eating the afternoon, the explicit fallback is a scoped service-account
  JSON key in GitHub Secrets now, swapped for WIF later — flagged as a
  deliberate tradeoff if taken, not a silent shortcut.
- Secret Manager: create the 4 real secrets (`gmail_credentials.json`,
  `gmail_token.json`, `cockpit_token`, DB password).

**End-of-day checkpoint**: one CI run goes green end to end — tests pass,
image builds, pushes to Artifact Registry. A manual `gcloud run deploy` is
an acceptable stand-in if CI's own deploy step isn't trusted yet.

**Run 2026-10-23/24 (one day past the original Oct 22 deadline — noted,
not hidden; sequencing continued anyway):** all of steps 1–5 done and
verified live, not assumed:
1. Artifact Registry repo (`acamar`, `us-central1`) — already existed by
   the time this was checked.
2. WIF pool (`github-pool`) + OIDC provider (`github-provider`, attribute-
   condition restricted to `savadha2/acamar`) + dedicated `acamar-deployer`
   SA + the impersonation binding — all confirmed live via `gcloud`
   describe/list calls, not just "command ran with no error."
3. `acamar-deployer` granted `roles/artifactregistry.writer` (scoped to
   the `acamar` repo) and `roles/run.developer` (project-wide for
   now — the services/jobs it would otherwise scope to don't exist until
   Day 3/4; revisit tightening this once they do).
4. The 4 Secret Manager secrets created — **caught one real gap**:
   `cockpit-token` came up with zero versions (an empty container, no
   payload ever attached) while the other three correctly showed one
   `enabled` version each. Fixed with `gcloud secrets versions add`
   piping the real value straight from `.env`, confirmed after.
5. `.github/workflows/deploy.yml` written (test gate → build all 4
   targets → SHA-tag → push → deploy services → update ingestion jobs)
   and committed locally (`4afb430`, bundled with a `.gitignore` fix to
   keep the downloaded `cloud-sql-proxy` binary out of git) — **not yet
   pushed**, so the first real CI run hasn't fired yet.

## Day 3 — Tue Oct 20

**You:**
- Deploy Cloud Run: `api`, `dashboard`, `cockpit`, pointed at Cloud SQL via
  the Unix-socket DSN (§14 — confirm the exact query-param syntax against
  this project's actual Postgres client here, not assumed).

**Me:**
- Verify `/health`, both Streamlit apps, against the real cloud stack.
- **If GPU quota has cleared**: deploy Ollama on Cloud Run GPU, internal
  ingress; set up the private DNS zone + Direct VPC egress for the API's
  path to it (§4/§9's corrected networking detail — the part most likely
  to need a second pass).
- **If GPU quota has NOT cleared** (the explicit fallback, not a silent
  slip): keep the cloud API pointed at local Ollama over a tunnel for now
  — proposal §11 phase 4 already allows decoupling this step — or stand up
  a temporary CPU-only Ollama on Cloud Run, swapped for the GPU version the
  moment quota clears. Either way, Oct 22 is still hittable; only the LLM
  latency during the gap is affected, not whether the rest ships.

**End-of-day checkpoint**: `/query` works end-to-end against the real
cloud API + Cloud SQL + (GPU or interim) Ollama.

## Day 4 — Wed Oct 21

**You:**
- Cloud Scheduler + Cloud Run Jobs for both ingestion jobs (6×/day,
  1×/day), mirroring the two real `launchd` plists exactly.
- Trigger one run manually (`gcloud scheduler jobs run ...`) rather than
  waiting for the next real firing to confirm it works.

**Me:**
- Kill-switch rebuild (§4): the Cockpit's dedicated least-privilege SA,
  the custom IAM role, the Cloud Run Admin API call itself.

**You:**
- Test the kill switch and pause/resume against the real cloud deployment
  — confirm it behaves as a real, hard-to-accidentally-undo stop before
  trusting it the way the local version was trusted.

**End-of-day checkpoint**: ingestion running on the real cloud schedule;
kill switch verified safe, not just deployed.

## Day 5 — Thu Oct 22 (deadline day)

- Full end-to-end verification pass against the live cloud stack —
  dashboard, cockpit, `/query`, both ingestion crons, kill switch,
  pause/resume. Nothing assumed working because an earlier day's checkpoint
  passed once.
- Most of the day reserved as buffer/fix time, not new scope — first real
  infra builds almost always need at least one rough patch; better to plan
  for it than discover it at 6pm.
- **Explicitly not done today**: decommissioning local (§11 phase 9).
  Bare-metal and `docker compose` both stay fully available as the
  standing dev/test environment, per the requirement at the top of this
  doc.
- End of day: plain go/no-go — what's verified live, what (if anything)
  is still running on the interim Ollama fallback, what's deferred past
  Oct 22 with an honest reason why.

---

## What this sprint deliberately does not attempt by Oct 22

Full Google OAuth app verification (optional polish per item 49's
correction — not needed for the 7-day cap itself); a cost ceiling pass
against the real GCP pricing calculator (decision 6, still open); DNS/a
custom domain for the Cloud Run URLs (never scoped anywhere in the
proposal); decommissioning anything local. None of these block "live and
verified" — they're real follow-ups, not silently dropped.
