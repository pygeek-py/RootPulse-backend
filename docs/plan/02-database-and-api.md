# Database & API Architecture

Single-user per account (no Organization/Membership/Role models — PRD Section 5.2 explicitly excludes teams for MVP). Every table below scopes to `user_id` directly rather than through a tenancy layer that isn't needed yet. If multi-user ever becomes real (V2+), the migration path is adding an `Organization` model and re-pointing these `user_id` FKs to it — not a rewrite, but deliberately not built now.

## 1. Entity model

| Entity (Django model) | Purpose | Key fields | Relationships | Indexes / constraints | Lifecycle notes |
| --- | --- | --- | --- | --- | --- |
| `User` (custom) | Account owner | `email` (unique), unusable password (sign-in is passwordless), `github_id` (nullable, unique), `timezone`, `created_at` | Owns everything below | Unique index on `email`, `github_id` | Custom user model set on day 1 (Django convention: never retrofit this later) |
| `AlertContact` | A reusable notification destination | `channel` (email/webhook/telegram/discord/slack/push), `config` (JSONB — e.g. webhook URL, chat ID), `delay_seconds`, `repeat_interval_seconds` | belongs to `User`; M2M with `Monitor` via `MonitorAlertContact` | Index on `user_id` | Deleting a contact that's attached to monitors detaches rather than cascades — never silently orphan a monitor's alerting |
| `Monitor` | One thing being checked | `type` (http/keyword/ping/port/ssl/domain/cron/dns), `name`, `target`, `config` (JSONB, shape varies by type), `interval_seconds`, `status` (pending/up/down/paused), `next_check_at`, `last_check_at` | belongs to `User`; has many `Check`, `Incident`; M2M `AlertContact` | Index on `(next_check_at)` for scheduler polling; index on `user_id` | `next_check_at` is the field the scheduler's `SELECT … skip_locked` claims against |
| `Check` | One executed check result | `region`, `started_at`, `duration_ms`, `success`, `status_detail`, `dns_ms`, `tcp_ms`, `tls_ms`, `ttfb_ms`, `response_time_ms` | belongs to `Monitor` | Index on `(monitor_id, started_at desc)`; partitioning/rollup strategy in `03-monitoring-engine.md` §Analytics | High-volume, append-only table — retention/rollup policy matters (PRD NFR: 90 days minimum, older rolled up) |
| `Incident` | A confirmed outage/anomaly | `started_at`, `ended_at` (nullable = ongoing), `reason`, `excluded_from_reports`, `deploy_id` (nullable FK), `root_cause_stage` (nullable: dns/tcp/tls/server) | belongs to `Monitor`; optionally references `Deploy`; has many `IncidentComment`, `IncidentEvent` | Index on `(monitor_id, started_at desc)`; index on `ended_at IS NULL` (ongoing lookup) | `ended_at IS NULL` is the "ongoing" state the recurring-notification job polls |
| `IncidentComment` | User-authored note | `body`, `visible_on_status_page`, `created_at` | belongs to `Incident` | Index on `incident_id` | Toggling `visible_on_status_page` off must remove it from the public status page immediately, not just for new page loads |
| `IncidentEvent` | System-authored timeline entry (distinct from user comments) | `kind` (opened/recheck_confirmed/notification_sent/deploy_linked/resolved), `metadata` (JSONB), `created_at` | belongs to `Incident` | Index on `incident_id` | Feeds the postmortem template's pre-filled timeline (PRD §6.3) |
| `Deploy` | An ingested deploy event | `source` (github_actions/vercel/render/generic), `external_ref`, `service_name`, `occurred_at`, `raw_payload` (JSONB) | referenced by `Incident` (nullable) | Index on `(service_name, occurred_at desc)` | Correlation window (default 5 min) matches on `service_name` vs. the monitor's configured deploy-service tag |
| `NotificationDelivery` | One attempted send | `channel`, `status` (pending/sent/failed), `attempt_count`, `next_attempt_at`, `sent_at`, `error` | belongs to `Incident` (nullable — also used for recovery/status-page-subscriber sends) and `AlertContact` | Index on `(status, next_attempt_at)` for the dispatcher's claim query | Same `skip_locked` claim pattern as the scheduler |
| `StatusPage` | A public status page | `name`, `slug` (unique), `custom_domain` (nullable, unique), `password_hash` (nullable), `branding` (JSONB), `is_public` | belongs to `User`; has many `StatusPageComponent`, `StatusPageAnnouncement`, `StatusPageSubscriber` | Unique index on `slug`, `custom_domain` | MVP limit: 1 per user (PRD §6.4) — enforced in the API, not the schema, since that limit is a product decision, not a data-integrity one |
| `StatusPageComponent` | A monitor shown on a status page | `display_name`, `order`, `group` (nullable) | belongs to `StatusPage` and `Monitor` | Unique `(status_page_id, monitor_id)` | |
| `StatusPageAnnouncement` | An incident/maintenance update | `title`, `body`, `kind` (incident/maintenance), `created_at` | belongs to `StatusPage` | Index on `status_page_id` | |
| `StatusPageSubscriber` | Email opt-in | `email`, `confirmed`, `unsubscribe_token` (unique) | belongs to `StatusPage` | Unique `(status_page_id, email)` | Double opt-in required to keep Resend's free-tier reputation clean |
| `MaintenanceWindow` | Scheduled downtime, excluded from alerting/uptime | `starts_at`, `ends_at`, `recurrence_rule` (nullable) | M2M with `Monitor` | Index on `(starts_at, ends_at)` | Scheduler and incident-evaluation both check this before treating a failure as real |
| `Provider` | A tracked 3rd-party dependency | `name`, `slug`, `status_feed_url` | has many `ProviderIncident` | Unique `slug` | Seeded/curated list of ~15–20 (PRD §6.6), not user-created |
| `ProviderIncident` | An ingested provider outage | `external_id`, `title`, `status`, `started_at`, `ended_at` (nullable), `raw_update` (JSONB) | belongs to `Provider` | Unique `(provider_id, external_id)`; index on `started_at` | Polled on a schedule, not pushed |
| `UserProviderSubscription` | Which providers a user tracks | — (through table) | M2M `User` ↔ `Provider` | Unique `(user_id, provider_id)` | Does not consume the monitor quota (PRD §6.6) |
| `ApiKey` | Programmatic access credential | `name`, `key_hash`, `scope` (read/full), `last_used_at`, `revoked_at` (nullable) | belongs to `User` | Index on `key_hash` | Only the hash is stored; the raw key is shown once at creation |
| `AuditLog` | Security-relevant action trail | `action`, `target_type`, `target_id`, `metadata` (JSONB), `ip_address`, `created_at` | belongs to `User` | Index on `(user_id, created_at desc)` | Login, API key creation/revocation, monitor deletion, status-page password changes |

## 2. API architecture

REST, versioned under `/api/v1/`. DRF `ModelViewSet`s where CRUD is symmetric, plain `APIView`s where it isn't (auth, scheduler-internal endpoints, reports). All authenticated endpoints require a valid JWT (`Authorization: Bearer …`) unless noted. All list endpoints are paginated (cursor pagination for `checks`/`incidents`, since they're append-heavy).

### `/api/v1/auth`

| Method | Route | Purpose | Auth | Request | Response | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/auth/register` | Start sign-up: email a link + 6-digit code (account is created when it is redeemed) | none | `email` | 204 (always) | 400 invalid email, 429 |
| POST | `/auth/login` | Email a sign-in link + code (nothing is sent for unknown/disabled addresses) | none | `email` | 204 (always, to avoid enumeration) | 400, 429 |
| POST | `/auth/verify` | Redeem the link token **or** email + code; creates the account on first use | none | `token` or `email`+`code` | `user`, `access_token` (+ refresh cookie) | 400 invalid/expired/used (generic), 429 |
| POST | `/auth/logout` | Invalidate refresh token | refresh cookie | — | 204 | 401 |
| POST | `/auth/refresh` | Rotate access token | refresh cookie | — | new `access_token` (+ rotated refresh cookie) | 401 expired/invalid |
| GET | `/auth/github/redirect` | Start GitHub OAuth | none | — | 302 to GitHub | — |
| GET | `/auth/github/callback` | Complete GitHub OAuth | none | `code`, `state` | `user`, `access_token` (+ refresh cookie) | 400 state mismatch, 502 GitHub error |
| GET | `/auth/me` | Current user | JWT | — | `user` | 401 |

### `/api/v1/monitors`

| Method | Route | Purpose | Auth | Request | Response | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/monitors` | List (filter/sort/search) | JWT | query: `status`, `type`, `q`, `sort` | paginated `Monitor[]` | 401 |
| POST | `/monitors` | Create | JWT | type-specific config (validated against a per-type Zod-mirrored serializer) | `Monitor` | 400, 422 (e.g. target fails SSRF validation, `04-security.md`) |
| GET | `/monitors/{id}` | Detail (incl. recent checks summary) | JWT | — | `Monitor` + summary stats | 404 |
| PATCH | `/monitors/{id}` | Update config | JWT | partial fields | `Monitor` | 400, 404 |
| DELETE | `/monitors/{id}` | Delete | JWT | — | 204 | 404 |
| POST | `/monitors/{id}/pause` | Pause | JWT | — | `Monitor` | 404 |
| POST | `/monitors/{id}/resume` | Resume | JWT | — | `Monitor` | 404 |
| POST | `/monitors/{id}/test-notification` | Send a test alert to attached contacts | JWT | — | 202 | 404 |
| GET | `/monitors/{id}/checks` | Paginated check history | JWT | query: `since`, `until`, `region` | paginated `Check[]` | 404 |
| GET | `/monitors/{id}/analytics` | Per-monitor analytics (PRD §6.5) | JWT | query: `range` | uptime %, response-time series, MTTR, timing percentiles | 404 |

### `/api/v1/incidents`

| Method | Route | Purpose | Auth | Request | Response | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/incidents` | List (filter/sort) | JWT | query: `status`, `monitor_id`, `start_date`, `end_date`, `sort` | paginated `Incident[]` | 401 |
| GET | `/incidents/{id}` | Detail incl. timeline (`IncidentEvent[]`), root-cause breakdown, linked deploy | JWT | — | `Incident` | 404 |
| POST | `/incidents/{id}/comments` | Add comment | JWT | `body`, `visible_on_status_page` | `IncidentComment` | 400, 404 |
| PATCH | `/incidents/{id}` | Exclude-from-reports toggle, manual close | JWT | `excluded_from_reports`? | `Incident` | 404 |
| POST | `/incidents/{id}/postmortem` | Generate pre-filled postmortem doc | JWT | — | postmortem markdown/structure | 404 |
| GET | `/incidents/export` | CSV export | JWT | query: same filters as list | CSV file | 401 |

### `/api/v1/alert-contacts`, `/api/v1/maintenance-windows`

Standard CRUD (`GET/POST /alert-contacts`, `GET/PATCH/DELETE /alert-contacts/{id}`; same shape for `/maintenance-windows`) — omitted row-by-row here since they follow the exact REST pattern above with no special cases.

### `/api/v1/status-pages`

| Method | Route | Purpose | Auth | Notes |
| --- | --- | --- | --- | --- |
| GET/POST | `/status-pages` | List / create (max 1 enforced here) | JWT | |
| GET/PATCH/DELETE | `/status-pages/{id}` | Manage | JWT | |
| POST | `/status-pages/{id}/components` | Add a monitor as a component | JWT | |
| DELETE | `/status-pages/{id}/components/{component_id}` | Remove | JWT | |
| POST | `/status-pages/{id}/announcements` | Post an update | JWT | |
| GET | `/public/status-pages/{slug}` | **Public**, no auth | Rendered data for the public page (password-checked if set) |
| POST | `/public/status-pages/{slug}/subscribe` | **Public** | Email subscribe (double opt-in) |
| GET | `/public/status-pages/{slug}/unsubscribe/{token}` | **Public** | One-click unsubscribe |

### `/api/v1/providers` (dependency monitoring)

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/providers` | Browse the curated ~15–20 provider list |
| POST | `/providers/{id}/subscribe` | Track a provider (attach alert contacts) |
| DELETE | `/providers/{id}/subscribe` | Untrack |
| GET | `/providers/subscribed` | List what the user tracks + recent `ProviderIncident`s |

### `/api/v1/deploys`, `/api/v1/reports`, `/api/v1/api-keys`

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/deploys/webhook/{source}` | Signed inbound deploy webhook (GitHub Actions/Vercel/Render/generic) |
| GET | `/reports/status-page/{id}.pdf` | On-demand PDF export |
| GET | `/reports/incidents.csv` | Alias of the incidents export above |
| GET/POST | `/api-keys` | List / create (raw key shown once) |
| DELETE | `/api-keys/{id}` | Revoke |

### Internal-only (not part of the public API surface)

| Method | Route | Purpose | Auth |
| --- | --- | --- | --- |
| POST | `/internal/probe` | Scheduler → prober: execute one check | shared secret (HMAC-signed request) |
| POST | `/internal/checks/ingest` | Prober → scheduler: return a result (if probers can't reach Postgres directly) | shared secret |

Full request/response bodies are defined as DRF serializers in code, not duplicated here; this table is the contract for what exists and why, matching what `drf-spectacular` will publish as the live OpenAPI schema the frontend generates its client from.
