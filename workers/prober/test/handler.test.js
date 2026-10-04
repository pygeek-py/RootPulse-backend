import assert from "node:assert/strict";
import { test } from "node:test";
import { checkHttp, checkTcp, statusMatches } from "../src/checks.js";
import { handleRequest } from "../src/index.js";
import { HEADER, sign, verify } from "../src/signing.js";

const SECRET = "prober-test-secret";
const env = { PROBER_SHARED_SECRET: SECRET, PROBER_NAME: "tokyo" };
const publicDns = async () => ["93.184.216.34"];

async function post(job, { secret = SECRET, path = "/probe", method = "POST", deps = {} } = {}) {
  const body = typeof job === "string" ? job : JSON.stringify(job);
  const request = new Request(`https://prober.example.workers.dev${path}`, {
    method,
    body: method === "GET" ? undefined : body,
    headers: { [HEADER]: await sign(secret, body) },
  });
  return handleRequest(request, env, { resolve: publicDns, ...deps });
}

const page = (body = "hello", status = 200, headers = {}) => async () =>
  new Response(body, { status, headers });

test("rejects unsigned and wrongly signed requests without saying why", async () => {
  const unsigned = await handleRequest(
    new Request("https://x.example/probe", { method: "POST", body: "{}" }),
    env,
  );
  assert.equal(unsigned.status, 401);
  assert.deepEqual(await unsigned.json(), { detail: "Unauthorized." });
  const wrong = await post({ type: "http", target: "https://example.com" }, { secret: "attacker" });
  assert.equal(wrong.status, 401);
});

test("only POST /probe exists", async () => {
  assert.equal((await post({}, { path: "/other" })).status, 404);
  assert.equal((await post({}, { method: "GET" })).status, 405);
});

test("bad jobs are 400s", async () => {
  assert.equal((await post("not json")).status, 400);
  assert.equal((await post({ type: "http" })).status, 400); // no target
  assert.equal((await post({ type: "nope", target: "x" })).status, 400);
  assert.equal((await post({ type: "ping", target: "example.com", config: {} })).status, 400); // no port
});

test("a successful probe replies with a signed, verifiable result", async () => {
  const response = await post(
    { type: "http", target: "https://example.com/", config: {} },
    { deps: { fetch: page() } },
  );
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.equal(await verify(SECRET, response.headers.get(HEADER), text), true);
  const { region, result } = JSON.parse(text);
  assert.equal(region, "tokyo");
  assert.equal(result.success, true);
  assert.equal(result.status_detail, "ok");
  assert.equal(result.status_code, 200);
  assert.ok(result.duration_ms >= 0 && result.ttfb_ms >= 0);
});

test("a blocked target is reported inconclusive and never fetched", async () => {
  let fetched = false;
  const response = await post(
    { type: "http", target: "http://169.254.169.254/latest/meta-data/", config: {} },
    { deps: { fetch: async () => { fetched = true; return new Response("secrets"); } } },
  );
  const { result } = await response.json();
  assert.equal(result.success, null);
  assert.equal(result.status_detail, "blocked_target");
  assert.equal(fetched, false);
});

test("status matching mirrors the Python rules", () => {
  assert.equal(statusMatches(200, ["2xx"]), true);
  assert.equal(statusMatches(204, ["200"]), false);
  assert.equal(statusMatches(404, ["2xx", "3xx"]), false);
  assert.equal(statusMatches(503, ["5xx"]), true);
  assert.equal(statusMatches(200, ["bogus"]), false);
});

test("http outcomes", async () => {
  const run = (fetch, config = {}, keyword = null) =>
    checkHttp("https://example.com/", config, keyword, { resolve: publicDns, fetch });

  assert.equal((await run(page("", 503))).status_detail, "status_mismatch");
  assert.equal((await run(page("", 404), { expected_status: ["404"] })).success, true);
  assert.equal(
    (await run(async () => { throw Object.assign(new Error("x"), { name: "TimeoutError" }); })).status_detail,
    "timeout",
  );
  assert.equal((await run(async () => { throw new Error("Connection refused"); })).status_detail, "connection_refused");
  assert.equal((await run(async () => { throw new Error("TLS handshake failed"); })).status_detail, "tls_error");
});

test("keyword checks", async () => {
  const run = (body, config = {}, keyword = "Welcome") =>
    checkHttp("https://example.com/", config, keyword, { resolve: publicDns, fetch: page(body) });
  assert.equal((await run("Welcome back")).success, true);
  const missing = await run("Server error");
  assert.deepEqual([missing.success, missing.status_detail], [false, "keyword_missing"]);
  const found = await run("Fatal error", { exists: false }, "Fatal");
  assert.deepEqual([found.success, found.status_detail], [false, "keyword_found"]);
  assert.equal((await run("welcome")).success, false); // case-sensitive
});

test("every redirect hop is vetted before it is followed", async () => {
  const fetched = [];
  const fetch = async (url) => {
    fetched.push(url);
    return new Response("", { status: 302, headers: { location: "http://internal.example.net/admin" } });
  };
  const dns = async (host) => (host === "internal.example.net" ? ["10.0.0.9"] : ["93.184.216.34"]);
  const result = await checkHttp("https://example.com/", {}, null, { resolve: dns, fetch });
  assert.equal(result.status_detail, "blocked_target");
  assert.equal(result.success, null);
  assert.deepEqual(fetched, ["https://example.com/"]); // the internal host was never contacted
});

test("redirects are followed, counted, and loops stop", async () => {
  const hop = async (url) =>
    new URL(url).pathname === "/old"
      ? new Response("", { status: 301, headers: { location: "/new" } })
      : new Response("landed");
  const result = await checkHttp("https://example.com/old", {}, null, { resolve: publicDns, fetch: hop });
  assert.equal(result.success, true);
  assert.equal(result.detail.redirects, 1);

  const loop = async () => new Response("", { status: 302, headers: { location: "/loop" } });
  const looped = await checkHttp("https://example.com/loop", {}, null, { resolve: publicDns, fetch: loop });
  assert.deepEqual([looped.success, looped.status_detail], [false, "too_many_redirects"]);
});

test("TCP connect outcomes", async () => {
  const ok = await checkTcp("example.com", 443, 5, { resolve: publicDns, connect: async () => {} });
  assert.deepEqual([ok.success, ok.status_detail], [true, "ok"]);
  assert.ok(ok.tcp_ms >= 0);

  const refused = await checkTcp("example.com", 443, 5, {
    resolve: publicDns,
    connect: async () => { throw new Error("connection refused"); },
  });
  assert.equal(refused.status_detail, "connection_refused");

  const slow = await checkTcp("example.com", 443, 5, {
    resolve: publicDns,
    connect: async () => { throw Object.assign(new Error("timed out"), { name: "TimeoutError" }); },
  });
  assert.equal(slow.status_detail, "timeout");

  const internal = await checkTcp("10.0.0.1", 5432, 5, { resolve: publicDns, connect: async () => {} });
  assert.deepEqual([internal.success, internal.status_detail], [null, "blocked_target"]);
});
