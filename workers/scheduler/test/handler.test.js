import assert from "node:assert/strict";
import { test } from "node:test";
import { handleScheduled } from "../src/index.js";
import { verify } from "../src/signing.js";

const SECRET = "test-scheduler-shared-secret";
const ENV = { API_PUBLIC_URL: "https://api.example.com", SCHEDULER_SHARED_SECRET: SECRET };

function fakeFetch(calls, status = 200) {
  return async (url, init) => {
    calls.push({ url, init });
    return new Response("{}", { status });
  };
}

test("the five-minute cron calls the checks endpoint, signed", async () => {
  const calls = [];
  await handleScheduled({ cron: "*/5 * * * *" }, ENV, { fetch: fakeFetch(calls) });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.example.com/internal/run-due-checks/");
  assert.equal(calls[0].init.method, "POST");
  const header = calls[0].init.headers["X-RootPulse-Signature"];
  assert.equal(await verify(SECRET, header, ""), true);
});

test("the hourly cron calls the rollups endpoint, signed", async () => {
  const calls = [];
  await handleScheduled({ cron: "7 * * * *" }, ENV, { fetch: fakeFetch(calls) });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.example.com/internal/run-rollups/");
  const header = calls[0].init.headers["X-RootPulse-Signature"];
  assert.equal(await verify(SECRET, header, ""), true);
});

test("an unrecognised cron calls nothing", async () => {
  const calls = [];
  await handleScheduled({ cron: "0 0 1 1 *" }, ENV, { fetch: fakeFetch(calls) });
  assert.equal(calls.length, 0);
});

test("a missing API_PUBLIC_URL calls nothing, and does not throw", async () => {
  const calls = [];
  await handleScheduled(
    { cron: "*/5 * * * *" },
    { SCHEDULER_SHARED_SECRET: SECRET },
    { fetch: fakeFetch(calls) },
  );
  assert.equal(calls.length, 0);
});

test("a missing secret calls nothing, and does not throw", async () => {
  const calls = [];
  await handleScheduled(
    { cron: "*/5 * * * *" },
    { API_PUBLIC_URL: "https://api.example.com" },
    { fetch: fakeFetch(calls) },
  );
  assert.equal(calls.length, 0);
});

test("a trailing slash on API_PUBLIC_URL does not produce a doubled slash", async () => {
  const calls = [];
  await handleScheduled(
    { cron: "7 * * * *" },
    { ...ENV, API_PUBLIC_URL: "https://api.example.com/" },
    { fetch: fakeFetch(calls) },
  );
  assert.equal(calls[0].url, "https://api.example.com/internal/run-rollups/");
});

test("a non-200 answer does not throw (the next run tries again)", async () => {
  const calls = [];
  await assert.doesNotReject(
    handleScheduled({ cron: "*/5 * * * *" }, ENV, { fetch: fakeFetch(calls, 401) }),
  );
  assert.equal(calls.length, 1);
});

test("a network failure does not throw (the next run tries again)", async () => {
  await assert.doesNotReject(
    handleScheduled({ cron: "*/5 * * * *" }, ENV, {
      fetch: async () => {
        throw new TypeError("network error");
      },
    }),
  );
});

test("fetch() answers with something other than a 404, for a human visiting the Worker's URL", async () => {
  const worker = (await import("../src/index.js")).default;
  const response = worker.fetch();
  assert.equal(response.status, 200);
  assert.match(await response.text(), /RootPulse scheduler/);
});
