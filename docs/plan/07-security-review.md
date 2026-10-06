# Security review (Phase 16)

A deliberate pass over everything built in Phases 0 to 15, as the roadmap asks: not an assumption that earlier phases got it right. This is what was checked, what was found, what was changed, and what can only be verified on the deployed system.

## How it was done

1. **Crawled the whole API instead of trusting each feature.** `tests/test_security_gate.py` walks every route in the URLconf and every operation in the published schema, so a new endpoint is covered the day it exists:
   - the routes that answer without signing in are exactly the reviewed list (below), and everything else requires a signed-in user and accepts only the two credentials (dashboard token, API key);
   - every endpoint in the schema refuses an anonymous caller;
   - a read-only API key is refused on every operation that changes something, and a full key is refused on all key management;
   - user B reaches none of user A's objects by any id through any endpoint, and B's lists and reports never contain A's data;
   - no endpoint returns a 500 to junk input.
   The gate was proven able to fail: removing an ownership filter and removing the read-scope check were each caught by it, then restored.
2. **Read the code** for the classes of mistake a crawl can't see (below).
3. **Scanned dependencies** (`pip-audit`, `npm audit`).
4. **Pinned each finding with a test** (`tests/test_security_hardening.py`).

## Findings, and what changed

| # | Finding | Severity | Change | Pinned by |
| --- | --- | --- | --- | --- |
| 1 | Django 5.1.15 had 8 known vulnerabilities (fixes only in 5.2.x) | High | Upgraded to Django 5.2.17 (the long-term-support line); `pip-audit` now reports none | `pip-audit` in requirements, run it before each deploy |
| 2 | The SSRF validator accepted numeric-shorthand host names (`127.1`, `0x7f.0.0.1`, `0177.0.0.1`) and relied on the system resolver to turn them into 127.0.0.1; it also let IPv4-compatible IPv6 forms (`::127.0.0.1`, `::a9fe:a9fe`), deprecated ranges (`fec0::/10`, `192.88.99.0/24`, `5f00::/16`, IPv4-translated `::ffff:0:0:0/96`) through as "public" | Medium (the connection is pinned to a vetted address, so exploitation needed a resolver that disagreed, but the validator shouldn't depend on that) | Refused by the validator itself, in **both** implementations (`monitoring/target_validation.py` and the Worker's `ip.js`/`validate.js`), driven by the shared fixture `tests/fixtures/ssrf_cases.json` | `tests/test_ssrf.py`, `workers/prober/test/validate.test.js`, `TestAddressSpellings` |
| 3 | The dependency poller followed redirects without checking where they led | Medium | Every request, including each redirect hop (max 3), must resolve to public addresses only. Also found that Anthropic's status page had moved (a 301), so its catalogue entry now points at `status.claude.com` | `TestProviderRedirects` |
| 4 | A monitor, contact, deploy-source or maintenance-window name could hold a newline. Django refuses a newline in an email header, so every email alert for that monitor failed (retried, then lost) | Medium (reliability of alerting; not an injection) | Names are collapsed to one line on the way in, and the alert email's subject is one line whatever old data holds | `TestNamesAreOneLine` |
| 5 | Production served without HSTS, without redirecting HTTP to HTTPS, and with non-`Secure` session/CSRF cookies (`manage.py check --deploy` listed 4 warnings) | Medium | `SECURE_SSL_REDIRECT` (the health probe exempt), a year of HSTS, secure cookies; `check --deploy --fail-level WARNING` is now clean and is a test | `TestProductionSettings` |
| 6 | API responses could be cached by a shared cache and carried no content policy | Low | Every `/api/` and `/internal/` response carries `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `nosniff` (the interactive docs page is left able to load its own scripts) | `TestResponseHeaders` |
| 7 | The Django admin lived at the well-known `/admin/` | Low | Address is configurable (`DJANGO_ADMIN_URL`); set it to something unguessable in production. The admin never shows a key hash, status-page password hash or unsubscribe token | `TestAdmin`, `TestProductionSettings` |
| 8 | The frontend sent no security headers | Medium | A content security policy (only this site and the API for scripts, connections, workers; no framing, plugins or base-URI changes), `X-Frame-Options`, `nosniff`, referrer and permissions policies, HSTS in production. `react/no-danger` is now a lint error | `lib/security-headers.test.ts`, `e2e/security.spec.ts` |
| 9 | Links built from data (provider incident pages, deploy URLs, a status page's support link and logo) trusted the server's validation alone | Low (defence in depth) | `safeHref()` lets only http(s) through. It also found that a rejected logo address still left an empty `<img>`; that element is now not rendered | `lib/safe-url.test.ts`, `e2e/security.spec.ts` |
| 10 | Wrong API keys were refused before the API's throttles ran, so they weren't limited | Low | Their own per-address limit (found and fixed in Phase 15; re-verified here) | `tests/test_api_keys.py::TestRateLimits` |

Not changed, with reasons:

- `npm audit` still lists the `eslint-config-next` chain (via `fast-glob`, `micromatch`, `braces`). It is development tooling, never shipped, and npm's suggested "fix" is a downgrade to Next 14's config. Production dependencies report no vulnerabilities (`npm audit --omit=dev`). Revisit when `eslint-config-next` ships an update.
- The content policy still allows inline scripts, because Next.js writes its own into every page. A nonce-based policy is the next tightening and needs every page rendered per request.

## Checked and found sound

- **Authorization:** every viewset filters by the signed-in user; the crawl above proves it end to end.
- **Authentication:** sign-in is passwordless with hashed single-use tokens; the refresh cookie is `HttpOnly`, `Secure`, rotated and revocable; GitHub sign-in uses only a *verified* primary email and a checked `state`; forged (`alg: none`, wrong key), refresh-as-access, deleted-user and status-page tokens are all refused.
- **Injection:** no raw SQL anywhere (the one queue-claim query uses the ORM's `skip_locked`); no `eval`, `pickle`, `subprocess`, `mark_safe` or `csrf_exempt`.
- **Secrets:** compared with `hmac.compare_digest` everywhere (signatures, OAuth state, codes, shared secrets); nothing logs a token, key, webhook URL or code; API keys, status-page passwords and unlock tokens are stored only as hashes and never returned.
- **Mass assignment:** no serializer uses `__all__`; a client can't set an owner, an id, a status, `verified_at`, a password hash, a key hash or an incident's times.
- **Outbound requests:** monitor checks pin the connection to a vetted address and validate each redirect; user webhooks use the same `safe_post`; Slack and Discord webhooks must match a fixed host pattern; browser-push endpoints must be on a real push service's domain; Telegram is a fixed host; provider feeds are a fixed catalogue.
- **XSS:** no `dangerouslySetInnerHTML` or `innerHTML` anywhere; hostile titles, notes, names, descriptions and links from the API render as text and never run (`e2e/security.spec.ts`).
- **CORS and CSRF:** an explicit origin allowlist (no wildcard); state-changing calls need a bearer token; the cookie-based refresh checks the request's `Origin`.
- **Indexes:** every foreign key to a user or a monitor is indexed, and the hot lookups have composite indexes (tested).
- **Inbound webhooks:** each provider's signature scheme is verified before the body is read; a connection without a secret trusts nobody.

## Accepted, and why

- **The refresh cookie is `SameSite=None` in production** (the plan said `Strict`). The dashboard (Vercel) and the API (Render) are different sites, so the browser won't send a `Strict` cookie to the API. The cookie is `Secure` and `HttpOnly`, scoped to `/api/v1/auth/`, and the refresh endpoint refuses any request whose `Origin` isn't the dashboard's. Putting both under one registrable domain later would let this go back to `Strict`.
- **Per-address throttles depend on `NUM_PROXIES`.** Behind Render's proxy it must be `1`; otherwise every caller looks like the proxy (too strict) or a spoofed header is trusted (too loose). It is in the deployment checklist below.
- **Python 3.14's test client can't render Django's 404 page** (an error in the standard library's `copy` and Django's template context). It affects only the test environment; the crawl avoids unresolvable URLs for that reason.

## Critical scenarios, and the tests that prove them

Every scenario in `05-testing-deployment-devex.md` is mapped to a named test below, and `tests/test_security_gate.py::TestScenarioMap` fails if any named test is renamed or removed.

| Scenario | Test |
| --- | --- |
| Monitor creation for all 8 types | `tests/test_monitors.py::TestCreateEveryType::test_creates_the_monitor_pending_with_a_real_next_check` |
| Validation rejection (bad URL, blocked target) | `tests/test_ssrf.py::test_forbidden_urls` |
| Failure, multi-region confirmation, incident, fan-out | `tests/test_notify_flow.py::TestOpening::test_an_incident_on_a_monitor_with_three_channels_produces_three_deliveries` |
| A blip not confirmed opens no incident | `tests/test_incident_lifecycle.py::TestOpening::test_a_blip_opens_nothing` |
| Single region alone is a blip | `tests/test_engine.py::TestConfirmation::test_one_region_alone_is_a_blip` |
| Recovery closes the incident and tells the right people | `tests/test_notify_flow.py::TestRecovery::test_contacts_that_were_told_are_told_it_is_fixed` |
| Deploy correlation window | `tests/test_deploys.py::TestCorrelation::test_a_deploy_outside_the_window_is_not_blamed` |
| Deploy webhook signatures | `tests/test_deploys.py::TestGenericWebhook::test_missing_or_bad_signatures_are_refused` |
| Root-cause stage from known timings | `tests/test_deploys.py::TestRootCause::test_the_stage_that_lost_the_most_time_wins` |
| Status page password gate | `tests/test_status_pages.py::TestPasswordGate::test_a_locked_page_says_only_that_it_is_locked` |
| Subscriber double opt-in | `tests/test_status_pages.py::TestDoubleOptIn::test_the_link_confirms_once` |
| Comment visibility toggle removes it from the page | `tests/test_status_pages.py::TestIncidentsOnThePage::test_a_note_appears_when_toggled_visible_and_vanishes_when_toggled_off` |
| API key: valid works | `tests/test_api_keys.py::TestSignIn::test_a_valid_key_lists_monitors` |
| API key: revoked is rejected at once | `tests/test_api_keys.py::TestSignIn::test_a_revoked_key_stops_working_at_once` |
| API key: read scope rejected on a write | `tests/test_api_keys.py::TestScopes::test_a_read_key_can_read_and_not_change` |
| User A can't reach user B's anything | `tests/test_security_gate.py::TestAccountIsolation::test_bob_reaches_none_of_alices_objects_by_any_id_through_any_endpoint` |
| SSRF: private target refused at creation | `tests/test_ssrf.py::TestResolution::test_a_public_name_that_resolves_to_a_private_ip_is_refused` |
| SSRF: DNS rebinding after creation | `tests/test_ssrf.py::TestResolution::test_check_time_resolution_catches_a_rebound_name` |
| SSRF: redirects are vetted per hop | `tests/test_checks_http.py::TestRedirects::test_every_redirect_target_is_vetted_before_it_is_followed` |
| Login rate limiting | `tests/test_auth_passwordless.py::TestHygiene::test_the_email_endpoints_are_rate_limited` |
| Retention only deletes what is already rolled up | `tests/test_analytics.py::TestRetention::test_old_raw_checks_are_deleted_once_folded_in_and_their_rollups_survive` |
| Two scheduler passes never claim the same monitor | `tests/test_engine.py::test_concurrent_passes_never_double_claim` |

## Can only be verified on the deployed system (a checklist for you)

These are the parts of Phase 16 that need real infrastructure. None can be proven from a laptop, and none should be assumed.

1. **SSRF against real targets, in staging.** From the deployed app, create monitors for a real internal address (for example the Render service's own private address, and `http://169.254.169.254/`) and confirm creation is refused; then use a domain you control whose DNS you can change after the monitor exists (point it at a public address, create the monitor, change the record to `10.0.0.1`) and confirm the next check fails as a validation error and never as "down". Run both through the **Worker probers** too, since they validate separately.
2. **Prober isolation.** Confirm in the Cloudflare dashboard that the Workers hold only `PROBER_SHARED_SECRET` and have no database credentials or bindings.
3. **Production settings.** Set `DJANGO_SECRET_KEY` (32+ random characters), `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS` (the dashboard's origin only), `FRONTEND_URL`, `NUM_PROXIES=1`, an unguessable `DJANGO_ADMIN_URL`, and `AUTH_REFRESH_COOKIE_SAMESITE` left at its production default. Run `python manage.py check --deploy` against the real environment.
4. **Rate limits under real traffic.** Confirm the per-address throttles see real client addresses (`NUM_PROXIES`), then tune from what Render's logs show.
5. **Alerting on anomalous error rates.** Add the Sentry alert rules (a spike in 5xx, in 401/403s, and in 429s).
6. **Dependencies before every deploy:** `pip-audit` and `npm audit --omit=dev`.
7. **The security review process on the final diff** before production (`/security-review` on the branch you deploy).
