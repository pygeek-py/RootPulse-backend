// The prober must refuse exactly what the Django validator refuses. Both are driven by the
// same fixture list, so a rule added on one side without the other fails CI.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { ipIsForbidden, parseIp } from "../src/ip.js";
import { TargetRejected, vetHost, vetHttpUrl } from "../src/validate.js";

const CASES = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/ssrf_cases.json", import.meta.url), "utf8"),
);

// Every name resolves to a public address unless a test says otherwise (no real network).
const publicDns = async () => ["93.184.216.34"];

test("forbidden IP addresses are refused", () => {
  for (const text of CASES.ips.forbidden) {
    const ip = parseIp(text);
    assert.ok(ip, `${text} should parse`);
    assert.equal(ipIsForbidden(ip), true, `${text} must be forbidden`);
  }
});

test("public IP addresses are allowed", () => {
  for (const text of CASES.ips.allowed) {
    const ip = parseIp(text);
    assert.ok(ip, `${text} should parse`);
    assert.equal(ipIsForbidden(ip), false, `${text} must be allowed`);
  }
});

test("forbidden hosts are refused", async () => {
  for (const host of CASES.hosts_forbidden) {
    await assert.rejects(vetHost(host, publicDns), TargetRejected, `${host} must be refused`);
  }
});

test("forbidden URLs are refused", async () => {
  for (const url of CASES.urls_forbidden) {
    await assert.rejects(vetHttpUrl(url, publicDns), TargetRejected, `${url} must be refused`);
  }
});

test("allowed URLs pass", async () => {
  for (const url of CASES.urls_allowed) {
    await assert.doesNotReject(vetHttpUrl(url, publicDns), `${url} must pass`);
  }
});

test("a public name that resolves to a private address is refused", async () => {
  const rebound = async () => ["10.1.2.3"];
  await assert.rejects(vetHttpUrl("https://looks-public.example.com/", rebound), TargetRejected);
});

test("one bad address among good ones is enough to refuse", async () => {
  const mixed = async () => ["93.184.216.34", "169.254.169.254"];
  await assert.rejects(vetHost("mixed.example.com", mixed), TargetRejected);
});

test("IPv6 answers are judged too", async () => {
  const v6 = async () => ["::1"];
  await assert.rejects(vetHost("v6.example.com", v6), TargetRejected);
});

test("odd IPv4 spellings are normalised by the URL parser, then refused", async () => {
  for (const url of ["http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/"]) {
    await assert.rejects(vetHttpUrl(url, publicDns), TargetRejected, `${url} must be refused`);
  }
});

test("IPv6 parsing handles compression and embedded IPv4", () => {
  assert.deepEqual(parseIp("::1").bytes.slice(12), [0, 0, 0, 1]);
  assert.deepEqual(parseIp("::ffff:127.0.0.1").bytes.slice(10), [0xff, 0xff, 127, 0, 0, 1]);
  assert.equal(parseIp("1::2::3"), null);
  assert.equal(parseIp("not-an-ip"), null);
  assert.equal(parseIp("256.1.1.1"), null);
  assert.equal(parseIp("01.1.1.1"), null);
});
