"""The critical scenarios, end to end, against a running RootPulse and REAL external services
(Phase 17: "every critical scenario passes on staging, against real, not mocked, services at
least once").

It uses nothing but the public API and a **full-access API key** (made under Settings, API keys),
so it exercises exactly what a customer could do, and it needs no database access. The monitored
targets are boring, stable public ones (example.com, which IANA reserves for this).

    python scripts/qa_scenarios.py --api https://your-api --key rp_...

A real engine has to be running (the scheduler's cron in staging; `run_engine --loop 60` locally),
or pass --scheduler-secret to trigger the passes directly. No alert contact is attached to anything
it makes, so nobody is paged. Everything it creates is named "qa-..." and removed at the end, even
if a step fails.
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta

import httpx

UP_URL = "https://example.com"
DOWN_URL = "https://example.com/rootpulse-qa-does-not-exist"
STAMP = datetime.now(UTC).strftime("%m%d%H%M%S")


class Failed(Exception):
    pass


class Qa:
    def __init__(self, api: str, key: str, secret: str | None, wait: int):
        self.api = api.rstrip("/")
        self.secret = secret
        self.wait = wait
        self.http = httpx.Client(
            base_url=self.api,
            headers={"Authorization": f"Bearer {key}", "Origin": "http://localhost:3000"},
            timeout=60,
        )
        self.anon = httpx.Client(base_url=self.api, timeout=60)
        self.made: list[tuple[str, str]] = []  # (collection path, id), removed at the end
        self.results: list[tuple[str, bool, str]] = []

    # --- plumbing -------------------------------------------------------------------------

    def req(self, method, path, expect=(200, 201, 204), **kw):
        response = self.http.request(method, path, **kw)
        if response.status_code not in expect:
            raise Failed(f"{method} {path} -> {response.status_code} {response.text[:200]}")
        return response

    def create(self, path, body, remember=True):
        data = self.req("POST", path, json=body, expect=(201,)).json()
        if remember:
            self.made.append((path, data["id"]))
        return data

    def trigger(self):
        """Run a pass now if we hold the scheduler's secret; otherwise the engine's cron will."""
        if not self.secret:
            return
        stamp = str(int(time.time()))
        mac = hmac.new(self.secret.encode(), f"{stamp}.".encode(), hashlib.sha256).hexdigest()
        self.anon.post(
            "/internal/run-due-checks/", headers={"X-RootPulse-Signature": f"t={stamp},v1={mac}"}
        )

    def until(self, what, probe, timeout=None):
        deadline = time.time() + (timeout or self.wait)
        last = None
        while time.time() < deadline:
            self.trigger()
            last = probe()
            if last:
                return last
            time.sleep(5)
        raise Failed(f"timed out waiting for {what}")

    def scenario(self, name):
        def decorate(fn):
            def run():
                started = time.time()
                try:
                    detail = fn() or ""
                    self.results.append((name, True, f"{detail} ({time.time() - started:.0f}s)"))
                except Failed as exc:
                    self.results.append((name, False, str(exc)))
                except Exception as exc:  # noqa: BLE001
                    self.results.append((name, False, f"{type(exc).__name__}: {exc}"))
                    traceback.print_exc()

            return run

        return decorate

    def cleanup(self):
        for path, object_id in reversed(self.made):
            try:
                self.http.delete(f"{path}{object_id}/")
            except httpx.HTTPError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--key", required=True, help="a FULL access API key")
    parser.add_argument("--read-key", help="a READ-ONLY key (made in the app) for the scope checks")
    parser.add_argument("--scheduler-secret")
    parser.add_argument("--wait", type=int, default=240, help="seconds to wait for a check")
    args = parser.parse_args()
    qa = Qa(args.api, args.key, args.scheduler_secret, args.wait)
    state: dict = {}

    @qa.scenario("API: a full key signs in; an unknown key is refused")
    def auth():
        me = qa.req("GET", "/api/v1/auth/me/").json()
        bad = httpx.get(f"{qa.api}/api/v1/monitors/", headers={"Authorization": "Bearer rp_nope"})
        if bad.status_code != 401:
            raise Failed(f"a made-up key answered {bad.status_code}")
        return f"signed in as {me['email']}"

    @qa.scenario("SSRF: private and internal targets are refused at creation")
    def ssrf():
        refused = []
        for target in (
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://localtest.me/",
            "http://127.0.0.1.nip.io/",
            "http://0x7f.0.0.1/",
        ):
            response = qa.http.post(
                "/api/v1/monitors/", json={"type": "http", "name": "qa-ssrf", "target": target}
            )
            if response.status_code != 400:
                if response.status_code == 201:
                    qa.http.delete(f"/api/v1/monitors/{response.json()['id']}/")
                raise Failed(f"{target} was accepted ({response.status_code})")
            refused.append(target)
        return f"{len(refused)} refused"

    @qa.scenario("Monitors: eight types can be created; real sites are checked")
    def create_monitors():
        specs = {
            "up": {"type": "http", "name": f"qa-up-{STAMP}", "target": UP_URL},
            "down": {"type": "http", "name": f"qa-down-{STAMP}", "target": DOWN_URL},
            "keyword": {
                "type": "keyword",
                "name": f"qa-keyword-{STAMP}",
                "target": UP_URL,
                "config": {"keyword": "Example Domain"},
            },
            "port": {
                "type": "port",
                "name": f"qa-port-{STAMP}",
                "target": "example.com",
                "config": {"port": 443},
            },
            "ssl": {"type": "ssl", "name": f"qa-ssl-{STAMP}", "target": "example.com"},
            "dns": {
                "type": "dns",
                "name": f"qa-dns-{STAMP}",
                "target": "example.com",
                "config": {"record_types": ["A"]},
            },
            "domain": {"type": "domain", "name": f"qa-domain-{STAMP}", "target": "wikipedia.org"},
            "heartbeat": {"type": "cron", "name": f"qa-heartbeat-{STAMP}", "config": {}},
        }
        for key, body in specs.items():
            state[key] = qa.create("/api/v1/monitors/", body)
        return f"{len(specs)} created"

    @qa.scenario("Engine: real checks run; up stays up, a missing page goes down")
    def real_checks():
        def settled():
            up = qa.req("GET", f"/api/v1/monitors/{state['up']['id']}/").json()
            down = qa.req("GET", f"/api/v1/monitors/{state['down']['id']}/").json()
            return (up, down) if up["status"] != "pending" and down["status"] != "pending" else None

        up, down = qa.until("the first checks", settled)
        if up["status"] != "up":
            raise Failed(f"example.com is {up['status']} ({up.get('last_status_detail')})")
        if down["status"] != "down":
            raise Failed(f"a 404 page is {down['status']}, expected down")
        detail = down["last_status_detail"]
        return f"up in {up.get('last_response_ms')} ms; the 404 page is down ({detail})"

    @qa.scenario("Engine: a confirmed failure opens exactly one incident, with its reason")
    def incident():
        def found():
            rows = qa.req("GET", f"/api/v1/incidents/?monitor_id={state['down']['id']}").json()
            return rows["results"] if rows["count"] else None

        rows = qa.until("an incident", found)
        if len(rows) != 1:
            raise Failed(f"{len(rows)} incidents for one failing monitor")
        state["incident"] = rows[0]
        if rows[0]["reason"] != "status_mismatch" or rows[0]["status_code"] != 404:
            raise Failed(f"unexpected cause: {rows[0]['reason']} {rows[0]['status_code']}")
        return f"opened for {rows[0]['reason']} ({rows[0]['status_code']})"

    @qa.scenario("Engine: other monitor types report real results")
    def other_types():
        want = {"keyword": "up", "port": "up", "ssl": "up"}

        def settled():
            got = {
                k: qa.req("GET", f"/api/v1/monitors/{state[k]['id']}/").json()["status"]
                for k in want
            }
            return got if all(v != "pending" for v in got.values()) else None

        got = qa.until("keyword/port/ssl checks", settled)
        wrong = {k: v for k, v in got.items() if v != want[k]}
        if wrong:
            raise Failed(f"expected up, got {wrong}")
        return ", ".join(f"{k} {v}" for k, v in got.items())

    @qa.scenario("Deploys: a signed deploy before a failure is linked to the incident")
    def deploys():
        source = qa.create("/api/v1/deploy-sources/", {"name": f"qa-ci-{STAMP}", "type": "generic"})
        body = json.dumps({"service": f"qa-svc-{STAMP}", "version": "qa-1"}).encode()
        stamp = str(int(time.time()))
        mac = hmac.new(source["secret"].encode(), f"{stamp}.".encode() + body, hashlib.sha256)
        sent = qa.anon.post(
            source["webhook_url"].replace(qa.api, ""),
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-RootPulse-Signature": f"t={stamp},v1={mac.hexdigest()}",
            },
        )
        if sent.status_code not in (200, 201, 202):
            raise Failed(f"the signed webhook answered {sent.status_code}")
        forged = qa.anon.post(
            source["webhook_url"].replace(qa.api, ""),
            content=body,
            headers={"Content-Type": "application/json", "X-RootPulse-Signature": "t=1,v1=bad"},
        )
        if forged.status_code != 401:
            raise Failed(f"a forged signature answered {forged.status_code}")
        # A monitor tagged with that service, that fails right after the deploy.
        state["tagged"] = qa.create(
            "/api/v1/monitors/",
            {
                "type": "http",
                "name": f"qa-tagged-{STAMP}",
                "target": DOWN_URL,
                "deploy_service": f"qa-svc-{STAMP}",
            },
        )

        def linked():
            rows = qa.req("GET", f"/api/v1/incidents/?monitor_id={state['tagged']['id']}").json()
            return rows["results"][0] if rows["count"] else None

        row = qa.until("the tagged incident", linked)
        detail = qa.req("GET", f"/api/v1/incidents/{row['id']}/").json()
        if not detail.get("deploy"):
            raise Failed("the incident wasn't linked to the deploy")
        return f"linked to {detail['deploy']['version']}; a forged signature was refused"

    @qa.scenario("Status page: published, reflects real status, and notes show and hide live")
    def status_page():
        slug = f"qa-{STAMP}"
        page = qa.create("/api/v1/status-pages/", {"name": f"QA {STAMP}", "slug": slug})
        qa.req("PATCH", f"/api/v1/status-pages/{page['id']}/", json={"is_public": True})
        qa.req(
            "POST",
            f"/api/v1/status-pages/{page['id']}/components/",
            json={"monitor_id": state["down"]["id"], "display_name": "Missing page"},
        )
        qa.req(
            "POST",
            f"/api/v1/status-pages/{page['id']}/components/",
            json={"monitor_id": state["up"]["id"], "display_name": "Home page"},
        )
        public = qa.anon.get(f"/api/v1/public/status-pages/{slug}/")
        if public.status_code != 200:
            raise Failed(f"the public page answered {public.status_code}")
        body = public.json()
        statuses = {c["name"]: c["status"] for c in body["components"]}
        if statuses != {"Missing page": "major_outage", "Home page": "operational"}:
            raise Failed(f"component statuses were {statuses}")
        if body["overall"]["status"] != "partial_outage":
            raise Failed(f"overall was {body['overall']['status']}")
        raw = public.text
        if UP_URL in raw or "example.com" in raw or state["down"]["id"] in raw:
            raise Failed("the public page leaked a monitor's target or id")

        incident_id = state["incident"]["id"]
        shown = qa.req(
            "POST",
            f"/api/v1/incidents/{incident_id}/comments/",
            json={"body": "We are looking into it.", "visible_on_status_page": True},
        ).json()
        hidden = qa.req(
            "POST", f"/api/v1/incidents/{incident_id}/comments/", json={"body": "internal only"}
        ).json()
        text = qa.anon.get(f"/api/v1/public/status-pages/{slug}/").text
        if "We are looking into it." not in text or "internal only" in text:
            raise Failed("note visibility wasn't respected")
        qa.req(
            "PATCH",
            f"/api/v1/incidents/{incident_id}/comments/{shown['id']}/",
            json={"visible_on_status_page": False},
        )
        if "We are looking into it." in qa.anon.get(f"/api/v1/public/status-pages/{slug}/").text:
            raise Failed("a note switched off was still on the page")
        qa.req("DELETE", f"/api/v1/incidents/{incident_id}/comments/{hidden['id']}/")
        qa.req("PATCH", f"/api/v1/status-pages/{page['id']}/", json={"is_public": False})
        if qa.anon.get(f"/api/v1/public/status-pages/{slug}/").status_code != 404:
            raise Failed("an unpublished page was still reachable")
        return "outage shown, notes toggled live, unpublish took effect at once"

    @qa.scenario("Reports and analytics agree and download")
    def reports():
        today = datetime.now(UTC).date()
        params = {
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": today.isoformat(),
        }
        pdf = qa.req("GET", "/api/v1/reports/uptime.pdf", params=params)
        if not pdf.content.startswith(b"%PDF-"):
            raise Failed("the report isn't a PDF")
        csv_rows = (
            qa.req("GET", "/api/v1/reports/uptime.csv", params=params).text.strip().splitlines()
        )
        if len(csv_rows) < 2:
            raise Failed("the CSV has no rows")
        qa.req("GET", "/api/v1/analytics/fleet/?range=7d")
        return f"PDF {len(pdf.content)} bytes, CSV {len(csv_rows)} rows"

    @qa.scenario("Dependencies: real provider status feeds are readable")
    def providers():
        rows = qa.req("GET", "/api/v1/providers/").json()
        known = [p for p in rows if p["status"] != "unknown"]
        if len(known) < max(1, len(rows) // 2):
            raise Failed(f"only {len(known)} of {len(rows)} providers have a known status")
        return f"{len(known)} of {len(rows)} providers read live"

    @qa.scenario("Scopes: a read-only key can look but not change; keys can't make keys")
    def scopes():
        # A key can't make keys (by design), so the read-only key is made in the app beforehand.
        made = qa.http.post("/api/v1/api-keys/", json={"name": "x"})
        if made.status_code != 403:
            raise Failed(f"a full key could make an API key ({made.status_code})")
        if not args.read_key:
            return (
                "keys can't make keys (no --read-key given, so the read-only checks were skipped)"
            )
        read = httpx.Client(
            base_url=qa.api, headers={"Authorization": f"Bearer {args.read_key}"}, timeout=30
        )
        if read.get("/api/v1/monitors/").status_code != 200:
            raise Failed("a read key couldn't list monitors")
        body = {"type": "http", "name": "x", "target": UP_URL}
        blocked = read.post("/api/v1/monitors/", json=body)
        if blocked.status_code != 403:
            raise Failed(f"a read key's write answered {blocked.status_code}")
        return "read ok, write refused, and a full key can't make keys"

    steps = [auth, ssrf, create_monitors, real_checks, incident, other_types, deploys,
             status_page, reports, providers, scopes]  # fmt: skip
    try:
        for step in steps:
            step()
            name, ok, detail = qa.results[-1]
            print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)
            if not ok and step in (create_monitors, real_checks):
                break  # the rest depend on these
    finally:
        qa.cleanup()
        print("\nCleaned up everything it created.")

    failed = [r for r in qa.results if not r[1]]
    print(f"\n{len(qa.results) - len(failed)} of {len(qa.results)} scenarios passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
