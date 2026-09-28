# RootPulse — Build Plan: Overview

Status: **Draft — awaiting approval before implementation begins.**
Source of truth for product scope: [`RootPulse_PRD.pdf`](../../RootPulse_PRD.pdf) (v1.2). Everything in this plan traces back to that document. Where the PRD explicitly excludes something from MVP (billing, teams/roles, SMS/voice, AI features, the full 6,000-provider dataset), this plan excludes it too rather than building generic SaaS scaffolding nobody asked for.

This plan lives in two repos:
- `RootPulse-backend/docs/plan/` — this file plus tech stack, database, API, monitoring engine, security, testing/deployment, and the phase-by-phase roadmap (the full system).
- `RootPulse-frontend/docs/plan/` — frontend architecture, routing, design system, and landing page (frontend-specific detail; points back here for anything backend/system-wide).

## 1. Repository assessment

| Repo | Contents found | Conclusion |
| --- | --- | --- |
| `RootPulse-backend` | One file: `RootPulse_PRD.pdf`. No `.git`, no `package.json`, no config, no framework, no tooling. | Genuine blank slate — nothing to preserve, no conflicting setup to work around. |
| `RootPulse-frontend` | Completely empty. | Same — blank slate. |

Both repos need `git init`, license/README, and a full toolchain from scratch (Phase 0).

## 2. Guiding principles for this plan

1. **PRD is the contract.** Every requirement in the PRD must land in a phase below. Nothing is invented that the PRD didn't ask for (no billing, no teams, no SMS, no AI) — see Section 11 of the PRD and the "explicitly deferred" column in the roadmap (`06-roadmap.md`).
2. **$0 ongoing cost is a hard constraint**, not a nice-to-have. Every infrastructure choice in `01-tech-stack.md` is selected because it has a genuinely perpetual free tier, not a free trial. Where a free tier has a real limitation (e.g. cold starts, quota caps), it's stated plainly rather than glossed over.
3. **Frontend-first where practical, without faking the contract.** The API shape (`02-database-and-api.md`) is designed *before* any frontend code, so frontend work can start against a typed mock client and swap to the real API with no rewrite.
4. **Two languages, one contract.** Frontend is TypeScript (Next.js), backend is Python (Django). Instead of hand-duplicating types across the language boundary, the backend's DRF serializers generate an OpenAPI 3.1 schema (`drf-spectacular`), and the frontend generates a typed client from it (`openapi-typescript`) — so a backend field rename becomes a frontend type error, not a silent bug.
5. **Boring, provable infrastructure over clever infrastructure.** The monitoring engine's job queue is a Postgres table claimed via Django's `select_for_update(skip_locked=True)`, not a Celery/Redis cluster — because at MVP scale (dozens of monitors) that's simpler, cheaper, and has one fewer moving part to keep free. This is revisited explicitly as a V2 trigger in the roadmap, not silently ignored.

## 3. System architecture, at a glance

```
                         ┌────────────────────────┐
   Browser  ───────────► │  Frontend (Next.js,     │
   (user)    ◄─────────  │  Vercel)                │
                         └───────────┬─────────────┘
                                     │ REST (typed via OpenAPI)
                                     ▼
   GitHub Actions        ┌────────────────────────┐
   cron (every 5 min) ──►│  Backend API             │
   triggers a scheduler  │  Django + DRF (Render,   │
   pass AND keeps the    │  free Web Service)       │
   free service awake    └───────────┬─────────────┘
                     ┌───────────────┼────────────────────┐
                     ▼               ▼                    ▼
           ┌──────────────┐ ┌────────────────┐  ┌──────────────────┐
           │  Postgres     │ │  Scheduler      │  │  Notification     │
           │  (Neon)       │ │  pass (one-shot │  │  dispatcher pass   │
           │  — monitors,  │ │  mgmt command,  │  │  (one-shot mgmt    │
           │  checks,      │ │  claims due     │  │  command, claims   │
           │  incidents…   │ │  monitors via   │  │  due sends via     │
           └──────────────┘ │  skip_locked)   │  │  skip_locked)      │
                             └───────┬─────────┘  └─────────┬──────────┘
                                     │ HTTP (signed)         ▼
                         ┌───────────┴─────────┐   Email / Slack / Discord /
                         ▼                      ▼   Telegram / Webhook / Push
                 ┌───────────────┐    ┌───────────────┐
                 │ Prober         │    │ Prober         │   (2–3 regions,
                 │ region A       │    │ region B       │    Cloudflare
                 │ (Cloudflare    │    │ (Cloudflare    │    Workers — no
                 │  Worker)       │    │  Worker)       │    sleep/cold-start)
                 └───────┬────────┘    └───────┬────────┘
                         ▼                      ▼
                External websites / APIs / servers / ports / DNS / heartbeats
```

## 4. Monitoring lifecycle, adapted to the PRD

```
Monitor created (any of 8 types)
        ↓
GitHub Actions cron (every 5 min) calls the signed /internal/run-due-checks
endpoint, which claims due rows from `monitors` (next_check_at <= now())
via SELECT … FOR UPDATE SKIP LOCKED — no external queue needed at MVP scale
        ↓
Primary region executes the check directly; for HTTP(S)/Ping/Port monitors,
2 additional regions are also asked to check (multi-location confirmation)
        ↓
Each result recorded to `checks` (status, timing breakdown, region)
        ↓
Failure evaluated: a single failed check never opens an incident —
a second region (or a same-region recheck for non-HTTP types) must confirm
        ↓
Confirmed failure → incident opened/updated; deploy-correlation and
root-cause timing breakdown attached automatically (Phase 10)
        ↓
Notification dispatcher claims the incident's due notifications
(same SKIP LOCKED pattern) and sends via every attached alert contact
        ↓
Recurring notifications re-fire on cadence while the incident stays open
        ↓
Next successful check (from the primary region) → recovery detected
        ↓
Incident closes, duration/reason logged, recovery notification sent,
analytics aggregates updated (Phase 9)
```

## 5. Master roadmap, at a glance

Full detail — objectives, frontend/backend/DB/infra work, dependencies, testing, definition of done, complexity, and risk per phase — is in `06-roadmap.md`.

| # | Phase | One-line objective |
| --- | --- | --- |
| 0 | Repository & Infrastructure Setup | Both repos scaffolded, CI green, free-tier accounts provisioned |
| 1 | Frontend Foundation | Next.js app, design system, shared UI kit, typed API client stub |
| 2 | Public Landing Page | Full marketing site, live before the product is |
| 3 | Backend Foundation & Authentication | API scaffold, DB schema, auth (email/password + GitHub OAuth) |
| 4 | Core Application Shell | Dashboard layout, nav, protected routes, empty states |
| 5 | Monitor Management | CRUD for all 8 monitor types, single scrollable creation form |
| 6 | Monitoring Engine | Scheduler, probers, multi-location checks, timing breakdown |
| 7 | Incident Detection & Management | Confirm → open → comment → postmortem → resolve → export |
| 8 | Notifications | Email/Webhook/Telegram/Discord/Slack/Push, recurring, maintenance windows |
| 9 | Analytics Dashboard | Per-monitor + fleet-wide, pre-aggregated |
| 10 | Deployment-Aware Incidents & Root-Cause Timing | Deploy webhooks, DNS/TCP/TLS/TTFB correlation |
| 11 | Dependency Monitoring | ~15–20 curated providers, public feed polling |
| 12 | Status Pages | Public page, components, subscribers, branding |
| 13 | Reports & Export | PDF/CSV, on demand |
| 14 | Onboarding & Polish | Gamified checklist, empty/success-state tone |
| 15 | API & Developer Platform | API keys, public REST API, rate limits |
| 16 | Security Hardening | SSRF defenses, abuse prevention, audit logging |
| 17 | Testing & QA | Coverage of every critical scenario in `05-testing-deployment-devex.md` |
| 18 | Deployment to Production | Staging → production, monitoring the monitor |

**Explicitly not phased** (per PRD Section 11 — deferred): billing/subscriptions, team roles/seats, SMS/voice alerts, AI-assisted features, synthetic multi-step transactions, RUM, multi-client agency workspaces, on-call scheduling/escalation.

## 6. Where to go next

- `01-tech-stack.md` — full stack with reasoning
- `02-database-and-api.md` — entities + endpoint contracts
- `03-monitoring-engine.md` — scheduler/workers/queue/notifications/analytics in depth
- `04-security.md` — full security plan, SSRF deep dive
- `05-testing-deployment-devex.md` — testing, deployment, local dev, git workflow
- `06-roadmap.md` — every phase in full detail, complexity/risk estimates
- `../../RootPulse-frontend/docs/plan/` — frontend architecture, design system, landing page
