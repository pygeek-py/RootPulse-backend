# Testing, Deployment & Developer Experience

## 1. Testing strategy

### Frontend

| Layer | Tool | Covers |
| --- | --- | --- |
| Unit | Vitest | Pure functions, hooks, Zod schemas |
| Component | Vitest + React Testing Library | Individual components in isolation (forms, tables, cards), mocked API client |
| Integration | Vitest + RTL | A feature's components + hooks together against a mocked API layer (MSW — Mock Service Worker, generated from the same OpenAPI schema so mocks can't silently drift from the real contract) |
| E2E | Playwright | Full critical flows against a real running backend + test database (§4 below) |

### Backend

| Layer | Tool | Covers |
| --- | --- | --- |
| Unit | pytest | Model methods, serializers, the SSRF target-validation module, the timing/root-cause calculation logic |
| API | pytest-django + DRF's `APIClient` | Every endpoint in `02-database-and-api.md`: happy path, validation errors, permission boundaries |
| Auth/permission | pytest-django | Every endpoint rejects another user's `user_id`-scoped resources (this is the single most important test class, given §2 of `04-security.md`) |
| Monitoring worker | pytest | The scheduler's claim query never double-claims under simulated concurrency; check execution against a mocked `httpx` transport (no real network calls in tests) |
| Notification | pytest | Each channel adapter against a mocked provider API; retry/backoff logic |

### Infrastructure

| Concern | Approach |
| --- | --- |
| Queue/claim correctness | A test that runs two scheduler "ticks" concurrently (via threads) against the same due-monitor set and asserts no monitor is claimed twice — this is the test that actually validates the whole Postgres-queue architecture decision in `01-tech-stack.md` |
| Scheduler timing | Freeze time (`freezegun` or Django's `override_settings`) to assert `next_check_at` math for every monitor type's interval logic, including the heartbeat grace period |
| Failure/recovery state machine | Simulate a sequence of check results and assert the exact `Incident`/`IncidentEvent`/`NotificationDelivery` rows produced at each step |

### Critical scenarios (explicit, must all be covered before Phase 17 exits)

- Monitor creation for all 8 types, including validation rejection (bad URL, SSRF-blocked target, invalid config per type)
- Monitor failure → multi-region confirmation → incident creation → notification fan-out
- A single-region blip that is **not** confirmed by the other regions → no incident created
- Recovery → incident closes → recovery notification
- Deploy webhook ingestion → correct correlation window matching → linked on the right incident, not a wrong one
- Root-cause timing breakdown computed correctly from a known set of `Check` timing values
- Status page: public read with and without a password; subscriber double opt-in; comment visibility toggle actually removing it from the public page
- API key auth: valid key works, revoked key is rejected immediately, `read`-scope key rejected on a write
- Permission boundaries: user A cannot read/modify user B's monitors, incidents, status pages, or API keys via any endpoint
- SSRF: a URL resolving to a private/loopback/link-local IP is rejected at creation *and* would be rejected at check time even if DNS changed after creation (the rebinding scenario, tested by mocking DNS resolution to return different IPs on two calls)
- Rate limiting: login endpoint actually throttles after the configured threshold
- Data retention: the rollup job correctly aggregates and the raw-check deletion only removes rows already folded into a rollup, never data that hasn't been aggregated yet

## 2. Deployment architecture

```
        Local dev            CI (GitHub Actions)         Production
   ┌──────────────────┐   ┌────────────────────┐   ┌────────────────────────┐
   │ Next.js dev server│   │ lint + typecheck    │   │ Vercel (frontend)       │
   │ Django runserver  │   │ + pytest + vitest   │   │ Render (API, free web   │
   │ local Postgres    │   │ + Playwright        │   │  service)               │
   │ (docker-compose)  │──►│ (against a service- │──►│ Cloudflare Workers      │
   └──────────────────┘   │  container Postgres) │   │  (2 regional probers)  │
                           └────────────────────┘   │ Neon (Postgres)         │
                                                     │ GitHub Actions cron     │
                                                     │  (scheduler trigger +   │
                                                     │  keep-alive, every 5m)  │
                                                     │ Resend (email)          │
                                                     │ Sentry (errors)         │
                                                     └────────────────────────┘
```

- **Environments:** local → CI (ephemeral, per-PR) → staging (a second Render service + Neon branch, mirrors production config, its own GitHub Actions cron trigger on a separate schedule) → production. Staging exists specifically so the monitoring engine can be exercised against real external targets without touching production data or alerting a real user.
- **Migrations:** Django's own migration system, run as a Render "pre-deploy" / release-phase command (`python manage.py migrate`) before the new backend version starts serving traffic.
- **Backups:** Neon's built-in point-in-time recovery (part of its free tier) — not a separately built backup system.
- **Monitoring the monitor:** an external free check against RootPulse's own `/health` endpoint (`01-tech-stack.md`), plus Sentry for error tracking, Render's dashboard logs, and the GitHub Actions workflow run history itself (a run that stops firing is as much a signal as an error would be).

## 3. Development environment

```bash
# Backend
cd RootPulse-backend
cp .env.example .env            # fill in local secrets
docker compose up -d db         # local Postgres
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py loaddata seed_providers   # curated ~15–20 dependency providers
python manage.py runserver                 # API on :8000
python manage.py run_scheduler_once         # separate terminal, run manually/on a
python manage.py run_notifications_once     # loop — no persistent process locally either,
                                             # matching the triggered-pass production model

# Frontend
cd RootPulse-frontend
cp .env.example .env.local      # NEXT_PUBLIC_API_URL=http://localhost:8000
npm install
npm run generate-api-client     # regenerates the typed client from the backend's OpenAPI schema
npm run dev                     # Next.js on :3000
```

A `docker-compose.yml` in the backend repo provides local Postgres only — the app processes themselves run natively (faster iteration than containerizing everything for local dev), with Docker reserved for the actual Render deployment image (`Dockerfile`).

## 4. Git & branching strategy

- `main` is always deployable; protected, requires a passing CI run and one review (even solo — self-review via PR is a cheap habit that pays off) before merge.
- Feature branches: `feat/monitor-creation-form`, `fix/ssrf-redirect-validation`, etc. — short-lived, one phase-slice per branch rather than one branch per entire phase.
- Commit convention: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`) — cheap to adopt, makes changelog generation free later if ever wanted.
- No long-lived `staging`/`develop` branches — staging deploys from `main` on every merge (matches the "main is always deployable" principle); a `staging` *environment* exists (§2), but it isn't a git branch.
- Release process: merge to `main` → CI → auto-deploy to staging → manual promote to production (a deliberate manual gate for this solo project, not full continuous deployment to prod, until the test suite's real-world coverage earns that trust).
