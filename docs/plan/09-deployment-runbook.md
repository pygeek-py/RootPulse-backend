# Deployment runbook (Phase 18)

How to take RootPulse live, in the order that avoids surprises, and how to undo it. Everything here costs $0. The steps that need *your* accounts, credentials or DNS are yours: nothing in this repository creates an account, signs in for you, or contains a secret.

Read with `01-tech-stack.md` (why these hosts), `07-security-review.md` and `08-qa-report.md` (what was checked, and what only a deployed system can show).

## What is already done, and what only you can do

| Done in the repository (and tested) | Only you can do |
| --- | --- |
| A production image that migrates on start, runs as a normal user, serves its own static files and has a hardened `gunicorn` | Make the accounts: Neon, Render, Vercel, Cloudflare, Resend, GitHub OAuth app, and the Telegram / Discord / Slack apps |
| `render.yaml`: every setting the API reads, with the secrets left for you to fill in | Fill the secrets in (Render dashboard, Vercel, GitHub, `wrangler secret put`) |
| `scripts/preflight.py`: checks your production settings for mistakes before you deploy, printing no values | Push the repositories to GitHub (nothing has been pushed yet) |
| `scripts/smoke_test.py`: checks a live deployment, and the CI job that runs it against the real container | A custom domain, if you want one (it is the one thing here that costs money; see "Addresses") |
| The scheduler's GitHub Actions workflows, and a time budget so one trigger can't overrun its caller | Deploy the Cloudflare Workers, register the Telegram webhook |
| The website-proxies-the-API setup so the sign-in cookie works in every browser; an off-switch for new accounts | Run the smoke test against production, then create the first real monitor |

## Before anything: the four decisions

1. **Addresses (decided: no custom domain yet).** The website is `https://<project>.vercel.app` and the API is `https://<service>.onrender.com`. Both are free and have HTTPS. A custom domain is optional, **not free**, and can be added later without code changes (Vercel and Render both take a domain in their dashboards; then update `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS` and the OAuth callbacks).
2. **Cookies (decided: the website proxies the API).** The sign-in cookie is set by the API. On two unrelated hosts it would be a third-party cookie, which Safari and some privacy browsers refuse (people would be signed out on every reload). So the browser only ever talks to the website, and the website forwards `/api/v1/*` to the API: the cookie is first-party and works everywhere, at no cost.

   | Where | Setting |
   | --- | --- |
   | Vercel | `NEXT_PUBLIC_API_URL` = empty, `API_PROXY_TARGET=https://<service>.onrender.com`, `NEXT_PUBLIC_SITE_URL=https://<project>.vercel.app` |
   | Render (already the default in `render.yaml`) | `AUTH_REFRESH_COOKIE_SAMESITE=Lax`, `NUM_PROXIES=2`; the GitHub sign-in callback is on the **website's** address |
   | Preflight | run with `--proxied` |

   Exercised locally against the running stack (the API answered through the website's port, with query strings, the `Origin` header and the cookie path intact). Two things only a deployment can show: that Vercel forwards `x-forwarded-for` so `NUM_PROXIES=2` sees real visitor addresses (sign in from two networks and watch the `X-RateLimit-*` headers differ), and Vercel's own request limits. `NEXT_PUBLIC_*` and `API_PROXY_TARGET` are read at **build** time: change one, redeploy the website.
3. **Staging or straight to production.** Render's free instance has 750 free hours a month, which is exactly one service running all month. A second always-on staging service would use the same pool, so staging can't run alongside production 24/7. Either deploy staging for a few days **before** production and then delete it, or skip it and treat the first days of production as the soak (the roadmap's preference is a real staging first). Staging must use its **own** Neon project and its own secrets; never reuse production's.
4. **Who can sign up.** Sign-up is open until you close it (`SIGNUPS_OPEN`). Create your own account first, then set `SIGNUPS_OPEN=false`.

## Accounts you need (all free)

GitHub (you have it), [Neon](https://neon.tech), [Render](https://render.com), [Vercel](https://vercel.com), [Cloudflare](https://cloudflare.com), [Resend](https://resend.com) (email; needs a domain you control to send to anyone but yourself, so for a first run use your own address as the recipient). Optional: Sentry (error tracking), UptimeRobot or similar (the external "who watches the watcher" check), and the apps for Telegram, Discord and Slack.

## Order of steps

Each step says how to know it worked.

### 1. Push the code

**Decide the backend repository's visibility first** (see the GitHub Actions row under "Free-tier limits"): the 5-minute scheduler only fits GitHub's free minutes in a public repository. The code holds no secrets (the history was scanned; `.env` files are ignored), and security does not depend on the code being hidden, but making it public is your decision.

Create the two GitHub repositories (backend, frontend) and push. Nothing is pushed yet. CI runs on the first push; wait for it to go green before deploying anything: it builds the image, starts it in production mode on Postgres and smoke-tests it.

### 2. Production database (Neon)

Create a **new** Neon project named for production (do not reuse the development one; it has development data in it). Copy its pooled connection string. Check: it ends with `sslmode=require`. Keep the free plan's limits in mind: 0.5 GB of storage (the 90-day check retention and rollups are sized for this) and a connection limit; the scheduler uses up to 20 at once.

### 3. Secrets

Generate fresh values; never reuse development or staging ones.

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"     # DJANGO_SECRET_KEY, if you don't let Render generate it
python -c "import secrets; print(secrets.token_urlsafe(48))"     # SCHEDULER_SHARED_SECRET, PROBER_SHARED_SECRET, TELEGRAM_WEBHOOK_SECRET
python manage.py generate_vapid_keys                              # VAPID_*  (new keys for production)
```

Render's blueprint generates the secret key, scheduler secret, prober secret and Telegram webhook secret for you. You read them back from the Render dashboard when you need to copy them elsewhere.

### 4. The API (Render)

1. New, Blueprint, pick the backend repository. Render reads `render.yaml`.
2. Fill the `sync: false` values (the file's comments say what each is). The minimum to boot: `DATABASE_URL`, `DJANGO_ADMIN_URL`, `API_PUBLIC_URL` (= the service's own `https://<service>.onrender.com`), and `FRONTEND_URL` / `CORS_ALLOWED_ORIGINS` (= the website's address). You don't know the website's address until step 5, so put your intended `https://<project>.vercel.app` now (Vercel shows whether the name is free when you create the project) and correct it afterwards if needed. `DJANGO_ALLOWED_HOSTS` can hold just the `*.onrender.com` name.
3. Before the first deploy, check your values on your own machine. Put them in a file named like `prod.env` (git ignores every `*.env` file) or, better, outside the repository, and never commit it:

   ```bash
   python scripts/preflight.py --env-file prod.env --proxied
   ```

   It prints the *names* of what is wrong and never a value. Fix every `FAIL`; read every `WARN`.
4. Deploy. On start the container runs `python manage.py migrate --noinput`, which also loads the curated provider catalogue (a post-migrate step). A migration that fails stops the container, so Render keeps serving the previous version.

Check: Render's log ends with gunicorn listening, and `https://<api>/health/` answers `{"status": "ok"}`. A free instance sleeps after 15 minutes without traffic; the scheduler's cron (step 7) keeps it awake.

### 5. The website (Vercel)

Import the frontend repository. Environment variables (add them **before** the first deploy; they are build-time): `NEXT_PUBLIC_API_URL` empty, `API_PROXY_TARGET` (the API's address from step 4), `NEXT_PUBLIC_SITE_URL` (this site's address), and optionally `NEXT_PUBLIC_STATUS_PAGE_URL`. Deploy. Check: the landing page loads; `https://<site>/api/v1/schema/` returns the API's OpenAPI document (that is the proxy working); and the page's response carries a `Content-Security-Policy` header. If the site's real address differs from what you put in `FRONTEND_URL` / `CORS_ALLOWED_ORIGINS` on Render, correct them there and let Render redeploy.

### 6. Your account

Open the site, register with your email, and sign in. Then on Render set `SIGNUPS_OPEN=false` and redeploy. Check: a second browser can still sign in as you, and "Sign up" with another address sends nothing.

### 7. The scheduler (GitHub Actions)

In the **backend** repository: Settings, Secrets and variables, Actions, add `API_URL` (the API's address, no trailing slash) and `SCHEDULER_SHARED_SECRET` (the same value as on Render). The workflows `scheduler.yml` (every 5 minutes) and `rollups.yml` (hourly) are already in the repository: run **Scheduler** once by hand (Actions tab, Run workflow). Check: it goes green and its log prints a summary like `{"claimed": 0, ...}`. GitHub may run scheduled jobs a few minutes late, and disables them after 60 days with no repository activity (re-enable them if that happens).

One pass does everything: checks, dependency (status feed) reads, alerts, subscriber mail. It is capped at about 110 s (`TRIGGER_BUDGET_SECONDS`), inside the workflow's 150 s limit and gunicorn's 170 s.

### 8. The Cloudflare probers (optional, recommended)

Follow `workers/prober/README.md` ("Deploying"): `npx wrangler login`, deploy a copy per region, `npx wrangler secret put PROBER_SHARED_SECRET` with the API's value, then set `PROBER_URLS` on Render. Check: after a monitor fails, its incident timeline names the regions that agreed. Without probers, failures are re-checked from the API's own region.

### 9. Alert channels (do this before you rely on RootPulse for anything real)

Discord, Slack and GitHub sign-in need apps registered with the production addresses (Telegram is the exception, above):

- **Telegram.** You are reusing your existing bot (the same token as in development), which is fine for one owner, with these consequences: (1) a bot has either a webhook or polling, never both, so once you register the production webhook the bot stops delivering "Start" presses to your local machine (local polling just gets a refusal; to develop against it locally again, run `python manage.py telegram_webhook delete`); (2) chats you connected in development live in the *development* database, so reconnect them in production (Settings, Notifications, Connect Telegram); (3) the token is a credential for that bot: if it ever leaks, revoke it in @BotFather (`/revoke`) and update both places. On Render set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` (copy them from your own `.env`) and make sure `API_PUBLIC_URL` is the https address; the blueprint generates `TELEGRAM_WEBHOOK_SECRET`. Then, **once**, from your own machine with the *production* values in your environment (Render's free tier has no shell):

  ```bash
  python manage.py telegram_webhook set
  python manage.py telegram_webhook info      # confirms Telegram can reach the API
  ```

- **Discord.** In the Discord developer portal add the redirect `<API_PUBLIC_URL>/api/v1/integrations/discord/callback/` and set `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`. (`API_PUBLIC_URL` stays the API's own address; the callback is a browser round trip that the API answers directly.)
- **Slack.** At api.slack.com/apps create an app, turn on Incoming Webhooks, add the https redirect `<API_PUBLIC_URL>/api/v1/integrations/slack/callback/`, and set `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`. To let people outside your own workspace install it, turn on public distribution.
- **Web push.** The new `VAPID_*` values from step 3 (subscriptions made with the development keys won't work against new ones).
- **Email.** Resend's SMTP on port **2587** (Render's free tier blocks port 587; Gmail's 587 is blocked the same way). `DEFAULT_FROM_EMAIL` must be on a domain verified with Resend.
- **GitHub sign-in.** A GitHub OAuth app whose callback is `https://<site>/api/v1/auth/github/callback/` (the **website's** address, so the sign-in cookie is set there), and the three `GITHUB_OAUTH_*` values with `GITHUB_OAUTH_REDIRECT_URI` set to that same callback. Discord and Slack use a signed token rather than a cookie, so their callbacks stay on the API's own address (`API_PUBLIC_URL`).

Then, for each channel, add it under Settings, Notifications, and press **Send test**. A channel that has never delivered a real message is not a working channel.

### 10. Smoke test

Immediately after the deploy, and after every later one:

```bash
SCHEDULER_SHARED_SECRET=... python scripts/smoke_test.py --api https://<api> --site https://<site>
```

It checks: the API is up (it waits for a sleeping instance), plain `http` redirects to `https`, anonymous access is refused and the security headers are present, CORS admits your website and not a stranger, `/admin/` is not where it was, static files are served, an unknown status page is a 404, the scheduler trigger refuses the unsigned and forged and accepts the signed (running one real pass), and the website and sign-in page load. It creates and deletes nothing. For the full set of real-world scenarios (monitors, incidents, status page, reports against real sites) make an API key under Settings, API keys, and run:

```bash
python scripts/qa_scenarios.py --api https://<api> --key rp_...
```

### 11. RootPulse monitors itself

Create real monitors for the production website and API (`/health/`), a heartbeat for the hourly rollup job if you like, and a public status page (slug `rootpulse`) for them. Point **an external free checker** (UptimeRobot, a free Better Stack plan, anything) at `https://<api>/health/` so that if RootPulse itself dies something outside it tells you: RootPulse cannot report its own death.

Finally, the definition of done: stop the thing you monitor (or monitor a URL you can break) and confirm that an alert reaches you through a **real** channel, then that the incident resolves and the status page follows.

## Rollback plan (have it open before you cut over)

| What went wrong | Undo |
| --- | --- |
| A bad API deploy | Render, the service, Events/Deploys, **Rollback** to the previous deploy (seconds). Migrations that already ran stay applied; every migration is reversible and tested for it, but prefer rolling the *code* back and fixing forward unless a migration itself is the problem |
| A bad migration | The container stops at start and Render keeps the old version. To reverse one by hand from your machine with production env: `python manage.py migrate <app> <previous migration>`, **after taking a Neon branch/snapshot** |
| A bad website deploy | Vercel, Deployments, the last good one, **Promote to Production** |
| A bad setting | Fix it in Render's Environment tab (it redeploys), re-run preflight and the smoke test |
| Alerts are wrong or flooding | Pause the monitors, or disable the channel under Settings, Notifications; disable the Scheduler workflow (Actions tab) to stop all checking at once |
| Everything | Disable the Scheduler workflow first (stops new work), then roll back each piece. Data lives in Neon and is untouched by code rollbacks; Neon's free plan keeps a short point-in-time history |
| Leaked secret | Rotate it at the source (Render environment, `wrangler secret put`, the GitHub secret, the provider's dashboard); `DJANGO_SECRET_KEY` signs people out; rotating `SCHEDULER_SHARED_SECRET` means updating the GitHub secret in the same minute |

## Free-tier limits that matter

| Service | Limit | What it means here |
| --- | --- | --- |
| Render free web service | 750 h/month, sleeps after 15 min idle, ~512 MB RAM, no shell | One always-on service only; the 5-minute cron keeps it awake; use `manage.py` commands from your machine |
| Neon free | 0.5 GB, limited connections, compute pauses when idle | Retention (90 days) and rollups keep data small; the cron keeps compute warm |
| GitHub Actions | **private repos: 2,000 minutes/month; public repos: unlimited.** Cron minimum 5 minutes, delays under load, disabled after 60 days idle | The scheduler runs 288 times a day and every run is billed as at least a minute (about 8,600 minutes a month), so on a private repository the free minutes are gone in about a week. The repository that holds the scheduler workflow must be **public** (or hold only that workflow, in a separate small public repository). Keep the repository active |
| Vercel Hobby | personal, non-commercial use; request and bandwidth limits | Fine for a personal tool; API calls pass through it (the proxy) |
| Cloudflare Workers free | 100,000 requests/day | Probers are only called for a failing monitor; ample |
| Resend free | small daily/monthly email caps | Sign-in codes and subscriber mail only |

## After go-live

- Watch the scheduler's run history for a few days (a missing run is itself a signal) and the Render log for errors. Add `SENTRY_DSN` if you want errors collected (the SDK sends no cookies, addresses or request bodies).
- Re-run the nightly live tests (they are in CI) and `scripts/qa_scenarios.py` after each deploy.
- Phase 17's open items need this deployed system: real traffic for a few days, one real alert per channel, the probers' regional spread, email delivery with https links, and the SSRF checks against the live API.
- Run a security review over the final diff before announcing it anywhere.
