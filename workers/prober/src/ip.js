// IP address parsing and classification: the JavaScript twin of the address rules in
// monitoring/target_validation.py. Both are tested against tests/fixtures/ssrf_cases.json,
// so a fix on one side that isn't mirrored on the other fails CI.

/** Parse a dotted-quad IPv4 string into 4 bytes, or null. Strict: no short or octal forms. */
export function parseIPv4(text) {
  const parts = text.split(".");
  if (parts.length !== 4) return null;
  const bytes = [];
  for (const part of parts) {
    if (!/^(0|[1-9][0-9]{0,2})$/.test(part)) return null;
    const value = Number(part);
    if (value > 255) return null;
    bytes.push(value);
  }
  return bytes;
}

/** Parse an IPv6 string (no brackets, no zone) into 16 bytes, or null. */
export function parseIPv6(text) {
  if (!text.includes(":") || text.includes("%")) return null;
  let head = text;
  let tail4 = null;
  // A trailing dotted quad (::ffff:127.0.0.1) stands for the last two groups.
  const lastColon = text.lastIndexOf(":");
  if (text.slice(lastColon + 1).includes(".")) {
    const v4 = parseIPv4(text.slice(lastColon + 1));
    if (!v4) return null;
    tail4 = v4;
    head = text.slice(0, lastColon + 1) + "0:0";
  }
  const halves = head.split("::");
  if (halves.length > 2) return null;
  const toGroups = (s) => (s === "" ? [] : s.split(":"));
  const left = toGroups(halves[0]);
  const right = halves.length === 2 ? toGroups(halves[1]) : [];
  const total = left.length + right.length;
  if (halves.length === 1 ? total !== 8 : total > 7) return null;
  const groups = [...left, ...Array(8 - total).fill("0"), ...right];
  const bytes = [];
  for (const group of groups) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return null;
    const value = parseInt(group, 16);
    bytes.push(value >> 8, value & 0xff);
  }
  if (tail4) bytes.splice(12, 4, ...tail4);
  return bytes;
}

/** Parse an address literal (IPv6 may be bracketed). Returns {v: 4|6, bytes} or null. */
export function parseIp(text) {
  let host = text.trim();
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  const v4 = parseIPv4(host);
  if (v4) return { v: 4, bytes: v4 };
  const v6 = parseIPv6(host);
  if (v6) return { v: 6, bytes: v6 };
  return null;
}

const inV4 = (b, base, bits) => {
  const value = ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) >>> 0;
  const net = ((base[0] << 24) | (base[1] << 16) | (base[2] << 8) | base[3]) >>> 0;
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
  return (value & mask) === (net & mask);
};

const V4_FORBIDDEN = [
  [[0, 0, 0, 0], 8], //          "this network"
  [[10, 0, 0, 0], 8], //         private
  [[100, 64, 0, 0], 10], //      carrier-grade NAT
  [[127, 0, 0, 0], 8], //        loopback
  [[169, 254, 0, 0], 16], //     link-local, including cloud metadata
  [[172, 16, 0, 0], 12], //      private
  [[192, 0, 0, 0], 24], //       IETF protocol assignments
  [[192, 0, 2, 0], 24], //       documentation
  [[192, 88, 99, 0], 24], //     deprecated 6to4 relay anycast
  [[192, 168, 0, 0], 16], //     private
  [[198, 18, 0, 0], 15], //      benchmarking
  [[198, 51, 100, 0], 24], //    documentation
  [[203, 0, 113, 0], 24], //     documentation
  [[224, 0, 0, 0], 4], //        multicast
  [[240, 0, 0, 0], 4], //        reserved, including the broadcast address
];

function v4Forbidden(bytes) {
  return V4_FORBIDDEN.some(([base, bits]) => inV4(bytes, base, bits));
}

const startsWith = (bytes, prefix) => prefix.every((p, i) => bytes[i] === p);

/** True for anything that is not an ordinary, publicly routable unicast address. */
export function ipIsForbidden(ip) {
  if (ip.v === 4) return v4Forbidden(ip.bytes);
  const b = ip.bytes;
  if (b.slice(0, 12).every((x) => x === 0)) return true; //                    ::/96 unspecified, loopback, IPv4-compatible
  if (b.slice(0, 8).every((x) => x === 0) && b[8] === 0xff && b[9] === 0xff && b[10] === 0 && b[11] === 0) {
    return true; //                                                              ::ffff:0:0:0/96 IPv4-translated
  }
  if (b[0] === 0xfe && (b[1] & 0xc0) === 0xc0) return true; //                  fec0::/10 deprecated site-local
  if (b[0] === 0x5f && b[1] === 0) return true; //                              5f00::/16 segment-routing SIDs
  if (b[0] === 0xff) return true; //                                            multicast
  if ((b[0] & 0xfe) === 0xfc) return true; //                                   fc00::/7 unique local
  if (b[0] === 0xfe && (b[1] & 0xc0) === 0x80) return true; //                  fe80::/10 link-local
  if (startsWith(b, [0x20, 0x01, 0x0d, 0xb8])) return true; //                  2001:db8::/32 documentation
  if (b[0] === 0x20 && b[1] === 0x01 && b[2] < 0x02) return true; //            2001::/23 protocol assignments
  if (startsWith(b, [0x01, 0x00, 0, 0, 0, 0, 0, 0])) return true; //            100::/64 discard
  // IPv4 smuggled inside IPv6: judge the embedded address too.
  const embedded = b.slice(12, 16);
  if (b.slice(0, 10).every((x) => x === 0) && b[10] === 0xff && b[11] === 0xff) {
    return v4Forbidden(embedded); //                                            ::ffff:0:0/96 mapped
  }
  if (startsWith(b, [0x00, 0x64, 0xff, 0x9b, 0, 0, 0, 0, 0, 0, 0, 0])) {
    return v4Forbidden(embedded); //                                            64:ff9b::/96 NAT64
  }
  if (b[0] === 0x20 && b[1] === 0x02) return v4Forbidden(b.slice(2, 6)); //     2002::/16 6to4
  return false;
}
