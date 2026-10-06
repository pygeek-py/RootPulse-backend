# Development Roadmap

19 phases, 0–18. Each has: objective, frontend/backend/database/infrastructure work, dependencies, testing requirement, definition of done, complexity, and major risks. Complexity/effort are realistic for a solo developer, not optimistic marketing numbers.

**Explicitly out of every phase below** (PRD §5.2/11, revisit triggers noted where relevant): billing/subscriptions, team roles/seats, SMS/voice alerts, AI-assisted features, synthetic multi-step transactions, RUM, multi-client agency workspaces, on-call scheduling/escalation.

---

### Phase 0 — Repository & Infrastructure Setup

| | |
| --- | --- |
| Objective | Both repos scaffolded, CI green on an empty app, every free-tier account provisioned |
| Frontend | `create-next-app` (TS, App Router, Tailwind), ESLint/Prettier, Vitest + Playwright configs, shadcn/ui installed |
| Backend | `django-admin startproject`, DRF + drf-spectacular installed, custom user model in place from commit 1, pytest-django configured |
| Database | **Neon project created and migrated (done)** |
| Infra | Render web service created (Docker deploy, `render.yaml` + `Dockerfile` ready); Vercel project linked; GitHub Actions workflows for lint/test (done for both repos); Sentry projects created |
| Dependencies | None — this is the starting point |
| Testing | CI pipeline itself is the test: a trivial passing test on each side, gating merges |
| Definition of done | `git push` to `main` on either repo triggers a green CI run and a successful (empty) deploy to staging |
| Complexity | Low | | Risk | Free-tier account setup friction (verification steps, etc.) — annoying, not hard |

**Checkpoint:** an empty Next.js page is live on Vercel; an empty Django health-check endpoint is live on Render; nothing else exists yet.

**Status as of this plan's last update:** frontend and backend scaffolding done and committed locally; Neon database provisioned and migrated (custom user model confirmed live). Still needed: Render/Vercel/Sentry/Resend accounts and the actual remote GitHub repos + pushes (see the backend README's "What needs you" table).

---

### Phase 1 — Frontend Foundation

| | |
| --- | --- |
| Objective | Design system implemented in code, shared UI kit, app shell that doesn't depend on the backend yet |
| Frontend | Tailwind theme (colors/type/spacing per `../../RootPulse-frontend/docs/plan/01-design-system.md`), shadcn/ui components themed, layout primitives (Card, Table, Badge, Modal, Dropdown), typed API client stub against a hand-written mock OpenAPI schema (real schema arrives Phase 3) |
| Backend | None |
| Database | None |
| Infra | None beyond Phase 0 |
| Dependencies | Phase 0 |
| Testing | Component tests for every shared primitive |
| Definition of done | A Storybook-less but structurally complete component library, all documented visually via the design-system doc, usable to build every later page without inventing new base components |
| Complexity | Medium | Risk | Over-building the kit before real pages exist — mitigated by only building what Phase 2's landing page and Phase 4's shell actually need first |

**Checkpoint:** the design system is browsable (e.g. a `/dev/components` route) even though no real feature page exists yet.

---

### Phase 2 — Public Landing Page

| | |
| --- | --- |
| Objective | The full marketing site is live, independent of auth/backend readiness |
| Frontend | All sections per `../../RootPulse-frontend/docs/plan/02-landing-page.md` (hero, features, monitoring types, incident workflow, analytics, status pages, integrations, differentiators, FAQ, footer) |
| Backend | None (a `/contact` form, if included, posts to a simple email-forwarding endpoint — the only backend touchpoint) |
| Database | None |
| Infra | Custom domain pointed at Vercel |
| Dependencies | Phase 1 |
| Testing | Playwright smoke test (page loads, nav works, no console errors); Lighthouse/perf check |
| Definition of done | Landing page live at the production domain, no fabricated stats/testimonials (PRD research explicitly forbids this), mobile-first responsive |
| Complexity | Medium | Risk | Copywriting/visual polish takes longer than the code does — budget for iteration, not a single pass |

**Checkpoint:** `rootpulse.<tld>` is a real, presentable, deployed marketing site before a single line of dashboard code exists.

---

### Phase 3 — Backend Foundation & Authentication

| | |
| --- | --- |
| Objective | Real API exists; a user can register, log in, and reach a protected endpoint |
| Frontend | Login/register pages (email link + code), verify page, protected-route middleware, auth state in the typed client |
| Backend | `/auth/*` endpoints in full (`02-database-and-api.md`), GitHub OAuth flow, JWT issuance/refresh, Argon2 hashing, real OpenAPI schema published |
| Database | `User`, `RefreshToken`, `AuditLog` models + first migrations |
| Infra | Real staging DB connected; secrets provisioned (JWT signing key, GitHub OAuth app credentials) |
| Dependencies | Phase 0; unblocks everything requiring auth |
| Testing | Full auth test matrix (`05-testing-deployment-devex.md` §Critical scenarios), rate-limit test on login |
| Definition of done | A real user can register, verify by logging back in, and GET `/auth/me`; frontend's mock API client is replaced by the generated real one |
| Complexity | Medium-High | Risk | OAuth callback edge cases (email already exists via password, account linking) — worth explicit test cases, not just happy path |

**Checkpoint:** registration → login → authenticated request round-trips against the real deployed backend.

---

### Phase 4 — Core Application Shell

| | |
| --- | --- |
| Objective | The logged-in app has a real layout, nav, and empty states — before any feature has data to show |
| Frontend | Dashboard shell (sidebar nav, top bar, account menu), route structure for all app pages (even as placeholders), empty-state components matching the tone in `../../RootPulse-frontend/docs/plan/01-design-system.md` |
| Backend | None new |
| Database | None new |
| Infra | None new |
| Dependencies | Phase 3 |
| Testing | Route protection test (unauthenticated access redirects to login); nav renders correctly |
| Definition of done | Every top-level route in `../../RootPulse-frontend/docs/plan/00-frontend-architecture.md` exists and renders an appropriate empty state |
| Complexity | Low-Medium | Risk | Low |

**Checkpoint:** a fresh account, after login, lands on a dashboard that looks intentional even with zero monitors.

---

### Phase 5 — Monitor Management

| | |
| --- | --- |
| Objective | Full CRUD for all 8 monitor types, matching the single-scrollable-form pattern from the PRD's confirmed UptimeRobot screenshots |
| Frontend | Monitor list (search/filter/sort), monitor detail page skeleton, the full creation/edit form (all 8 types' config, sticky section nav) |
| Backend | `/monitors/*` endpoints, per-type config validation, SSRF target-validation module (`04-security.md`) wired in at creation time |
| Database | `Monitor`, `AlertContact`, `MonitorAlertContact`, `MaintenanceWindow` models |
| Infra | None new |
| Dependencies | Phase 3 (auth), Phase 4 (shell) |
| Testing | Creation/validation tests for all 8 types incl. SSRF rejection cases |
| Definition of done | A user can create, edit, pause, resume, and delete any of the 8 monitor types; `next_check_at` is set correctly on create — but nothing checks it yet (that's Phase 6) |
| Complexity | High (the form alone has real surface area across 8 types) | Risk | Config-shape drift between frontend Zod and backend serializers — mitigated by the OpenAPI-generated client, not manual sync |

**Checkpoint:** a monitor can be created and inspected in the Django admin, sitting "pending" with a real `next_check_at`.

---

### Phase 6 — Monitoring Engine

| | |
| --- | --- |
| Objective | Monitors actually get checked, from multiple regions, and results are recorded |
| Frontend | Monitor detail page shows real check history, current status, response-time chart |
| Backend | `run_scheduler_once`/`run_notifications_once` management commands, the signed `/internal/run-due-checks` endpoint that invokes them, `ThreadPoolExecutor` dispatch, `httpx`-based execution per monitor type (`03-monitoring-engine.md` §13), the 2 Cloudflare Worker probers, `/internal/probe` |
| Database | `Check` model + indexes |
| Infra | 2 Cloudflare Workers deployed (regional probers); the GitHub Actions scheduled workflow that calls `/internal/run-due-checks` every 5 minutes (also keeps the Render free service from sleeping) |
| Dependencies | Phase 5 |
| Testing | The concurrency/double-claim test, per-type execution tests, timeout handling, prober round-trip test |
| Definition of done | A real monitor against a real target produces real `Check` rows on schedule, from 3 regions where applicable, with correct timing breakdown |
| Complexity | Very High — this is the technical core of the product | Risk | "Ping" is TCP-connect, not ICMP (`03-monitoring-engine.md` §13 note) since neither Render nor Cloudflare Workers expose raw sockets — a platform constraint to design around, not a deployment detail to fix |

**Checkpoint:** watch a monitor go from "pending" to "up" with real response-time data appearing within minutes of creation.

---

### Phase 7 — Incident Detection & Management

| | |
| --- | --- |
| Objective | Confirmed failures become incidents with a full, usable lifecycle |
| Frontend | Incident list (filter/sort/export), incident detail (timeline, comments, root-cause display once Phase 10 lands), postmortem trigger |
| Backend | Multi-region confirmation logic, `Incident`/`IncidentComment`/`IncidentEvent` creation/resolution (`03-monitoring-engine.md` §8–11), `/incidents/*` endpoints |
| Database | `Incident`, `IncidentComment`, `IncidentEvent` models |
| Infra | None new |
| Dependencies | Phase 6 |
| Testing | The full failure→confirm→incident→recovery state-machine tests |
| Definition of done | A deliberately broken test target produces exactly one incident (not one per region, not one per flaky blip), and recovers cleanly |
| Complexity | High | Risk | Getting the "2 of 3 regions" confirmation logic exactly right under real network flakiness — worth deliberately testing against an intentionally unstable target |

**Checkpoint:** break a real test endpoint on purpose, watch an incident open within the confirmation window, fix it, watch the incident close.

---

### Phase 8 — Notifications

| | |
| --- | --- |
| Objective | Every incident event reaches the user through their configured channels |
| Frontend | Alert-contact management UI, per-monitor contact attachment, delivery status visibility |
| Backend | `run_notifications` command, all 6 channel adapters (`03-monitoring-engine.md` §12), recurring notification logic, maintenance-window exclusion |
| Database | `NotificationDelivery` model |
| Infra | Resend account live; Slack/Discord/Telegram app credentials for outbound webhook/bot use where needed |
| Dependencies | Phase 7 |
| Testing | Per-channel delivery tests (mocked providers), recurring-cadence test, maintenance-window suppression test |
| Definition of done | An incident on a monitor with 3 different attached channels produces 3 successful deliveries, and a maintenance-windowed monitor's failure produces none |
| Complexity | Medium-High | Risk | Real provider API quirks (Telegram chat-ID discovery, Slack webhook formatting) — budget time for each channel individually, they're not interchangeable |

**Checkpoint:** a real Slack/Discord/Telegram message and a real email arrive within seconds of a confirmed incident.

> **Status: built.** Everything above is implemented and tested against stubbed providers. The checkpoint needs real credentials (a Slack or Discord webhook, a Telegram bot) which only you can create; email already works locally. See the Notifications section of the README.

---

### Phase 9 — Analytics Dashboard

| | |
| --- | --- |
| Objective | Per-monitor and fleet-wide analytics, fast, from pre-aggregated data |
| Frontend | Per-monitor analytics tab (uptime %, response-time trend, MTTR, timing percentiles), fleet overview dashboard (top offenders, incident volume trend) |
| Backend | `run_rollups` command, `/monitors/{id}/analytics`, fleet summary endpoint |
| Database | `CheckRollupHourly`, `CheckRollupDaily`, `FleetSummaryDaily` (`03-monitoring-engine.md` §14) |
| Infra | None new |
| Dependencies | Phase 6 (needs real check data to aggregate) |
| Testing | Rollup correctness against known synthetic check data; performance test that a dashboard load never scans raw `Check` rows beyond the 24–48h window |
| Definition of done | A dashboard with weeks of accumulated data still loads fast, because it's reading rollups, not raw history |
| Complexity | Medium-High | Risk | Getting the rollup job's idempotency right (re-running it must not double-count) |

**Checkpoint:** the fleet overview correctly identifies which monitor caused the most downtime this month.

> **Status: built.** See the Analytics section of the README. The checkpoint is a test (three monitors with known incidents are ranked correctly, and a shorter range changes who is on top).

---

### Phase 10 — Deployment-Aware Incidents & Root-Cause Timing

| | |
| --- | --- |
| Objective | Incidents explain themselves, not just report themselves |
| Frontend | Incident detail shows "started 90s after deploy #4821" and the DNS/TCP/TLS/TTFB breakdown with the regressed stage flagged |
| Backend | `/deploys/webhook/{source}` (signed per source, `04-security.md` §13), correlation-window matching, root-cause stage calculation |
| Database | `Deploy` model |
| Infra | None new |
| Dependencies | Phase 7 |
| Testing | Correlation-window matching tests (correct service, correct window, no false links); root-cause calculation against known timing fixtures |
| Definition of done | Pushing to a connected repo, then breaking the deployed service, produces an incident correctly linked to that deploy |
| Complexity | Medium | Risk | Matching a deploy to "the right" monitor when service names are ambiguous — needs a clear, documented tagging convention on the monitor |

**Checkpoint:** RootPulse itself is set up to monitor its own deploys as the first real dogfood test of this feature.

> **Status: built.** See the Deploys section of the README. The dogfood step needs the deployed app (connect the repo's webhook to the production API and tag the production monitor).

---

### Phase 11 — Dependency Monitoring

| | |
| --- | --- |
| Objective | Track ~15–20 major providers without licensing a dataset |
| Frontend | Provider browse/subscribe UI, subscribed-provider incident feed |
| Backend | `/providers/*`, a polling job per provider's public status feed, seed data for the curated list |
| Database | `Provider`, `ProviderIncident`, `UserProviderSubscription` |
| Infra | None new |
| Dependencies | Phase 8 (routes through existing alert contacts) |
| Testing | Feed-parsing tests per provider format (these will differ — Statuspage.io-style JSON is common but not universal) |
| Definition of done | A real provider incident (e.g. a real past GitHub or Cloudflare outage, replayed from their historical feed) shows up correctly and, if subscribed, notifies |
| Complexity | Medium | Risk | Provider feed formats aren't standardized — expect to write a small adapter per provider, not one universal parser |

**Checkpoint:** subscribing to GitHub's status feed and seeing their real current status reflected accurately.

> **Status: built.** See the Dependency monitoring section of the README. All 20 providers read live; the replay of GitHub's real "Actions Job Delays" incident is a test (opens and alerts when replayed mid-incident, resolves and notifies afterwards).

---

### Phase 12 — Status Pages

| | |
| --- | --- |
| Objective | A public, branded status page exists for the user's monitors |
| Frontend | Status page builder (branding, components, announcements), the public-facing rendered page itself (a separate, unauthenticated Next.js route) |
| Backend | `/status-pages/*` and `/public/status-pages/*` |
| Database | `StatusPage`, `StatusPageComponent`, `StatusPageAnnouncement`, `StatusPageSubscriber` |
| Infra | None new (custom domain support noted as a stretch goal within this phase, not blocking) |
| Dependencies | Phase 6 (needs real monitor status to display) |
| Testing | Public-page access-control tests (password, unpublished pages), subscriber double opt-in flow |
| Definition of done | A public status page is reachable by anyone with the link, reflects real monitor status, and a comment toggled visible actually appears there |
| Complexity | Medium-High | Risk | The public page is a distinct security surface (unauthenticated) — review it explicitly in Phase 16, not just trust Phase 12's own tests |

**Checkpoint:** RootPulse's own public status page, for RootPulse itself, is live and linked from the landing page footer.

> **Status: built.** See the Status pages section of the README. The checkpoint needs the deployed app: create the page, add RootPulse's own monitors, publish it, and set `NEXT_PUBLIC_STATUS_PAGE_URL` on the frontend so the footer's Status link points at it. Custom domains (the stretch goal) are not built. The public surface is on the Phase 16 review list.

---

### Phase 13 — Reports & Export

| | |
| --- | --- |
| Objective | On-demand PDF/CSV export |
| Frontend | Export buttons on incidents list and status page |
| Backend | `/reports/*`, PDF generation (open-source library, headless-browser or server-side) |
| Database | None new |
| Infra | None new |
| Dependencies | Phase 7, Phase 12 |
| Testing | Generated file correctness (parse the PDF/CSV back and assert content matches source data) |
| Definition of done | A downloaded PDF/CSV accurately reflects the selected date range |
| Complexity | Low-Medium | Risk | PDF rendering libraries can be finicky about fonts/layout in a server environment — test in the actual Render deployment container, not just locally |

**Checkpoint:** export a real month of incident data and confirm the numbers match the dashboard.

> **Status: built.** See the Reports section of the README. The checkpoint is a test (a report and the fleet analytics agree for the same period) and was also checked against the real dev database. The remaining step the roadmap asks for, trying the PDF in the actual Render container, needs the deployment; ReportLab is pure Python so no system packages should be needed.

---

### Phase 14 — Onboarding & Polish

| | |
| --- | --- |
| Objective | The gamified onboarding checklist and consistent empty/success-state tone across the whole app |
| Frontend | Onboarding checklist widget (create monitor → set up alert channel → connect deploy webhook → create status page), empty/success-state copy pass across every page |
| Backend | Onboarding-progress tracking (a simple `User` field or small related model) |
| Database | Minor addition (onboarding progress) |
| Infra | None new |
| Dependencies | Phases 5, 8, 10, 12 (the checklist references real features) |
| Testing | Checklist state transitions tested; copy reviewed against the tone guide in the frontend design-system doc |
| Definition of done | A brand-new account sees a guided path through the product's real differentiators, not a generic "welcome" screen |
| Complexity | Low-Medium | Risk | Low — mostly content and small UI work |

**Checkpoint:** a fresh signup completes the whole checklist and understands what makes RootPulse different, not just that it works.

> **Status: built.** See the Onboarding section of the README and the voice-and-tone guide (section 8 of the frontend design-system doc, enforced by `lib/tone.test.ts`). The checklist has five steps, not four (dependencies was added).

---

### Phase 15 — API & Developer Platform

| | |
| --- | --- |
| Objective | The same API the frontend uses is a real, documented, public developer surface |
| Frontend | API key management UI |
| Backend | `/api-keys/*`, API-key auth as an alternative to JWT on all endpoints, published OpenAPI docs page |
| Database | `ApiKey` |
| Infra | Public API docs hosted (can be the OpenAPI schema served directly, or a generated docs page) |
| Dependencies | Phase 3 |
| Testing | API-key auth test matrix (scopes, revocation, matches `04-security.md` §3) |
| Definition of done | A `curl` request with a generated API key against production successfully lists monitors |
| Complexity | Medium | Risk | Low — this mostly formalizes what already exists |

**Checkpoint:** the same API a `curl` script uses is exactly the one the dashboard uses — no shadow "public API" reimplementation.

> **Status: built.** See the API keys section of the README. The definition of done (a `curl` with a generated key lists monitors) was run against the local stack; the same call against production needs the deployment. Keys are hashed with SHA-256 rather than Argon2, for the reason given there.

---

### Phase 16 — Security Hardening

| | |
| --- | --- |
| Objective | A deliberate pass, not an assumption that earlier phases got it all right |
| Frontend | Review for any lingering `dangerouslySetInnerHTML` or unescaped user content |
| Backend | Full SSRF test suite run against the real prober deployment (not just mocks), rate-limit tuning based on real usage patterns, dependency vulnerability scan |
| Database | Review indexes added for every `user_id`-scoped query (permission-boundary correctness depends on these existing) |
| Infra | Cloudflare Worker prober isolation confirmed (they hold no credentials for and have no path to the backend's database or internal services); Sentry alerting on anomalous error rates |
| Dependencies | Effectively all prior phases |
| Testing | Every scenario in `05-testing-deployment-devex.md` §Critical scenarios re-run explicitly as a gate, plus the `security-review` process on the final diff before production |
| Definition of done | The SSRF defenses in `04-security.md` are verified against real attempted targets (a real internal address, a real DNS-rebinding domain set up for the test) in the staging environment |
| Complexity | High (verification is slower than building) | Risk | This is the phase most tempting to rush — don't |

> **Status: code-side built; deployed verification outstanding.** See `07-security-review.md`. Done: the whole-API security gate, a code review with fixes, dependency scans, hardened production settings and headers, the frontend review. Outstanding because it needs real infrastructure: the SSRF suite against real targets and a real rebinding domain in staging, prober isolation, rate-limit tuning on real traffic, Sentry alerting, and the `security-review` pass on the final diff. All are listed as a checklist in the review.

**Checkpoint:** a genuine attempted SSRF against staging (pointed at staging's own internal address) is blocked and logged.

---

### Phase 17 — Testing & QA

| | |
| --- | --- |
| Objective | Full coverage confidence before production traffic |
| Frontend | E2E suite covers every flow in `../../RootPulse-frontend/docs/plan/00-frontend-architecture.md`'s route table |
| Backend | Coverage report reviewed for gaps, not just a percentage target |
| Database | Migration rollback tested (can a bad migration be reverted cleanly on staging) |
| Infra | Load-test the scheduler at a realistic monitor count (dozens) to confirm the thread-pool sizing from `01-tech-stack.md` holds |
| Dependencies | All prior phases |
| Testing | This phase *is* testing |
| Definition of done | Every "Critical scenario" in `05-testing-deployment-devex.md` passes in CI, on staging, against real (not mocked) external services at least once |
| Complexity | Medium-High | Risk | Flaky E2E tests against real external targets — mitigate with a small set of dedicated, stable test targets rather than the public internet |

**Checkpoint:** a full CI run, clean, against a staging environment that has been exercised with real traffic for at least a few days.

---

### Phase 18 — Deployment to Production

| | |
| --- | --- |
| Objective | Go live |
| Frontend | Production Vercel deploy, custom domain, analytics/error tracking confirmed live |
| Backend | Production Render deploy (API + triggered scheduler/notifier endpoints), production Cloudflare Worker probers, production Neon database, production secrets rotated (never reuse staging secrets) |
| Database | Production migrations run, seed data (curated providers) loaded |
| Infra | DNS cutover, SSL confirmed, the production GitHub Actions cron trigger enabled, the external "who watches the watcher" check pointed at production |
| Dependencies | Phase 17 |
| Testing | Smoke test suite run against production immediately post-deploy |
| Definition of done | A real monitor, created by the real product owner, on the real product, checking a real site, alerting through a real channel |
| Complexity | Medium (mechanically) | Risk | First-real-traffic surprises are normal — have the rollback plan (previous Render deploy, previous Vercel deployment) ready before cutover, not improvised during it |

**Checkpoint:** RootPulse is monitoring itself, in production, for real.

---

## Master roadmap

```
RootPulse
│
├── Phase 0  — Repository & Infrastructure Setup
├── Phase 1  — Frontend Foundation
├── Phase 2  — Public Landing Page
├── Phase 3  — Backend Foundation & Authentication
├── Phase 4  — Core Application Shell
├── Phase 5  — Monitor Management
├── Phase 6  — Monitoring Engine
├── Phase 7  — Incident Detection & Management
├── Phase 8  — Notifications
├── Phase 9  — Analytics Dashboard
├── Phase 10 — Deployment-Aware Incidents & Root-Cause Timing
├── Phase 11 — Dependency Monitoring
├── Phase 12 — Status Pages
├── Phase 13 — Reports & Export
├── Phase 14 — Onboarding & Polish
├── Phase 15 — API & Developer Platform
├── Phase 16 — Security Hardening
├── Phase 17 — Testing & QA
└── Phase 18 — Deployment to Production
```

Phases 0–4 are strictly sequential (each depends on the last). From Phase 5 onward, Phases 9, 10, 11, 13, and 15 can run in parallel with each other once their stated dependency phase is done, if there's ever more than one contributor — noted explicitly since the PRD's single-user framing doesn't preclude a small dev team building it.
