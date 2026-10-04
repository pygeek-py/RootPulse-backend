// Target validation for the prober: the JavaScript twin of monitoring/target_validation.py.
//
// The Worker can't pin a connection to a vetted IP (fetch() resolves the name itself), so
// this is defence in depth rather than the whole defence: Workers run on Cloudflare's edge,
// with no route to our database or any internal network. Names are still resolved (over
// DNS-over-HTTPS) and the *addresses* judged, so a name pointing at an internal-looking
// address is refused rather than probed.

import { ipIsForbidden, parseIp } from "./ip.js";

export class TargetRejected extends Error {}
export class ResolutionFailed extends Error {}

const BLOCKED_SUFFIXES = [
  ".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".corp", ".home.arpa",
];
const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/;
const MAX_URL_LENGTH = 2048;

function normalizeHostname(raw) {
  const host = raw.trim().replace(/\.$/, "").toLowerCase();
  if (!host) throw new TargetRejected("Enter a host name.");
  let ascii;
  try {
    ascii = new URL(`http://${host}/`).hostname; // IDNA (punycode) conversion
  } catch {
    throw new TargetRejected("That isn't a valid host name.");
  }
  if (ascii.length > 253 || !ascii.split(".").every((label) => LABEL.test(label))) {
    throw new TargetRejected("That isn't a valid host name.");
  }
  return ascii;
}

function rejectInternalName(host) {
  if (host === "localhost" || BLOCKED_SUFFIXES.some((s) => host.endsWith(s))) {
    throw new TargetRejected("Private and local host names can't be monitored.");
  }
  if (!host.includes(".")) {
    throw new TargetRejected("Use a fully qualified public host name (like example.com).");
  }
}

/** Resolve a host name to addresses over DNS-over-HTTPS. `fetchFn` is injectable for tests. */
export async function resolveHost(host, fetchFn = fetch) {
  const answers = [];
  for (const type of ["A", "AAAA"]) {
    try {
      const response = await fetchFn(
        `https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=${type}`,
        { headers: { accept: "application/dns-json" }, signal: AbortSignal.timeout(3000) },
      );
      if (!response.ok) continue;
      const body = await response.json();
      for (const record of body.Answer ?? []) {
        if (record.type === 1 || record.type === 28) answers.push(record.data);
      }
    } catch {
      /* a failed lookup of one family is fine; no answers at all is an error below */
    }
  }
  if (answers.length === 0) throw new ResolutionFailed(host);
  return answers;
}

/**
 * Vet a host (name or IP literal) and return its addresses. Throws TargetRejected for
 * anything non-public and ResolutionFailed if the name doesn't resolve.
 */
export async function vetHost(raw, resolve = resolveHost) {
  const text = raw.trim();
  if (/[\s/@?#]|:\/\//.test(text)) {
    throw new TargetRejected("Enter just the host name or IP address.");
  }
  if (text.includes("%")) throw new TargetRejected("Addresses with a zone ID are not allowed.");

  const literal = parseIp(text);
  if (literal) {
    if (ipIsForbidden(literal)) throw new TargetRejected("That is a private or reserved address.");
    return [text];
  }
  if ((text.match(/:/g) ?? []).length === 1 && !text.startsWith("[")) {
    throw new TargetRejected("Put the port in the Port field, not in the host name.");
  }
  const host = normalizeHostname(text);
  // The URL parser rewrites odd spellings (2130706433, 0x7f.1) into dotted form: judge that.
  const normalized = parseIp(host);
  if (normalized) {
    if (ipIsForbidden(normalized)) throw new TargetRejected("That is a private or reserved address.");
    return [host];
  }
  rejectInternalName(host);
  const addresses = await resolve(host);
  for (const address of addresses) {
    const ip = parseIp(address);
    if (!ip || ipIsForbidden(ip)) {
      throw new TargetRejected("That host points to a private or reserved address.");
    }
  }
  return addresses;
}

/** Validate an http(s) URL and its host. Returns the parsed URL. */
export async function vetHttpUrl(raw, resolve = resolveHost) {
  const text = String(raw).trim();
  if (!text || text.length > MAX_URL_LENGTH) throw new TargetRejected("Enter a URL up to 2048 characters.");
  if (/[\x00-\x20\x7f]/.test(text)) throw new TargetRejected("The URL can't contain spaces or control characters.");
  let url;
  try {
    url = new URL(text);
  } catch {
    throw new TargetRejected("That isn't a valid URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TargetRejected("The URL must start with http:// or https://.");
  }
  if (!url.hostname) throw new TargetRejected("The URL needs a host name.");
  if (url.username || url.password) throw new TargetRejected("Credentials in the URL aren't allowed.");
  if (url.port === "0") throw new TargetRejected("That isn't a valid port.");
  await vetHost(url.hostname, resolve);
  return url;
}
