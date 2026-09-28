# RootPulse — Backend

Django 5 + Django REST Framework, Python. See [`docs/plan/`](./docs/plan/) for the full system plan (architecture, database, API, monitoring engine, security, testing/deployment, and the 19-phase roadmap).

## Getting started

```bash
cp .env.example .env               # fill in local secrets
docker compose up -d db            # local Postgres (needs Docker Desktop running)
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver          # API on :8000
```

Scheduler and notification-dispatcher management commands (`run_scheduler`, `run_notifications`) are added in Phase 6/8 — not present yet.

## Scripts

| Command | What it does |
| --- | --- |
| `python manage.py runserver` | Dev server |
| `python manage.py check` | Django system checks |
| `ruff check .` | Lint |
| `black .` / `black --check .` | Format / check |
| `python -m pytest` | Test suite |

## Notes on this environment's setup

- **Local dev runs on Python 3.14** (whatever was installed on this machine) — confirmed working with Django 5.1 and every package in `requirements.txt`. **CI pins Python 3.12** deliberately (Django 5.1's officially supported range), since a bleeding-edge interpreter is fine for local iteration but not the right reproducibility target for CI/production — this is an intentional mismatch, not an oversight.
- Custom user model (`accounts.User`) is in place from the first migration, per `docs/plan/01-tech-stack.md` — never retrofitted later.
- `Dockerfile` and `fly.toml` are written and ready but **not yet deployed** — that needs a Fly.io account (see "What needs you" below).

## What needs you (account creation this session won't do on its own)

Per this session's own safety rules, I don't create accounts on third-party services. These are ready to wire up as soon as you've done the account-creation step yourself:

| Service | What it's for | What to do |
| --- | --- | --- |
| **Neon** | Postgres hosting (dev/staging/prod) | Create a free account + project, give me the connection string |
| **Fly.io** | API + scheduler + prober hosting | Create a free account, install `flyctl`, run `fly auth login` — then I can run `fly launch`/`fly deploy` using the `Dockerfile`/`fly.toml` already in this repo |
| **Vercel** | Frontend hosting | Create a free account, connect the `RootPulse-frontend` repo |
| **Resend** | Transactional email (Phase 8) | Create a free account, give me the API key |
| **Sentry** | Error tracking | Create a free account + project |
| **GitHub** | Remote repo + CI/CD | Create the `RootPulse-backend`/`RootPulse-frontend` repos, push these local commits, add secrets for the CI workflows |

Once any of these exist, tell me and I'll wire up the corresponding config/secrets immediately.

## Local Postgres blocker (as of Phase 0)

Docker Desktop wasn't running when this was set up, so migrations haven't been applied/verified against a real database yet — `python manage.py check` and the full lint/format/test suite (everything that doesn't need a live DB) all pass. **Start Docker Desktop, then run `docker compose up -d db && python manage.py migrate`** to finish this off.
