// RootPulse scheduler trigger (Cloudflare Worker, Cron Triggers).
//
// GitHub Actions' `schedule` trigger (the previous way this was done — see
// .github/workflows/scheduler.yml) is not reliable at five-minute intervals in practice: under
// load GitHub can delay a scheduled run by hours, not minutes. Cloudflare runs Cron Triggers on
// its own schedule rather than a shared, best-effort queue, so this Worker calls the same signed
// endpoints the GitHub Actions workflow did. The GitHub workflows are left in place as a backup
// (calling the same idempotent endpoints twice in a row is harmless — see monitoring/engine.py's
// "table is the queue" pattern) until this is confirmed working.
//
// One Worker, two schedules, told apart by `event.cron`:
//   */5 * * * *   -> POST /internal/run-due-checks/   (checks, dependency reads, alerts, mail)
//   7 * * * *     -> POST /internal/run-rollups/       (hourly analytics fold-in)
// Both calls are signed exactly like monitoring/signing.py (the JS twin is ./signing.js, tested
// against the same tests/fixtures/signing_vector.json as the Python and prober code).

import { HEADER, sign } from "./signing.js";

const CRON_PATHS = {
  "*/5 * * * *": "/internal/run-due-checks/",
  "7 * * * *": "/internal/run-rollups/",
};

async function call(env, path, deps) {
  const fetchImpl = deps.fetch ?? fetch;
  const base = (env.API_PUBLIC_URL || "").replace(/\/+$/, "");
  if (!base || !env.SCHEDULER_SHARED_SECRET) {
    console.error("RootPulse scheduler: API_PUBLIC_URL or SCHEDULER_SHARED_SECRET is not set");
    return;
  }
  const header = await sign(env.SCHEDULER_SHARED_SECRET, "");
  let response;
  try {
    response = await fetchImpl(`${base}${path}`, { method: "POST", headers: { [HEADER]: header } });
  } catch (error) {
    // A cold Render instance waking up, or a transient network error: next run tries again.
    console.error(`RootPulse scheduler: ${path} failed: ${error}`);
    return;
  }
  if (!response.ok) {
    console.error(`RootPulse scheduler: ${path} answered ${response.status}`);
  }
}

/** Exported for tests; `deps.fetch` replaces the real network call. */
export async function handleScheduled(event, env, deps = {}) {
  const path = CRON_PATHS[event.cron];
  if (!path) {
    console.error(`RootPulse scheduler: unrecognised cron "${event.cron}"`);
    return;
  }
  await call(env, path, deps);
}

export default {
  scheduled(event, env, ctx) {
    ctx.waitUntil(handleScheduled(event, env));
  },
  fetch() {
    return new Response("RootPulse scheduler worker. See README.md.\n", {
      headers: { "content-type": "text/plain" },
    });
  },
};
