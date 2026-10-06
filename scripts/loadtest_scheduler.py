"""Load-test one scheduler pass at a realistic monitor count (Phase 17, docs/plan/01-tech-stack.md:
"a bounded pool of ~20 workers").

What it does: starts a throwaway HTTP server on 127.0.0.1 that answers like a mix of real sites
(fast, slow, erroring), creates N monitors pointed at it, all due now, and times one real
`run_scheduler_once` pass for several pool sizes. Everything else is the real thing: the
claim query, the HTTP client, the confirmation protocol, the writes of every check, status and
incident.

It needs a SCRATCH database (it creates a user and monitors and deletes nothing it didn't make):

    DATABASE_URL=postgres://.../rootpulse_scratch python scripts/loadtest_scheduler.py --monitors 60

The only change to the code under test is that loopback is allowed as a target, in this process
only, because the server it talks to is on loopback (the SSRF defence correctly forbids it
otherwise).
"""

import argparse
import os
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import User  # noqa: E402
from monitoring import engine, target_validation  # noqa: E402
from monitoring.management.commands.check_migrations_reversible import is_scratch  # noqa: E402
from monitoring.models import Check, Monitor  # noqa: E402

# A rough picture of the web: most things answer fast, some are slow, a few are broken.
PROFILE = [
    ("fast", 0.70, "/ok?ms=150"),
    ("slowish", 0.15, "/ok?ms=900"),
    ("slow", 0.05, "/ok?ms=4000"),
    ("erroring", 0.07, "/status/500"),
    ("missing", 0.03, "/status/404"),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/ok":
            ms = int(dict(p.split("=") for p in query.split("&")).get("ms", 0))
            time.sleep(ms / 1000)
            body, status = b"hello from the load test", 200
        elif path.startswith("/status/"):
            body, status = b"nope", int(path.rsplit("/", 1)[1])
        else:
            body, status = b"?", 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def db_round_trip_ms(samples=15) -> float:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        times = []
        for _ in range(samples):
            started = time.perf_counter()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            times.append((time.perf_counter() - started) * 1000)
    return statistics.median(times)


def build_monitors(user, count, port):
    plan = []
    for name, share, path in PROFILE:
        plan += [(name, path)] * max(1, round(count * share))
    plan = (plan * 2)[:count]
    now = timezone.now()
    Monitor.objects.bulk_create(
        [
            Monitor(
                user=user,
                name=f"load-{i:03d}-{kind}",
                type="http",
                target=f"http://127.0.0.1:{port}{path}",
                interval_seconds=300,
                config={"timeout_seconds": 10},
                next_check_at=now,
                status="pending",
            )
            for i, (kind, path) in enumerate(plan)
        ]
    )


def reset(user):
    Check.objects.filter(monitor__user=user).delete()
    Monitor.objects.filter(user=user).update(next_check_at=timezone.now(), status="pending")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitors", type=int, default=60)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 5, 10, 20, 40])
    args = parser.parse_args()

    if not is_scratch():
        sys.exit("Refusing to run: DATABASE_URL isn't a scratch database (see the docstring).")
    call_command("migrate", verbosity=0, interactive=False)

    original = target_validation.ip_is_forbidden
    target_validation.ip_is_forbidden = lambda ip: False if str(ip) == "127.0.0.1" else original(ip)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    user, _ = User.objects.get_or_create(
        email="loadtest@example.invalid", defaults={"username": "loadtest"}
    )
    Monitor.objects.filter(user=user).delete()
    build_monitors(user, args.monitors, port)

    rtt = db_round_trip_ms()
    print(f"{args.monitors} monitors; database round trip ~{rtt:.0f} ms (median of 15)\n")
    print(
        f"{'workers':>7} {'wall time':>10} {'checked':>8} {'up':>4} {'down':>5} {'blips':>6} "
        f"{'released':>9} {'errors':>7} {'checks written':>15}"
    )
    rows = []
    for workers in args.workers:
        reset(user)
        started = time.monotonic()
        summary = engine.run_scheduler_once(workers=workers, time_budget=600, batch_size=500)
        wall = time.monotonic() - started
        written = Check.objects.filter(monitor__user=user).count()
        rows.append((workers, wall, summary))
        print(
            f"{workers:>7} {wall:>9.1f}s {summary.checked:>8} {summary.up:>4} {summary.down:>5} "
            f"{summary.blips:>6} {summary.released:>9} {summary.errors:>7} {written:>15}"
        )

    slowest = max(
        float(p.split("=")[1]) / 1000
        for _, _, path in PROFILE
        for p in path.partition("?")[2].split("&")
        if p
    )
    print(f"\nThe slowest target takes {slowest:.1f} s, so no pass can beat that.")
    Monitor.objects.filter(user=user).delete()
    user.delete()
    server.shutdown()


if __name__ == "__main__":
    main()
