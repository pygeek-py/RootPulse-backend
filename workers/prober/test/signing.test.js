import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { sign, verify } from "../src/signing.js";

const VECTOR = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/signing_vector.json", import.meta.url), "utf8"),
);

test("produces exactly what the Python signer produces (shared vector)", async () => {
  assert.equal(await sign(VECTOR.secret, VECTOR.body, VECTOR.timestamp), VECTOR.header);
});

test("verifies a header signed by Python", async () => {
  assert.equal(await verify(VECTOR.secret, VECTOR.header, VECTOR.body, VECTOR.timestamp + 5), true);
});

test("refuses tampering, wrong secrets, malformed headers and stale timestamps", async () => {
  const { secret, header, body, timestamp } = VECTOR;
  assert.equal(await verify(secret, header, body + "x", timestamp), false);
  assert.equal(await verify("other", header, body, timestamp), false);
  assert.equal(await verify(secret, header.replace("v1=", "v1=0"), body, timestamp), false);
  assert.equal(await verify(secret, "garbage", body, timestamp), false);
  assert.equal(await verify(secret, "", body, timestamp), false);
  assert.equal(await verify(secret, null, body, timestamp), false);
  assert.equal(await verify("", header, body, timestamp), false);
  assert.equal(await verify(secret, header, body, timestamp + 301), false);
  assert.equal(await verify(secret, header, body, timestamp - 301), false);
  assert.equal(await verify(secret, header, body, timestamp + 299), true);
});

test("the timestamp is covered by the MAC", async () => {
  const { secret, header, body, timestamp } = VECTOR;
  const forged = header.replace(`t=${timestamp}`, `t=${timestamp + 5}`);
  assert.equal(await verify(secret, forged, body, timestamp), false);
});

test("handles non-ASCII bodies the same way as Python (UTF-8 bytes)", async () => {
  const header = await sign("s", "café ✓", 1700000000);
  assert.equal(await verify("s", header, "café ✓", 1700000000), true);
});
