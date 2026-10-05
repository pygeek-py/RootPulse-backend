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
 Per-channel tests use stubbed HTTP, so no test touches the network.

