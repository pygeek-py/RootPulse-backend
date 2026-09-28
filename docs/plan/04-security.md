# Security Plan

## 1. Authentication

- Password hashing: **Argon2** (via `django-argon2` / Django's built-in Argon2 hasher, set as the primary hasher) — stronger than PBKDF2 against GPU-based cracking, and Django ships first-class support.
- Sessions: short-lived JWT access token (15 min) + rotating refresh token in an **httpOnly, Secure, SameSite=Strict** cookie (30-day expiry, rotated on every use — old refresh tokens are invalidated server-side via a `RefreshToken` table storing hashes, so a stolen refresh token has a small usable window even if the cookie leaks).
- Logout invalidates the specific refresh token server-side (not just clears the cookie client-side).
- GitHub OAuth: standard authorization-code flow with `state` parameter checked for CSRF on the callback; GitHub's `id`/`email` mapped to the existing `User` by email if one exists, else a new account is created.

## 2. Authorization

Every queryset in every DRF view is filtered by `request.user` — there is no cross-user data access path because there is no code path that queries without that filter (enforced by a shared base `ViewSet` that all resource viewsets inherit from, rather than remembering to add `.filter(user=request.user)` in each one individually). Public endpoints (`/public/status-pages/{slug}`, the heartbeat ingest URL) are the explicit, narrow exception, and are reviewed as a checklist item in Phase 16.

## 3. API keys

- Only a hash (Argon2) is stored; the raw key is shown exactly once, at creation.
- Two scopes: `read` (all `GET`s) and `full` (everything). No finer-grained scoping in MVP — matches the PRD's single-user, single-key-type design.
- `last_used_at` updated async (not on the hot path) so a stale/compromised key is visible without slowing every request.
- Revocation is immediate (checked on every request, not cached).

## 4. Secrets

- Never committed — `.env` files are gitignored from Phase 0 onward, with `.env.example` committed instead.
- Local dev: `.env` file. Production: Fly.io secrets (`flyctl secrets set`) and Vercel environment variables. CI: GitHub Actions encrypted secrets.
- The shared HMAC secret used between the scheduler and the regional probers (`01-tech-stack.md`) is treated with the same rigor as a database credential.

## 5. Encryption

- TLS everywhere in transit (automatic certs, `01-tech-stack.md`).
- At rest: handled by the managed Postgres provider (Neon encrypts at rest by default) — not reimplemented at the application layer.

## 6. Rate limiting

DRF's built-in throttling (`UserRateThrottle`, `AnonRateThrottle`), tuned per endpoint class:

| Endpoint class | Limit | Why |
| --- | --- | --- |
| `/auth/login`, `/auth/password/forgot` | Aggressive (e.g. 5/min/IP) | Credential-stuffing / enumeration defense |
| `/auth/register` | Moderate (e.g. 10/hour/IP) | Abuse/spam-account defense |
| General authenticated API | Generous (matches PRD §6.10's own published limits, e.g. 60/min) | Normal dashboard use shouldn't be throttled |
| Public status-page endpoints | Moderate per-IP | These are unauthenticated and publicly linked |
| `/internal/*` | Not rate-limited by user (no user context) — instead gated entirely by the HMAC shared secret | Internal-only traffic |

## 7. CSRF

The API is token-based for all state-changing requests (`Authorization: Bearer`), which is not vulnerable to classic CSRF the way cookie-session auth is. The one cookie in the system (the refresh token) is `SameSite=Strict`, which alone prevents it from being sent on a cross-site request — belt-and-suspenders over relying on SameSite alone.

## 8. CORS

`django-cors-headers` configured with an explicit allowlist: the production frontend origin and `localhost` dev origins only — never a wildcard, even though there are no cookies at risk on most endpoints (defense in depth, and the refresh-token endpoint specifically does rely on cookie behavior being origin-scoped).

## 9. SSRF protection — the critical one

**The threat:** every monitor type except DNS accepts a user-supplied host/URL, and the backend's job is to make outbound network requests to it. Without defenses, a user (or an attacker who compromises a user's account, or simply any user acting maliciously) could point a monitor at `http://169.254.169.254/latest/meta-data/` (cloud metadata endpoint), `http://localhost:5432` (the database, if reachable), or an internal Fly.io private-network address, and use RootPulse's own infrastructure as a proxy into networks it should never be able to reach.

**Defense, layered:**

1. **Resolve, don't trust the string.** At both monitor-creation time *and* at every single check execution (not just creation — see DNS rebinding below), the hostname is resolved to its actual IP address(es) before any connection is made.
2. **Blocklist resolved IPs, not hostnames.** A hostname string tells you nothing; the IP it resolves to does. Reject if any resolved IP falls in: loopback (`127.0.0.0/8`, `::1`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16` — this is what covers the AWS/GCP/Azure/DigitalOcean cloud metadata endpoint at `169.254.169.254`), IPv6 unique-local (`fc00::/7`) and link-local (`fe80::/10`), and any provider-specific reserved ranges worth adding to the list as they're discovered.
3. **DNS rebinding defense (the part that's easy to get wrong):** validating at creation time is not enough — a domain can resolve to a safe IP during validation and be repointed at an internal IP by the time the check actually runs (a classic TOCTOU gap). The fix: resolve once immediately before connecting, validate that specific IP, and **connect directly to the validated IP** (setting the `Host` header for virtual hosting) rather than letting the HTTP client re-resolve the hostname itself — this closes the window between validation and use to effectively zero.
4. **Validate redirect targets, not just the original URL.** If `follow_redirects` is enabled for an HTTP monitor, each redirect hop is independently resolved and validated before being followed — an attacker-controlled server could otherwise return a 302 to an internal address after passing initial validation.
5. **Scheme allowlist.** HTTP(S) monitors only ever use `http://`/`https://` — no `file://`, `gopher://`, `dict://`, etc.
6. **Network-level defense in depth, not just application logic.** The prober processes run with no network route to the backend's own database or internal services (Fly.io private networking is scoped so probers can reach the public internet and the scheduler's callback endpoint, and nothing else) — so even a bug in the application-level filter above doesn't expose internal infrastructure.
7. **Blast-radius limits regardless.** Short timeouts (§`03-monitoring-engine.md` §4) and a response-body size cap prevent a successful SSRF from being used for large data exfiltration or as a sustained tunnel even in a worst case.
8. **Applies to Ping and Port monitors too**, even though they're not URL-based — the target host is resolved and validated the same way before an ICMP echo or TCP connect is attempted.

This entire flow lives in one shared module (`monitoring/target_validation.py`), called from both the monitor-creation serializer (fail fast with a clear error) and the check-execution path (fail the check silently as a validation error, never as a false "down" incident) — one implementation, not two that can drift out of sync.

## 10. Input validation

DRF serializers validate every request server-side regardless of what the frontend's Zod schemas already checked — the frontend validation is for UX (fast feedback), the backend validation is the actual authority (PRD §20's principle, "the backend must always be the final authority," applies here even though billing/limits themselves are out of MVP scope).

## 11. SQL injection

Django's ORM parameterizes queries by default across the entire codebase. The one place raw SQL appears is the `skip_locked` claim query in the scheduler — written with the ORM's `select_for_update(skip_locked=True)`, not hand-written SQL, so it inherits the same protection.

## 12. XSS

React escapes rendered content by default. The one place user-authored content is displayed to *other* viewers (status-page branding text, incident comments shown publicly, announcements) is rendered as plain text or through a restrictive markdown renderer with HTML tags stripped — never `dangerouslySetInnerHTML` on user input.

## 13. Webhook security

- **Inbound** (deploy webhooks, `POST /deploys/webhook/{source}`): each source's own signature scheme is verified (GitHub Actions' HMAC-SHA256 signature header, Vercel's signature header, etc.) before the payload is trusted; a generic/custom source uses a per-user shared secret configured when the webhook URL is generated.
- **Outbound** (RootPulse's own webhook notification channel): every payload is signed with an HMAC-SHA256 header derived from a per-alert-contact secret, so the receiver can verify it actually came from RootPulse — documented for users the same way Stripe/GitHub document their own outbound webhook signing.

## 14. Abuse prevention

- Server-side enforcement of the minimum check interval (never trust a client-supplied interval below the floor, even though there's no paid tier to bypass in MVP — this is about protecting shared infrastructure, not revenue).
- Registration rate limiting (§6) and, if abuse is observed post-launch, a CAPTCHA on registration as a Phase 16+ addition, not built pre-emptively.

## 15. Audit logging

`AuditLog` rows written for: login (success and failure), password change/reset, API key creation/revocation, monitor deletion, status-page password changes, and GitHub account linking. This is a single-user product, so audit logging here is about the user's own visibility and forensics (e.g. "was my account accessed from somewhere I don't recognize") rather than team-accountability — still valuable, still cheap to include from the start.
