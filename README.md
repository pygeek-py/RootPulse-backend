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
| **Resend** | Transactional email (Phase 8) | Create a free account, give me the API key |
| **Sentry** | Error tracking | Create a free account + project |
| **GitHub** | Remote repos + CI/CD + the scheduler-trigger cron workflow (Phase 6) | Create the `RootPulse-backend`/`RootPulse-frontend` repos, push these local commits, add secrets for the CI/cron workflows |

Once any of these exist, tell me and I'll wire up the corresponding config/secrets immediately.
