// HMAC request signing: the JavaScript twin of monitoring/signing.py.
// Header:  X-RootPulse-Signature: t=<unix seconds>,v1=<hex hmac-sha256>
// The MAC covers "<t>." followed by the exact body bytes. Both sides are tested
// against tests/fixtures/signing_vector.json.

export const HEADER = "X-RootPulse-Signature";
export const TOLERANCE_SECONDS = 300;

const encoder = new TextEncoder();

async function mac(secret, timestamp, body) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const data = new Uint8Array([...encoder.encode(`${timestamp}.`), ...encoder.encode(body)]);
  const signature = await crypto.subtle.sign("HMAC", key, data);
  return [...new Uint8Array(signature)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sign(secret, body = "", now = Date.now() / 1000) {
  const timestamp = Math.floor(now);
  return `t=${timestamp},v1=${await mac(secret, timestamp, body)}`;
}

/** Constant-time string comparison. */
function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** True only for a well-formed, correctly signed, fresh header. Never throws. */
export async function verify(secret, header, body = "", now = Date.now() / 1000) {
  if (!secret || !header) return false;
  try {
    const parts = Object.fromEntries(header.split(",").map((p) => p.split("=", 2)));
    const timestamp = Number(parts.t);
    if (!Number.isInteger(timestamp) || typeof parts.v1 !== "string") return false;
    if (Math.abs(now - timestamp) > TOLERANCE_SECONDS) return false;
    return safeEqual(parts.v1, await mac(secret, timestamp, body));
  } catch {
    return false;
  }
}
