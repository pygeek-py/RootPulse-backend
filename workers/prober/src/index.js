// RootPulse regional prober (Cloudflare Worker).
//
// The Django API asks each prober to re-check a monitor that just failed; a failure only
// counts when at least two regions agree (docs/plan/03-monitoring-engine.md section 8).
//
//   POST /probe   body: {"type": "http|keyword|ping|port", "target": "...", "config": {...}}
//
// Requests must carry a fresh HMAC signature (X-RootPulse-Signature) made with
// PROBER_SHARED_SECRET, and the reply is signed the same way so the API can trust it.

import { checkHttp, checkTcp } from "./checks.js";
import { HEADER, sign, verify } from "./signing.js";

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });

async function signedJson(secret, data) {
  const body = JSON.stringify(data);
  return new Response(body, {
    status: 200,
    headers: { "content-type": "application/json", [HEADER]: await sign(secret, body) },
  });
}

export async function handleRequest(request, env, deps = {}) {
  const url = new URL(request.url);
  if (url.pathname !== "/probe") return json({ detail: "Not found." }, 404);
  if (request.method !== "POST") return json({ detail: "Method not allowed." }, 405);

  const body = await request.text();
  const authorised = await verify(env.PROBER_SHARED_SECRET, request.headers.get(HEADER), body);
  if (!authorised) return json({ detail: "Unauthorized." }, 401); // says nothing about why

  let job;
  try {
    job = JSON.parse(body);
  } catch {
    return json({ detail: "Invalid JSON." }, 400);
  }
  const { type, target, config = {} } = job ?? {};
  if (typeof target !== "string" || typeof config !== "object" || config === null) {
    return json({ detail: "Invalid job." }, 400);
  }

  let outcome;
  if (type === "http") outcome = await checkHttp(target, config, null, deps);
  else if (type === "keyword") outcome = await checkHttp(target, config, String(config.keyword ?? ""), deps);
  else if ((type === "ping" || type === "port") && Number.isInteger(config.port)) {
    outcome = await checkTcp(target, config.port, Number(config.timeout_seconds ?? 10), deps);
  } else {
    return json({ detail: "Unsupported monitor type." }, 400);
  }

  // Report the data centre that really ran the check, so a deployment whose probers all landed
  // in the same place is visible rather than silently giving no geographic diversity.
  outcome.detail = { ...outcome.detail, colo: request.cf?.colo ?? null };
  return signedJson(env.PROBER_SHARED_SECRET, { region: env.PROBER_NAME ?? "unknown", result: outcome });
}

export default {
  fetch: (request, env) => handleRequest(request, env),
};
