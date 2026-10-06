# RootPulse — Backend

Django 5 + Django REST Framework, Python. See [`docs/plan/`](./docs/plan/) for the full system plan (architecture, database, API, monitoring engine, security, testing/deployment, and the 19-phase roadmap).

## Getting started

```bash
cp .env.example .env               # fill in local secrets, or point DATABASE_URL at Neon
docker compose up -d db            # local Postgres (only needed if NOT using DATABASE_URL)
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver          # API on :8000
```

`config/settings.py` uses `DATABASE_URL` when it's set (Neon, and Render in production) and falls back to the local docker-compose Postgres otherwise — so local dev works either way.

Scheduler and notification-dispatcher management commands (`run_scheduler_once`, `run_notifications_once`) are added in Phase 6/8 — not present yet. They're single-pass commands (not persistent processes) invoked via a signed internal endpoint on a GitHub Actions cron schedule — see `docs/plan/01-tech-stack.md` and `docs/plan/03-monitoring-engine.md` for why (short version: Render's free tier has no always-on worker).

## Scripts

| Command | What it does |
| --- | --- |
| `python manage.py runserver` | Dev server |
| `python manage.py check` | Django system checks |
| `ruff check .` | Lint |
| `black .` / `black --check .` | Format / check |
| `python -m pytest` | Test suite |

## Authentication (Phase 3)

Endpoints under `/api/v1/auth/`: `register/`, `login/` (each emails a link + 6-digit code), `verify/` (redeems either), `logout/`, `refresh/`, `me/` (GET/PATCH), `github/redirect/`, `github/callback/`. Interactive docs at `/api/v1/docs/`, schema at `/api/v1/schema/`.

- Access JWT (15 min) is returned in the body; refresh JWT (30 days) is an httpOnly cookie scoped to `/api/v1/auth/`, rotated and blacklisted on every use. In production it is `SameSite=None; Secure` (Vercel and Render are different sites), so cookie-authenticated endpoints also check the `Origin` header against `CORS_ALLOWED_ORIGINS` as CSRF defence.
- **Passwordless.** There are no user passwords. `register/` and `login/` always answer 204 (no account enumeration) and email a single-use link plus a 6-digit code; either one signs in, and the account is created on first successful verify. Challenges expire after 15 minutes, are stored only as hashes, are superseded by the newest email, allow one email per address per minute, and the code is locked after 5 wrong guesses. The link carries its token in the URL fragment and is redeemed by `POST verify/`, so mail scanners can't consume it. Per-IP rate limits honour `NUM_PROXIES`.
- GitHub OAuth only trusts verified emails and uses a `state` cookie against login-CSRF. Leave `GITHUB_OAUTH_CLIENT_ID` blank to disable it.
- Sign-in emails go over SMTP when `EMAIL_HOST` is set (see `.env.example` for Gmail and Resend settings) and print in the server console otherwise. Check delivery with `python manage.py sendtestemail you@example.com`. Expired challenge rows are not yet purged; a cleanup job is added with the Phase 6 scheduler.
- Tests: `python -m pytest` (66 tests; sqlite in-memory, or `TEST_DATABASE=postgres` as CI does).

## Notes on this environment's setup

- **Local dev runs on Python 3.14** (whatever was installed on this machine) — confirmed working with Django 5.1 and every package in `requirements.txt`. **CI pins Python 3.12** deliberately (Django 5.1's officially supported range), since a bleeding-edge interpreter is fine for local iteration but not the right reproducibility target for CI/production — this is an intentional mismatch, not an oversight.
- Custom user model (`accounts.User`) is in place from the first migration, per `docs/plan/01-tech-stack.md` — never retrofitted later. **Confirmed live on the real Neon database.**
- **Database: Neon, already provisioned and migrated.** `.env` (gitignored, never committed) holds the real `DATABASE_URL`.
- **Hosting: Render (API) + Vercel (frontend)**, not Fly.io as originally planned — Render's free tier can't run an always-on scheduler, so the scheduler/notification passes are invoked by a GitHub Actions cron trigger instead (also doubles as a keep-alive so the free web service doesn't sleep). Full reasoning in `docs/plan/01-tech-stack.md`. `Dockerfile` + `render.yaml` are written and ready, not yet deployed.
- **Multi-location probers move to Cloudflare Workers** (not Fly.io regions) — no sleep/cold-start, and it turns out neither Render nor Cloudflare Workers can do true ICMP ping (no raw sockets on either platform), so "Ping" monitoring is implemented as a TCP connect instead — see `docs/plan/03-monitoring-engine.md` §13.
- **Local DNS resolution to Neon can be flaky in this environment** (IPv6 lookups time out before falling back to IPv4, and occasionally fail outright) — a retry usually works; this is a local-sandbox quirk, not a Neon or code problem, and won't affect Render's Linux containers in production.

## What needs you (account creation this session won't do on its own)

Per this session's own safety rules, I don't create accounts on third-party services. These are ready to wire up as soon as you've done the account-creation step yourself:

| Service | What it's for | Status |
| --- | --- | --- |
| **Neon** | Postgres hosting | ✅ Done — connection string in `.env`, migrated |
| **Render** | API hosting | Create a free account, connect the `RootPulse-backend` repo (or give me an API key) — `render.yaml` + `Dockerfile` are ready |
| **Cloudflare** | Worker probers (Phase 6) | Create a free account for Workers |
| **Vercel** | Frontend hosting | Create a free account, connect the `RootPulse-frontend` repo |
| **Resend** (or any SMTP) | Alert and sign-in email in production | Create a free account; put the SMTP settings in the host's environment. Locally the `.env` SMTP settings already send real mail |
| **Telegram bot** | One-click Telegram (Phase 8) | Create one bot with @BotFather; set `TELEGRAM_BOT_TOKEN` (the rest is automatic) |
| **Discord app** | One-click Discord | Create one application at discord.com/developers; set `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` and the redirect URL |
| **Slack app** | One-click Slack | Create one app at api.slack.com/apps; set `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` and the redirect URL (https only) |
| **VAPID keys** | Browser push (Phase 8) | Run `python manage.py generate_vapid_keys` and put both values in `.env` / the host's environment |
| **Sentry** | Error tracking | Create a free account + project |
| **GitHub** | Remote repos + CI/CD + the scheduler-trigger cron workflow (Phase 6) | Create the `RootPulse-backend`/`RootPulse-frontend` repos, push these local commits, add secrets for the CI/cron workflows |

Once any of these exist, tell me and I'll wire up the corresponding config/secrets immediately.

## Monitors (Phase 5)

`/api/v1/monitors/`: list (`q`, `status`, `type`, `sort`, `page`, `page_size`), create, retrieve, PATCH (no PUT), delete, `POST {id}/pause/` and `{id}/resume/`. Everything is scoped to the signed-in user (someone else's monitor is a 404) and capped at `MAX_MONITORS_PER_USER` (50).

- **Eight types**, one table (`monitoring/types.py`) driving validation, defaults and the OpenAPI schema, where `config` is a typed `oneOf`. Check intervals are 5 minutes to 24 hours (SSL and domain: hourly or slower). A monitor's type can't change after creation. New monitors are `pending` with `next_check_at` set (a heartbeat's is its first expected ping plus grace); paused monitors have no `next_check_at`, which a database constraint enforces. Nothing checks them until Phase 6.
- **SSRF** (`monitoring/target_validation.py`): names are resolved and the *addresses* judged, so loopback, private, link-local (cloud metadata), reserved and IPv6-embedded (mapped, NAT64, 6to4) addresses are refused, as are credentials in URLs, non-http(s) schemes, and single-label or `.local`/`.internal` names. Unresolvable names are allowed at creation (the site may not be live yet); `resolve_public_ips()` is the check-time entry point that re-validates and returns IPs to pin the connection to. `tests/fixtures/ssrf_cases.json` is the shared list the Cloudflare Worker implementation will also be tested against.
- Heartbeat monitors get a secret URL (`/api/v1/heartbeat/{token}/`); receiving pings is added with the engine.
- `AlertContact`, `MonitorAlertContact` and `MaintenanceWindow` models exist (admin only for now); their endpoints come with Phases 8 and 6.
- Validation errors are flattened to dotted field paths (`config.timeout_seconds`). 271 tests.

## Monitoring engine (Phase 6)

Monitors are checked by **scheduler passes**. A pass claims everything that is due from Postgres (`select_for_update(skip_locked=True)`, so two overlapping passes can never claim the same monitor), pushes each claimed monitor's `next_check_at` one interval ahead, then runs the checks in a bounded thread pool (`CHECK_WORKERS`, default 20) within a time budget (`CHECK_PASS_BUDGET_SECONDS`, default 80). No lock is held during network calls.

**Running it locally** (nothing runs the checks until you do):

```bash
python manage.py run_scheduler_once              # one pass
python manage.py run_scheduler_once --loop 60    # a pass every minute, until Ctrl+C
```

**In production** the pass is triggered every five minutes by `.github/workflows/scheduler.yml`, which calls `POST /internal/run-due-checks/` with an HMAC signature (`X-RootPulse-Signature`, over a fresh timestamp, so captures can't be replayed). It also keeps Render's free service awake. Set up:

1. Render generates `SCHEDULER_SHARED_SECRET`; copy its value into a GitHub Actions **secret** of the same name, and add an `API_URL` secret (the API's public URL).
2. Optional: deploy the Cloudflare Worker probers (`workers/prober/README.md`) and set `PROBER_URLS` and `PROBER_SHARED_SECRET` so a failure is confirmed from several regions. Without probers a failure is re-checked from the API after a short delay.

What each type does:

| Type | Check |
| --- | --- |
| HTTP, keyword | Resolves the host, **connects to the vetted IP** (never re-resolving), re-vets every redirect, captures a `dns/tcp/tls/ttfb` breakdown, caps the body at 1 MB, enforces the configured timeout. Keyword matching is case-sensitive. |
| Ping, port | TCP connect (Render and Workers can't send ICMP). |
| SSL | Real TLS handshake with chain and host-name verification; days until expiry vs `warn_days`. |
| Domain | RDAP lookup (IANA bootstrap). A TLD without RDAP or an unreadable answer is *inconclusive*, never "down". |
| DNS | Resolves the chosen record types; the first check records a baseline, a change fails once and becomes the new baseline. |
| Heartbeat | No outbound request: the job pings `/api/v1/heartbeat/{token}/`; late = last ping + interval + grace. |

**Status rules.** A monitor goes *down* only after a failure is **confirmed**: two regions must agree (the API plus at least one prober), or, with no probers, a re-check from the API also fails. A failure that isn't confirmed is a *blip*: logged, no status change. A monitor that is already down isn't re-confirmed. **Inconclusive** results (a blocked target, an unreadable lookup, a crashed check) are recorded but never change status. SSL re-checks locally; domain, DNS and heartbeat failures are deterministic and confirmed immediately. Status changes call `monitoring/hooks.py:status_changed`, which incidents and notifications (Phases 7 and 8) hook into.

`GET /api/v1/monitors/{id}/checks/` is cursor-paginated (`since`, `until`, `region`, `confirmation`), and the monitor detail includes a 24-hour `summary` (uptime, average and p95 response time). Raw checks are only read over short windows; long-range analytics will come from rollups (Phase 12), and a retention sweep for checks older than 90 days arrives with them.

Tests: `python -m pytest` (432). The real row-lock race and the thread pool run only on Postgres (`TEST_DATABASE=postgres`, which CI uses); the Worker's 26 tests run with `npm test` in `workers/prober`.

## Incidents (Phase 7)

An **incident** is a confirmed outage. `incidents/services.py:reconcile` runs inside the same transaction as every status change, so **a monitor is down exactly when it has one open incident**:

- It opens when a failure is confirmed (the engine's two-region or re-check rule) and closes on the next successful check ("recovered"). It also closes if the monitor is **paused** (nothing can observe the outage any more) or **edited** (the old verdict no longer applies). There is deliberately no manual close.
- **One open incident per monitor is a database constraint** (a partial unique index), not just application logic, so a flaky network, several regions agreeing, or a retried pass can't create duplicates. If opening or closing the incident fails, the status change rolls back with it and the next pass retries.
- It self-heals: a monitor that is down with no incident (for example from before incidents existed) gets one on its next failing check, and a stale open incident on an up monitor is closed by the next success.
- Timeline events (`opened`, `recheck_confirmed`, `resolved`, `closed_paused`, `closed_edited`, `excluded`, `included`) are written by the system; people add **comments** (private by default, with a flag for the future public status page).

API under `/api/v1/incidents/`: list (`status`, `monitor_id`, `start_date`/`end_date` read in the account's time zone, `excluded`, `sort` incl. by duration), detail (timeline, comments, the check that opened it), `PATCH` (exclude from reports), comments (`POST`, and `PATCH`/`DELETE` on your own), `POST {id}/postmortem/` (a pre-filled Markdown draft, once resolved), and `GET export/` (CSV with the same filters; cells that start with `=`, `+`, `-` or `@` are neutralised so a spreadsheet can't run them). `CORS_EXPOSE_HEADERS` includes `Content-Disposition` so the frontend can read the download's filename. Incidents can't be created or deleted through the API.

Tests: 490 at that point, including the full failure, confirm, incident, recovery state machine, a real unstable local HTTP server (one incident across several broken passes; a single flaky response opens none), and the constraint on real Postgres (`TEST_DATABASE=postgres`). Root-cause stage and deploy linking arrive in Phase 10.

## Notifications (Phase 8)

An incident event becomes a row in `NotificationDelivery`, which doubles as the **send queue** and the **history**: `run_notifications_once` claims due rows with `select_for_update(skip_locked)` (the same pattern as the scheduler), sends outside any lock, and records what happened. The same signed `/internal/run-due-checks/` trigger runs checks and then alerts, so production needs no extra cron. Locally, `python manage.py run_engine --loop 60` does both.

**Who hears what, and when** (`notifications/services.py` is the one place these rules live):

- An incident **opening** alerts every *enabled, confirmed* contact attached to the monitor, after that contact's **delay** (0 to 1 hour), and only if the monitor is *still* down then. A blip shorter than the delay sends nothing.
- **Recovery** is announced only to contacts who were actually told it went down. If the down-alert hadn't gone out yet (still inside the delay, or failing), it is cancelled instead: nobody gets an "all clear" for an alarm they never heard.
- While an incident stays open, a contact with a **reminder interval** (5 minutes to 24 hours) is reminded, at most `NOTIFY_MAX_REMINDERS` (24) times.
- **Maintenance windows** silence everything: the scheduler doesn't check those monitors, an incident that opens inside one creates only *skipped* rows, and a window that starts before a queued alert goes out suppresses it at send time.
- Pausing or editing a monitor cancels alerts still waiting. Creation is idempotent (`dedupe_key` plus a unique constraint), so a retried pass can never alert twice.
- Failures are classified. A temporary one (timeout, 5xx, 429 with the provider's own `Retry-After`) is retried after 1, 5, 15, 15 minutes, up to `NOTIFY_MAX_ATTEMPTS` (5); a permanent one (bad credentials, deleted webhook, refused address) fails at once with a message a person can act on. A crashed adapter is retried, never lost. A claimed row that is never finished (the process died) becomes due again after a two-minute lease. Every send and failure is also written to the incident's timeline.

**Six channels** (`notifications/channels.py`), each validating its own config when it is saved so a typo is caught then, not at 3 a.m.:

| Channel | Notes |
| --- | --- |
| Email | An address other than the account's must be confirmed first (a 6-digit code emailed there: 30 minutes, 5 tries), so RootPulse can't be pointed at a stranger's inbox. Subjects and bodies are HTML-escaped. |
| Webhook | A JSON POST signed with an HMAC-SHA256 secret that RootPulse generates (`X-RootPulse-Signature: t=..,v1=..`, the same scheme as the internal endpoints, with `X-RootPulse-Event` and `X-RootPulse-Delivery`). The secret is shown **once** (create or rotate) and never returned again. The URL is user-supplied, so sends use `safe_post`: the name is resolved and judged, the connection is pinned to the vetted IP, redirects are never followed, and the config is SSRF-checked when saved. |
| Slack | Only `hooks.slack.com` webhook URLs. Names and messages are escaped (`<!channel>` can't ping anyone). |
| Discord | Only `discord.com` webhook URLs, with `allowed_mentions` off (`@everyone` can't ping the server). |
| Telegram | Your own bot token plus a chat id. `POST /alert-contacts/telegram-chats/` lists the chats that have messaged the bot, so nobody has to hunt for a numeric id. |
| Web push | VAPID via `pywebpush`. Subscriptions are accepted only for the real push services (FCM, Mozilla, Windows, Apple). An expired subscription (404/410) switches the contact off. |

Secrets (webhook URLs, bot tokens, push keys) are masked in every response and never appear in error messages, logs or the delivery history.

API: `/api/v1/alert-contacts/` (CRUD, `test`, `verify`, `resend-verification`, `rotate-secret`, `telegram-chats`; capped at 20 per user), `/api/v1/notifications/` (delivery history, filterable by status, monitor, contact, incident), `/api/v1/notifications/vapid-key/`, `/api/v1/maintenance-windows/` (CRUD, `?state=`), `POST /api/v1/monitors/{id}/test-notification/`, and monitors take `alert_contact_ids` (omit it on create to use all your contacts). Incident detail lists its `notifications`.

### One-click connections

Pasting webhook URLs and bot tokens is how developers connect things, so Telegram, Discord and Slack also have a **Connect** button (`notifications/integrations.py`). Each is optional and appears only when the server has its credentials; the paste-it-yourself route always remains.

- **Telegram** uses *one* bot owned by the deployment. `POST /integrations/telegram/start/` returns a `t.me` deep link carrying a random one-time token (only its hash is stored; it works once and expires in 15 minutes). When the person presses Start, the bot receives `/start <token>` *from their chat*, which is how we know whose chat it is, and a contact is created and attached to their existing monitors. `GET /integrations/telegram/links/{id}/` is what the page polls. In development the server polls Telegram itself (`run_engine` and that status call); in production Telegram posts updates to `/integrations/telegram/webhook/`, gated by a shared secret (`manage.py telegram_webhook set|info|delete`). The deep link also works for groups (`startgroup`). A chat connected this way stores no token (it is sent by the deployment's bot), and the API refuses to let anyone create or repoint such a contact by hand, so the bot can't be aimed at arbitrary chat ids.
- **Discord and Slack** use OAuth. `POST /integrations/{discord|slack}/start/` returns the provider's authorize URL with a signed, 10-minute `state`; the provider redirects the browser to `/integrations/{provider}/callback/`, which verifies the state, exchanges the code for an incoming-webhook URL, validates it with the same rules as a typed one, creates the contact and redirects to `/settings/notifications?connected=...` (or `?connect_error=...` with a short code, never provider text). The webhook URL and the OAuth code never appear in redirects or logs.
- Discord and Slack apps are created once by whoever runs the deployment: see the variables in `.env.example`. Register `<API_PUBLIC_URL>/api/v1/integrations/<provider>/callback/` as the redirect URL (Slack requires https, so Slack connect works once the API is deployed or behind an https tunnel).

Not built yet: recurring maintenance windows (`recurrence_rule` is stored but ignored), and per-severity routing.

Tests: 726 in total (the thread-pool dispatch test runs only on Postgres).

## Analytics (Phase 9)

Dashboards never read raw checks (a 5-minute monitor from three regions writes ~860 a day). `analytics/` keeps three pre-aggregated tables, all UTC:

| Table | Grain | Built from |
| --- | --- | --- |
| `CheckRollupHourly` | monitor x hour | raw scheduled checks (re-checks that confirm a failure are ignored) |
| `CheckRollupDaily` | monitor x day | the hourly rows, not raw checks |
| `FleetSummaryDaily` | user x day | the monitors' daily rows plus their incidents |

Each row holds up/down/inconclusive counts, response-time statistics (count, sum, min, max, p50, p95 for successful checks) and the DNS/connection/TLS/server timing sums. **Every row is recomputed from its source and replaced, never added to**, so running the job twice (or overlapping with itself, or retrying) can't double count. That is tested directly: the same pass repeated leaves every table identical.

- **Running it:** `python manage.py run_rollups_once` (`--full` rebuilds everything from each monitor's first check, `--no-prune` keeps old raw checks). In production the signed `POST /internal/run-rollups/` is called hourly by `.github/workflows/rollups.yml` (same secrets as the scheduler); locally `run_engine --loop` does it every ten minutes. One monitor failing doesn't stop the others.
- **Retention:** raw checks older than `CHECK_RETENTION_DAYS` (90) are deleted *after* being folded in, and only for monitors whose rollup succeeded. Old hours are never recomputed from raw rows that may be gone, so history keeps its rollups.
- **Honest uptime:** uptime is the share of conclusive checks that passed. Inconclusive checks (a problem on our side) never lower it, and an hour with no checks is "no data", not 100%. **Failures inside an incident you excluded from reports are left out of both sides**; toggling the exclusion recomputes the hours it covers, so the numbers follow immediately.
- **Percentiles:** exact for an hour. A day, and any longer range, combines the hourly p50/p95 by a response-weighted mean, so they are close rather than exact (the UI says so).
- **Incident figures** (count, downtime, MTTR, longest) come straight from incidents, which are few: downtime is clipped to the range, ongoing incidents count up to now, excluded ones are left out and counted separately.

API: `GET /api/v1/monitors/{id}/analytics/?range=24h|7d|30d|90d` (hourly buckets up to a week, daily beyond; summary, response-time series, timing breakdown; the last two hours are refreshed on request so it is current without waiting for the job, which is the only raw-check read and is bounded to that window) and `GET /api/v1/analytics/fleet/?range=7d|30d|90d` (summary, a daily series, and the top offenders by downtime; read entirely from rollups, a fixed number of queries however long the range). The fleet figures can be up to an hour old (`data_as_of`).

Tests: 792 in total, including rollup correctness against known synthetic checks (exact percentiles, hour boundaries, confirmation re-checks, exclusions, midnight-spanning incidents, retention), idempotency, and endpoint tests proving a month of history is served with no raw checks present and that no query touches raw checks outside the last 48 hours.

## Deploys and root cause (Phase 10)

An incident now says **what it followed** and **where the request went wrong**.

**Connecting a source.** `POST /api/v1/deploy-sources/` creates a connection (`github`, `vercel`, `render` or `generic`). Each has its own webhook address, `POST /api/v1/deploys/webhook/{source}/{token}/`, and its own signing secret. The token in the URL says whose connection it is; the provider's signature over the body says the request is genuine, and **both must hold**: an unknown, switched-off or mismatched address is a plain 404 (it never says which part was wrong), a bad signature is a 401, and a body over 256 KB is a 413. Nothing in the body is read before the signature is checked. Each provider's own scheme is verified:

| Source | Signature | Counts as a deploy |
| --- | --- | --- |
| `generic` | `X-RootPulse-Signature: t=..,v1=hmac_sha256(secret, "<t>." + body)` (the same scheme as the scheduler trigger; stale timestamps refused) | `{"service": "my-api", "version"?, "environment"?, "url"?, "occurred_at"?, "id"?}` |
| `github` | `X-Hub-Signature-256: sha256=...` | `deployment_status` with state `success`; a `workflow_run` that completed successfully **and is named like a deploy** (deploy, release, ship, publish). Pings are acknowledged and ignored. |
| `vercel` | `x-vercel-signature` (HMAC-SHA1 of the body) | `deployment.succeeded` / `deployment.ready` |
| `render` | Svix-style `webhook-id` / `webhook-timestamp` / `webhook-signature` (several signatures allowed for key rotation; stale timestamps refused) | `deploy_ended` with a succeeded status |

A genuine request that isn't a successful deploy (a ping, a failed run, a preview) is answered `202 {"status": "ignored"}`. A connection can be limited to one environment (`production`); other environments are ignored. Ingestion is idempotent per provider id (a retry or replay is a no-op), the provider's clock is believed only when plausible, links must be plain http(s), text is stripped of control characters, and only a small summary of the payload is stored. Secrets: RootPulse generates the secret for GitHub and generic connections (shown once, rotatable); for Vercel and Render the provider issues it, but only once the webhook exists there (and creating the webhook needs our address first), so those connections are created first, **without** a secret, and the secret is added afterwards (`PATCH signing_secret`; `has_secret` says whether it is set). Until it is, the connection accepts nothing, and a signature made with an empty key is never honoured. Secrets never appear in a response after creation.

**Which monitor does a deploy belong to?** The convention is explicit: a monitor has a **`deploy_service`** tag, and a deploy links to it only when that tag equals the deploy's service name (case-insensitive). For GitHub the service name is the repository name, and `owner/name` also matches; for Vercel it is the project name; for Render the service name (or id); for generic whatever you send as `service`. **A monitor with no tag is never linked**, so one deploy can't blame every monitor.

**Linking.** When an incident opens, it is linked to the latest deploy of its monitor's tagged service that finished within `DEPLOY_CORRELATION_WINDOW_SECONDS` (300) *before* it started; a deploy after the incident is not blamed (it is probably the fix). If the deploy webhook arrives *after* the incident was opened, the deploy links it retroactively when it happened just before the incident; a closer deploy replaces a farther one, never the reverse. The link is written to the timeline (`deploy_linked`), shown in the incident and its alerts ("Started 90 s after a deploy of my-api (abc1234)."), added to the postmortem draft, and counted on the fleet overview ("% of incidents after a deploy"). Deleting a deploy or a source leaves incidents intact.

**Root-cause stage** (`deploys/rootcause.py`, HTTP and keyword monitors). The failing check's timings are compared with that monitor's own 30-day average per stage, from the rollups. Either the failure names the stage (a DNS error is DNS, a refused connection is TCP, a TLS error is TLS), or the stage that regressed most wins, provided it is both at least twice as slow and at least 100 ms slower than usual (so 2 ms to 5 ms isn't an incident). A server that answers with an error is "the server's response". A timeout leaves no timings, so the stage is left blank rather than guessed. The per-stage figures are stored with the incident so the UI can show them against the usual.

Tests: 919 in total, including a signature matrix per provider (bad, missing, wrong secret, stale, tampered), replays, environment filtering, every correlation case (window edges, late deploys, closer-wins, other users and services, untagged monitors) and the stage rules.
 Per-channel tests use stubbed HTTP, so no test touches the network.


## Dependency monitoring (Phase 11)

RootPulse watches the status pages of the services you depend on and tells you through your own alert contacts, so "is it us or them?" has an answer before you start debugging. Nothing is licensed: every provider publishes a public status feed, and RootPulse reads those.

**The catalogue** (`providers/catalog.py`) is curated: GitHub, npm, CircleCI, Atlassian, Sentry, Datadog, Cloudflare, Vercel, Netlify, Render, Fly.io, DigitalOcean, Google Cloud, Supabase, MongoDB Atlas, Twilio, Discord, Slack, OpenAI and Anthropic. It is synced into the database after every migrate (idempotent, so editing a name or feed URL there updates it). Stripe is left out on purpose: its public feed stopped updating in 2024, so it would report "operational" forever.

**Feed formats** are not standardised, so each provider names an adapter (`providers/adapters.py`). Adapters take already-fetched JSON and return normalised incidents; they never touch the network, so every one is tested against a **real captured feed** (`tests/fixtures/providers/`).

| Adapter | Used by | Reads |
| --- | --- | --- |
| `statuspage` | most providers (Atlassian Statuspage v2) | `/api/v2/summary.json` (required: overall status and components) and `/api/v2/incidents.json` (history) |
| `slack` | Slack | `/api/v2.0.0/current` and `/api/v2.0.0/history` |
| `gcp` | Google Cloud | `/incidents.json` |

Real feeds are untidy, and the adapters cope with what was actually seen: an incident marked resolved *before* it started is clamped, scheduled maintenance is not treated as an outage, markdown and HTML in updates become plain text, only plain http(s) links are kept, a response over 3 MB is refused, and an incident that vanishes from a feed is closed.

**Polling** is the same "table is the queue" pattern as checks and alerts. Each provider has a `next_poll_at`; `run_engine` (or `POST /internal/run-due-checks/`) claims due providers with `FOR UPDATE SKIP LOCKED` and a short lease, reads them in a small thread pool, and stores the result in bulk (one round trip, not one per incident). A provider someone tracks is read every 5 minutes, an untracked one every 30; a failing one backs off (5, 10, 20... up to 60 minutes). After three unreadable passes in a row the status becomes `unknown` rather than keeping a stale "operational", and the API reports `stale: true` so the UI can say "can't reach its status page".

**Alerts** use the existing queue, dedupe, retries, delay and recovery rules, so provider incidents reach the same six channels as monitor incidents. For each subscription, an *opened* alert is raised when an incident is ongoing, its impact is at or above the subscription's `min_impact` (`none`, `minor`, `major`), and it began within `PROVIDER_NOTIFY_MAX_AGE_HOURS` (12). That age rule matters: several providers leave an old incident "open" forever, and subscribing must not fire an alert for something from last spring. A *resolved* alert goes only to contacts who actually received the opened one. Subscribing while something is already going on alerts immediately; unsubscribing cancels anything still queued. Each contact is a delivery row, unique per (incident, contact, event).

**API** (all under `/api/v1/`, signed-in users only):

- `GET /providers/` (`?q=`, `?category=`, `?subscribed=true`), `GET /providers/{slug}/` (components that aren't operational, recent incidents)
- `POST /providers/{slug}/subscribe/` (`min_impact`, `alert_contact_ids`; 201 on create, 200 on update, and all your contacts by default), `DELETE /providers/{slug}/subscribe/` (204, idempotent)
- `GET /provider-incidents/` (`?scope=subscribed|all`, `?status=ongoing|resolved`, `?provider=slug`, paginated)

Two deliberate differences from the roadmap's sketch: providers are addressed by **slug** rather than id (readable URLs, stable across environments), and "my providers" is a `?subscribed=true` filter rather than a separate `/providers/subscribed` route.

**Replaying a real incident** (the definition of done): `python manage.py replay_provider_feed github --doc summary=tests/fixtures/providers/github_summary.json --doc incidents=tests/fixtures/providers/github_incidents.json --as-of 2026-10-01T15:00:00Z` rewinds a captured feed to a moment in the past, ingests it, and prints the status and alerts it would have produced. The GitHub "Actions Job Delays" incident (14:47-17:56 UTC on 2026-10-01) is asserted in the tests at 15:00 (ongoing, a subscriber is alerted) and at 18:00 (resolved, the resolution is sent to those who got the alert).

`python manage.py run_provider_polls_once` reads everything due once; `seed_providers` re-syncs the catalogue. Tunables (all optional): `PROVIDER_POLL_SECONDS`, `PROVIDER_IDLE_POLL_SECONDS`.

Tests: adapters against the real fixtures, ingestion, the alert rules, replay, the poller (with a mocked transport; the suite blocks real network), and the API.

## Status pages (Phase 12)

A public, branded page that shows the state of the monitors you choose. It is the one place RootPulse shows your data to people who aren't signed in, so what it may reveal is narrow and written down in one function (`statuspages/services.py: build_public`), which builds the payload field by field instead of serialising models.

**What the public sees:** the page name and branding; each component's name (the one *you* chose), group, current status and 90-day uptime bar; ongoing and recent (14 days) outages as "component, started, ended"; announcements you posted; and any incident note you switched to **visible on status page**. **What it never sees:** a monitor's name, id, target, type, config or deploy tag, why a check failed, status codes, who wrote a note, or anything about your account. A test asserts none of those strings appear in the response.

**Models** (`statuspages/models.py`): `StatusPage` (name, unique slug, password hash, `is_public`, branding), `StatusPageComponent` (a monitor shown under a display name, group and order, unique per page), `StatusPageAnnouncement` (an incident write-up or planned maintenance), `StatusPageSubscriber` (email, confirmation and unsubscribe tokens) and `StatusPageEmail` (the outgoing mail queue). One page per user (a product limit, enforced in the API rather than the schema).

**Owner API** (`/api/v1/status-pages/`, signed in, everything scoped to you; another user's page and its parts are a 404 on every route):

- `GET/POST /status-pages/`, `GET/PATCH/DELETE /status-pages/{id}/` (name, slug, `is_public`, `branding`, write-only `password`: a string sets it, `null` removes it; password changes are audit-logged without the password)
- `GET /status-pages/{id}/preview/`: the page as visitors will see it, even when it is a draft or has a password
- `GET/POST .../components/`, `PATCH/DELETE .../components/{cid}/`, `POST .../components/reorder/`
- `GET/POST .../announcements/`, `PATCH/DELETE .../announcements/{aid}/`, `POST .../announcements/{aid}/resolve/`
- `GET .../subscribers/` (paged), `DELETE .../subscribers/{sid}/`

Branding takes only `accent` (`#rrggbb`), `logo_url` and `support_url` (https only), and a plain-text `description`; unknown keys are not stored, and the frontend renders everything as text.

**Public API** (`/api/v1/public/status-pages/{slug}/`, no sign-in, each with its own per-IP throttle):

| Request | What it does |
| --- | --- |
| `GET /` | The page. A missing page and an unpublished one are the same 404. A password-protected page returns only `{name, slug, branding, password_required: true}` until unlocked. Never cached (`Cache-Control: no-store`), so a note switched off or a page unpublished disappears at once. |
| `POST /unlock/` `{password}` | 403 for a wrong password (10/min per IP), otherwise a signed token valid 12 hours, sent back as `X-Status-Page-Token`. The token is tied to the page and to the current password, so changing the password signs everyone out. |
| `POST /subscribe/` `{email}` | Starts double opt-in. Always answers 202 with the same words, whether or not the address is known. 10/hour per IP, one confirmation email per address per 10 minutes, caps on subscribers (1000) and unconfirmed requests (200). |
| `POST /confirm/{token}/` | Confirms once. Unconfirmed requests expire after 7 days. |
| `GET /unsubscribe/{token}/`, `POST /unsubscribe/{token}/` | GET tells the page who the link is for and changes nothing (mail scanners open links); POST removes the subscriber. Works even if the page is later unpublished. |

Deviation from the plan sketch: unsubscribing is a POST (with a GET that only describes), not a state-changing GET, and every email carries `List-Unsubscribe` and `List-Unsubscribe-Post` headers for one-click unsubscribe.

**Subscriber email** uses the same queue shape as alerts (`statuspages/mail.py`): rows are written when something happens, claimed with `FOR UPDATE SKIP LOCKED`, retried with the alert back-off, and sent in the engine pass (`run_engine`, `POST /internal/run-due-checks/`, or `manage.py run_status_page_mail_once`). Plain text only. Confirmed subscribers of a **published** page are emailed when a component's monitor goes down and when it recovers (recovery only to those who were told it went down; if the first email hasn't gone out yet it is cancelled instead), and when you post or resolve an announcement (you can untick "Email subscribers"). Nothing is sent about a monitor in a maintenance window, for a page that was unpublished before the mail went out, or to anyone who hasn't confirmed. A problem queueing these emails never costs the incident or the alerts to the owner.

**Uptime bars** come from the daily rollups (`CheckRollupDaily`): failures inside an excluded incident don't count, and a day with no data is blank, never 100%. Incidents excluded from reports are left off the public page as well. Component status maps the monitor's state (`up` operational, `down` outage, `paused` not monitored, `pending` checking) and shows *maintenance* while the monitor is inside a maintenance window; the banner is *all operational*, *partial outage* (some down), *major outage* (all down) or *under maintenance*.

**Not built (stretch goal in the plan):** custom domains. The page lives at `/s/{slug}` on the frontend.

Tests: 129 in `tests/test_status_pages.py`, covering ownership on every route, slug/branding/password validation, the access rules (unpublished vs missing, locked pages leak nothing, tokens are per page and die with the password, expiry, throttles), what the public payload contains and omits, a fixed number of queries however large the page, the comment-visibility toggle appearing and disappearing on the page through the real incident API, the double opt-in flow, and the email queue (fan-out, recovery rules, retries, permanent failures, header injection).

## Reports and export (Phase 13)

Files you can download, built on the spot from the same data the dashboards read, so a report for the last 30 days agrees with the analytics page for the last 30 days (a test asserts the equality, and so does a check against the real dev database). All under `/api/v1/reports/`, signed in, scoped to you, 20 requests a minute.

| Request | What you get |
| --- | --- |
| `GET /reports/uptime.pdf?start_date=&end_date=[&monitor_id=]` | A PDF: the period, a summary (uptime, checks, average response, incidents, total downtime, longest incident, average time to recover), a table per monitor, and every incident that started in the period. |
| `GET /reports/uptime.csv?...` | The per-monitor table as CSV, plus an "All monitors" total row. |
| `GET /reports/incidents.csv` | The incidents export (the same filters and columns as `GET /incidents/export/`), so every download lives under `/reports/`. |
| `GET /reports/status-page/{id}.pdf` | A snapshot of one of your status pages: overall status, each service with status and 90-day uptime, announcements, and the last 14 days of outages with the notes visitors can see. Works for a draft or a password-protected page (you own it); built from the same function as the public page, so it can't show anything the public page wouldn't. |

**The period.** Both dates are required, inclusive, at most 366 days, not in the future. Days are **UTC days** (that is what the daily rollups are made of); the PDF says so. Checks and uptime come from `CheckRollupDaily`; downtime, incident counts, longest incident and time to recover come from incidents clipped to the period (an incident that began before it counts only the time inside, and is counted under the day it began). Incidents marked *excluded from reports* are left out of every figure and from the list, and the report says how many were left out. A monitor with no checks and no incidents in the period isn't listed as a row of zeros.

**Files.** The PDF is made with ReportLab (pure Python, no browser or system libraries, which is what makes it behave the same on a laptop and in the Render container; check it once in the deployed container, as the roadmap advises). Everything from your account is shown as text, never interpreted as markup, and characters the built-in PDF font can't draw (for example CJK) appear as `?` rather than as boxes. Times are in your time zone. Lists are capped at the most recent 500 incidents, and the PDF says when it was cut. CSV cells that start with `=`, `+`, `-` or `@` are prefixed with `'` so a spreadsheet won't run them as formulas. Responses are `Cache-Control: no-store`.

Tests: 57 in `tests/test_reports.py`. The CSV is parsed back with `csv` and the PDF with `pypdf`, and both are compared with numbers worked out by hand (checks, failures, uptime, an incident across midnight, an ongoing one, excluded ones), with the fleet analytics for the same period, and with the incident export. They also cover range validation, other users' data never appearing, the status page PDF showing nothing a visitor couldn't see, markup and non-Latin names, time zones, a 520-incident report running over pages, and the rate limit.

## Onboarding (Phase 14)

A setup checklist on the dashboard that teaches what RootPulse is for as it sets it up. Progress is **read from the account's own data, not stored**, so it can't drift: delete your only monitor and the step reopens, connect a deploy source anywhere and it ticks itself.

`GET /api/v1/auth/onboarding/` returns the steps in order, each `{id, done}`, with `done_count`, `total`, `completed_at` and `dismissed`:

| Step | Done when |
| --- | --- |
| `monitor` | you have a monitor |
| `alerts` | you have an alert contact that can actually reach you (enabled, and for email, confirmed; an unconfirmed address isn't "set up") |
| `deploys` | you have a deploy source |
| `dependencies` | you track a provider |
| `status-page` | you have a status page |

Deviation from the roadmap's four steps: **dependencies** is the fifth, because knowing whether a problem is yours or a provider's is part of what RootPulse is for. Only two moments are remembered, as `User` fields: `onboarding_completed_at` (stamped the first time every step is done, once; later removing something reopens that step but doesn't un-finish it) and `onboarding_dismissed_at`. `PATCH` the same URL with `{"dismissed": true|false}` to hide the card or bring it back. Everything is per person, and signed-out callers get a 401.

Tests: 21 in `tests/test_onboarding.py`: the steps in order, each ticking from the real thing (including alert-channel reachability and other people's data not counting), progress following the data in both directions, the completion stamp (once, surviving later changes), and dismiss/restore.

## API keys and the developer surface (Phase 15)

The API the dashboard uses is the API scripts use; keys are only a different way to sign in. No shadow "public API": a test fetches the same endpoints with a key and with a dashboard token and compares the JSON.

**Keys.** `GET/POST /api/v1/api-keys/`, `DELETE /api-keys/{id}/` (the first DELETE revokes; deleting an already-revoked key removes it from the list). A key looks like `rp_` plus 43 random characters and is shown in the create response **once**; only its SHA-256 and a visible prefix (`rp_Ab3dE9fG`) are stored. Deviation from the plan, which says Argon2: a key is 256 random bits, so there is nothing to guess and nothing for a slow hash to protect, while it is checked on every request and has to be fast and indexable. A slow, salted hash would put roughly 50 ms of CPU on every API call and make lookup by hash impossible. Up to 10 active keys per account; making, revoking and removing are audit-logged without the key.

**Using one.** `Authorization: Bearer rp_...` on any endpoint. It is tried before the dashboard's JWT (they can't be confused: a JWT doesn't start with `rp_`). A wrong, revoked or unknown key is a `401` with one message for every case, so it says nothing about which keys exist; a revoked key stops working on the next request (nothing is cached). The key's owner must still be active.

**Scopes.** `read`: `GET`, `HEAD` and `OPTIONS` only; anything else is a `403 "This API key is read-only."`. `full`: everything except managing keys. The check lives in the authentication class, not in a permission class, so a view that sets its own `permission_classes` can't forget it. Key management itself needs a dashboard session (`SessionOnly`): even a full key can't list, make or revoke keys, so a leaked key can't dig in.

**`last_used_at`** is for people reviewing their keys, so it is written at most once a minute per key (never on every request) and a refused request doesn't count.

**Rate limits.** A key is its account for throttling: 60 requests a minute shared with the dashboard, so making a key doesn't raise anyone's allowance. Every throttled response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds), and a `429` adds `Retry-After`. Wrong keys are refused before DRF's throttles run, so they have their own limit: 20 wrong keys a minute per address (`API_KEY_FAILURES_PER_MINUTE`), counted only for failures, so a good key never adds to it.

**Reference.** The API serves its own reference, always in step with the code: Swagger UI at `/api/v1/docs/` and the OpenAPI schema at `/api/v1/schema/` (both public). The schema declares both sign-in schemes (`jwtAuth`, `apiKey`) and its description (`config/api_description.md`) covers authentication, scopes, rate limits, errors and pagination.

Tests: 52 in `tests/test_api_keys.py`: the matrix from the security plan (valid works, revoked is rejected at once, a read key is rejected on a write), the key shown once and stored only as a hash, malformed headers, keys can't manage keys, ownership, `last_used_at`, rate-limit headers and limits, guessing throttled, and the published schema.

## Security hardening (Phase 16)

A deliberate review of Phases 0 to 15; the full write-up, with every finding, the fix and the test that pins it, is `docs/plan/07-security-review.md`.

**The gate** (`tests/test_security_gate.py`) crawls the whole API, so a new endpoint is covered the day it exists: the routes that answer without signing in are exactly the reviewed list; every other operation in the published schema refuses an anonymous caller; a read-only API key is refused on every write; user B reaches none of user A's objects by any id through any endpoint; no endpoint answers junk with a 500; every API response carries `no-store`, a locked-down content policy and `nosniff`; and the production settings pass `manage.py check --deploy --fail-level WARNING`. The gate was proven able to fail by deliberately breaking an ownership check and the read-scope check.

**What it found and fixed:** Django 5.1 had 8 known vulnerabilities (now Django 5.2 LTS, `pip-audit` clean); the SSRF validator relied on the system resolver to refuse `127.1`, `0x7f.0.0.1` and IPv4-compatible IPv6 spellings (now refused by the validator itself, in Python and in the Cloudflare Worker, from one shared fixture); the dependency poller followed redirects unchecked (each hop is now vetted); a newline in a monitor name made every email alert for it fail (names are one line, subjects too); production lacked HTTPS redirect, HSTS and secure cookies; the admin was at a well-known address (`DJANGO_ADMIN_URL`); the frontend sent no security headers (now a content policy and the standard set) and trusted server-validated links (`safeHref`).

**What can only be verified once deployed** (SSRF against real internal targets and a real rebinding domain, prober isolation, rate limits under real traffic, Sentry alerting) is a checklist at the end of the review document.

Run before every deploy: `pip-audit`, `python manage.py check --deploy`, the whole test suite.

Tests: 30 in the gate, 43 in `tests/test_security_hardening.py` (one-line names, address spellings, provider redirects, mass assignment, forged tokens), plus the shared SSRF fixture run by both Python (147) and the Worker (26).
