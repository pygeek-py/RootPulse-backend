"""SSRF defence for monitor targets (docs/plan/04-security.md section 9).

Every monitor type except DNS and heartbeat makes the server open a connection
to a user-supplied host, so a target must never reach loopback, private,
link-local (cloud metadata) or otherwise non-public addresses.

The rule is: resolve the name, then judge the *IP addresses*, never the string.
This module is used twice:

* at creation/edit time, to fail fast with a clear message (`validate_*`), and
* at check time (Phase 6), via `resolve_public_ips`, which returns the vetted
  addresses so the connection can be pinned to one of them. Validating at
  creation alone would leave a DNS-rebinding gap.

The Cloudflare Worker probers need the same algorithm in JavaScript; both
sides are tested against `tests/fixtures/ssrf_cases.json` so they can't drift.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_URL_LENGTH = 2048
RESOLVE_TIMEOUT_SECONDS = 3.0

# Names that only ever mean "something on a private network". Blocked by name
# in addition to by resolved address, so they are refused even when DNS is down.
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home.arpa",
)

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")


class TargetRejected(ValueError):
    """The target is syntactically invalid or points somewhere we must not connect."""


class ResolutionFailed(Exception):
    """The name could not be resolved (no such host, or DNS too slow)."""


# --- IP checks ------------------------------------------------------------


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address smuggled inside an IPv6 one, if any (mapped, NAT64, 6to4)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def ip_is_forbidden(ip: IPAddress) -> bool:
    """True for anything that is not an ordinary, publicly routable unicast address."""
    candidates: list[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None:
            candidates.append(inner)
    return any(not c.is_global or c.is_multicast or c.is_unspecified for c in candidates)


# --- host syntax ----------------------------------------------------------


def parse_ip_literal(host: str) -> IPAddress | None:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if "%" in host:  # IPv6 zone ids select a local interface
        raise TargetRejected("Addresses with a zone ID are not allowed.")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalize_hostname(raw: str) -> str:
    """Lower-cased ASCII (IDNA) hostname, or TargetRejected if it isn't a valid DNS name."""
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise TargetRejected("Enter a host name.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise TargetRejected("That isn't a valid host name.") from exc
    if len(host) > 253 or not all(_LABEL.match(label) for label in host.split(".")):
        raise TargetRejected("That isn't a valid host name.")
    return host


def _reject_internal_name(host: str) -> None:
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise TargetRejected("Private and local host names can't be monitored.")
    if "." not in host:
        # Single-label names ("db", "redis") are resolved through internal search domains.
        raise TargetRejected("Use a fully qualified public host name (like example.com).")


# --- resolution -----------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")


def _getaddrinfo(host: str) -> list[IPAddress]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen: dict[str, IPAddress] = {}
    for family, _, _, _, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
            seen[str(ip)] = ip
    return list(seen.values())


def resolve_host(host: str) -> list[IPAddress]:
    """Resolve to IP addresses with a hard time limit. Raises ResolutionFailed."""
    future = _pool.submit(_getaddrinfo, host)
    try:
        ips = future.result(timeout=RESOLVE_TIMEOUT_SECONDS)
    except (OSError, FutureTimeout, UnicodeError) as exc:
        raise ResolutionFailed(host) from exc
    if not ips:
        raise ResolutionFailed(host)
    return ips


def resolve_public_ips(host: str) -> list[IPAddress]:
    """Check-time entry point: the vetted addresses to connect to.

    Raises TargetRejected if the host is (or resolves to) anything non-public,
    and ResolutionFailed if it doesn't resolve. Callers must connect to one of
    the returned IPs rather than letting the HTTP client resolve the name again.
    """
    literal = parse_ip_literal(host)
    if literal is not None:
        ips: list[IPAddress] = [literal]
    else:
        name = normalize_hostname(host)
        _reject_internal_name(name)
        ips = resolve_host(name)
    for ip in ips:
        if ip_is_forbidden(ip):
            raise TargetRejected("That host points to a private or reserved address.")
    return ips


# --- creation-time validators --------------------------------------------


def validate_public_host(raw: str) -> str:
    """A bare host name or IP address that we will connect to (ping, port, SSL).

    Unresolvable names are accepted (the site may simply not be live yet; the
    check will report it down), but any name that resolves to a non-public
    address is refused, and every check re-validates anyway.
    """
    raw = raw.strip()
    if re.search(r"[\s/@?#]|://", raw):
        raise TargetRejected("Enter just the host name or IP address, without http:// or a path.")
    if raw.count(":") == 1 and not raw.startswith("["):
        raise TargetRejected("Put the port in the Port field, not in the host name.")

    literal = parse_ip_literal(raw)
    if literal is not None:
        if ip_is_forbidden(literal):
            raise TargetRejected("That is a private or reserved address.")
        return str(literal)

    host = normalize_hostname(raw)
    _reject_internal_name(host)
    try:
        ips = resolve_host(host)
    except ResolutionFailed:
        return host
    if any(ip_is_forbidden(ip) for ip in ips):
        raise TargetRejected("That host points to a private or reserved address.")
    return host


def validate_http_url(raw: str) -> str:
    """An http(s) URL that we will request."""
    url = raw.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise TargetRejected("Enter a URL up to 2048 characters.")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise TargetRejected("The URL can't contain spaces or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise TargetRejected("That isn't a valid URL.") from exc
    if parts.scheme not in ("http", "https"):
        raise TargetRejected("The URL must start with http:// or https://.")
    if not parts.hostname:
        raise TargetRejected("The URL needs a host name.")
    if parts.username is not None or parts.password is not None:
        raise TargetRejected("Credentials in the URL aren't allowed; use a header instead.")
    if port == 0:
        raise TargetRejected("That isn't a valid port.")
    validate_public_host(parts.hostname)
    return url


def validate_domain_name(raw: str) -> str:
    """A registrable domain we only look up (DNS records, WHOIS/RDAP): never connected to."""
    raw = raw.strip()
    if parse_ip_literal(raw) is not None or re.search(r"[\s/@:?#]", raw):
        raise TargetRejected("Enter a domain name like example.com.")
    host = normalize_hostname(raw)
    if "." not in host:
        raise TargetRejected("Enter a domain name like example.com.")
    return host
