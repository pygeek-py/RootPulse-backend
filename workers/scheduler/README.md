# RootPulse scheduler trigger (Cloudflare Worker)

Calls RootPulse's own signed endpoints on a schedule, the same two calls
`.github/workflows/scheduler.yml` and `rollups.yml` make:

```
POST /internal/run-due-checks/   every 5 minutes  (checks, dependency reads, alerts, mail)
POST /internal/run-rollups/      hourly, at :07    (folds checks into the analytics rollups)
```

## Why this exists next to the GitHub Actions workflows

GitHub Actions' `schedule` trigger does not run reliably every five minutes in practice: under
load GitHub can delay a scheduled run by hours rather than minutes, which showed up as monitors
going most of a day with only a handful of checks. Cloudflare Cron Triggers run on Cloudflare's
own schedule, not a shared best-effort queue, so this Worker is the reliable path; the GitHub
workflows are left in place as a harmless backup (calling the same endpoints twice back to back
does nothing extra — see `monitoring/engine.py`'s "table is the queue" pattern). Once this Worker
has been running for a few days and its invocation log shows it firing every five minutes, you can
remove the `schedule:` block from `scheduler.yml` and `rollups.yml` (keep `workflow_dispatch:` for
a manual run) to stop the duplicate calls and log noise.

## Deploying (you do this; it needs your Cloudflare account)

1. `npm install` in this folder (installs Wrangler).
2. `npx wrangler login`.
3. Edit `wrangler.toml`'s `API_PUBLIC_URL` to your API's real address (e.g.
   `https://rootpulse-o5c8.onrender.com`, no trailing slash).
4. Set the secret — the **same value** as `SCHEDULER_SHARED_SECRET` on Render (copy it from the
   Render dashboard's Environment tab; never written to a file here):
   ```bash
   npx wrangler secret put SCHEDULER_SHARED_SECRET
   ```
5. `npx wrangler deploy`.
6. Check it worked: Cloudflare dashboard, Workers & Pages, `rootpulse-scheduler`, **Triggers**
   tab shows both cron schedules, and **Logs** (or `npx wrangler tail`) shows an invocation within
   five minutes. A monitor's "Next check" in the app should also start moving again.

## Tests

```bash
npm test     # node's built-in test runner; no dependencies to install
```

The signing code (`src/signing.js`) is the same tested implementation as the prober's, checked
against the same `tests/fixtures/signing_vector.json` the Python signer uses, so it cannot drift
out of step with `monitoring/signing.py`.

## Free plan

Two Cron Triggers on one Worker, invoked a few hundred times a day, is well within Cloudflare
Workers' free plan. Cloudflare's own pricing page has the current, exact limits.
