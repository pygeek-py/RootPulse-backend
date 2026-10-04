// The checks a prober can run: HTTP(S), keyword, and TCP connect (which is also "Ping").
// Results use the same shape and outcome names as monitoring/checks/result.py.

import { ResolutionFailed, TargetRejected, resolveHost, vetHost, vetHttpUrl } from "./validate.js";

const MAX_REDIRECTS = 5;
const MAX_BODY_BYTES = 1_000_000;
const BODY_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const REDIRECTS = new Set([301, 302, 303, 307, 308]);

const result = (success, status_detail, extra = {}) => ({
  success,
  status_detail,
  status_code: null,
  duration_ms: null,
  dns_ms: null,
  tcp_ms: null,
  tls_ms: null,
  ttfb_ms: null,
  detail: {},
  ...extra,
});

/** Inconclusive: refusing to connect is our decision, not evidence the target is down. */
const blocked = (reason) => result(null, "blocked_target", { detail: { reason } });

export function statusMatches(code, specs) {
  return specs.some((spec) => {
    const s = String(spec).toLowerCase();
    if (/^[1-5][0-9]{2}$/.test(s)) return Number(s) === code;
    if (/^[1-5]xx$/.test(s)) return Math.floor(code / 100) === Number(s[0]);
    return false;
  });
}

function classify(error) {
  if (error?.name === "TimeoutError" || error?.name === "AbortError") return "timeout";
  const text = String(error?.message ?? error).toLowerCase();
  if (text.includes("refused")) return "connection_refused";
  if (text.includes("tls") || text.includes("ssl") || text.includes("certificate")) return "tls_error";
  if (text.includes("timed out") || text.includes("timeout")) return "timeout";
  return "connection_error";
}

async function readBody(response, deadline, now) {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (total < MAX_BODY_BYTES) {
    if (now() > deadline) {
      await reader.cancel();
      throw Object.assign(new Error("deadline"), { name: "TimeoutError" });
    }
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  await reader.cancel();
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes.slice(0, MAX_BODY_BYTES));
}

/** HTTP(S) and keyword checks. `deps` lets tests inject fetch, the DNS resolver and the clock. */
export async function checkHttp(target, config, keyword, deps = {}) {
  const fetchFn = deps.fetch ?? fetch;
  const resolve = deps.resolve ?? resolveHost;
  const now = deps.now ?? Date.now;
  const started = now();
  const timeoutMs = Number(config.timeout_seconds ?? 10) * 1000;
  const deadline = started + timeoutMs;
  const specs = config.expected_status?.length ? config.expected_status : ["2xx", "3xx"];
  const follow = config.follow_redirects !== false;
  let method = String(config.method ?? "GET").toUpperCase();
  let body = BODY_METHODS.has(method) ? String(config.body ?? "") : "";
  let url = target;
  let redirects = 0;
  let dnsTotal = 0;

  for (;;) {
    let parsed;
    const dnsStart = now();
    try {
      parsed = await vetHttpUrl(url, resolve);
    } catch (error) {
      if (error instanceof TargetRejected) return blocked(error.message);
      if (error instanceof ResolutionFailed) {
        return result(false, "dns_error", { duration_ms: now() - started });
      }
      throw error;
    }
    dnsTotal += now() - dnsStart;

    let response;
    const requestStart = now();
    try {
      response = await fetchFn(parsed.href, {
        method,
        body: body || undefined,
        redirect: "manual", // every hop is vetted by us, never followed blindly
        headers: { "user-agent": "RootPulse-Prober/1.0", accept: "*/*", ...(config.headers ?? {}) },
        signal: AbortSignal.timeout(Math.max(1, deadline - now())),
      });
    } catch (error) {
      return result(false, classify(error), { duration_ms: now() - started, dns_ms: dnsTotal });
    }
    const ttfb = now() - requestStart;
    const code = response.status;

    if (follow && REDIRECTS.has(code) && response.headers.get("location")) {
      redirects += 1;
      if (redirects > MAX_REDIRECTS) {
        return result(false, "too_many_redirects", {
          status_code: code, duration_ms: now() - started, dns_ms: dnsTotal, ttfb_ms: ttfb,
          detail: { redirects },
        });
      }
      url = new URL(response.headers.get("location"), parsed.href).href;
      if ([301, 302, 303].includes(code) && method !== "GET" && method !== "HEAD") {
        method = "GET";
        body = "";
      }
      await response.body?.cancel();
      continue;
    }

    let page = "";
    try {
      page = await readBody(response, deadline, now);
    } catch (error) {
      return result(false, classify(error), { duration_ms: now() - started, dns_ms: dnsTotal });
    }
    const base = {
      status_code: code, duration_ms: now() - started, dns_ms: dnsTotal, ttfb_ms: ttfb,
      detail: { redirects },
    };
    if (!statusMatches(code, specs)) return result(false, "status_mismatch", base);
    if (keyword != null) {
      const present = page.includes(keyword);
      const wantsPresent = config.exists !== false;
      if (present !== wantsPresent) {
        return result(false, wantsPresent ? "keyword_missing" : "keyword_found", base);
      }
    }
    return result(true, "ok", base);
  }
}

/** TCP connect: the Port monitor, and "Ping". `deps.connect` wraps cloudflare:sockets. */
export async function checkTcp(host, port, timeoutSeconds, deps = {}) {
  const resolve = deps.resolve ?? resolveHost;
  const now = deps.now ?? Date.now;
  const started = now();
  try {
    await vetHost(host, resolve);
  } catch (error) {
    if (error instanceof TargetRejected) return blocked(error.message);
    if (error instanceof ResolutionFailed) {
      return result(false, "dns_error", { duration_ms: now() - started });
    }
    throw error;
  }
  const dnsMs = now() - started;
  const connect = deps.connect ?? (async (h, p, ms) => {
    const { connect: open } = await import("cloudflare:sockets");
    const socket = open({ hostname: h, port: p });
    const timer = new Promise((_, reject) =>
      setTimeout(() => reject(Object.assign(new Error("timed out"), { name: "TimeoutError" })), ms));
    try {
      await Promise.race([socket.opened, timer]);
    } finally {
      try { await socket.close(); } catch { /* already closed */ }
    }
  });
  const attempt = now();
  try {
    await connect(host, port, timeoutSeconds * 1000);
  } catch (error) {
    return result(false, classify(error), { duration_ms: now() - started, dns_ms: dnsMs, detail: { port } });
  }
  return result(true, "ok", {
    duration_ms: now() - started, dns_ms: dnsMs, tcp_ms: now() - attempt, detail: { port },
  });
}
