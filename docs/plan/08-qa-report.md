# QA report (Phase 17)

What was tested, what it found, what changed, and what only a few days of real staging traffic can show. Read with `07-security-review.md` (the security half of the same work).

## Summary

| Area | Result |
| --- | --- |
| Backend tests | 1,502 passed, 24 live tests held back for their own run; **96% line and branch coverage**, read for gaps (below) |
| Frontend unit tests | 392 passed |
| Frontend e2e | Every route in the architecture route map loaded, with no script error, no content-policy block, one heading, no sideways overflow on a phone or a laptop, and **no serious or critical accessibility violation in dark or light** (axe) |
| Against real services | 24 live tests (real DNS, TLS, HTTP, RDAP, every provider's real status feed) pass; **11 of 11 end-to-end scenarios pass** on the running stack against real targets, using nothing but the public API |
| Migrations | All 19 reversible and re-appliable, proven on SQLite **and on real Postgres**; one defect found and fixed |
| Scheduler | 60 monitors in 38 s with the planned 20-worker pool even over a 207 ms database link; sizing holds |
| Dependencies | `pip-audit`: none; `npm audit --omit=dev`: none |

## What it found, and what changed

| # | Finding | How it was found | Change |
| --- | --- | --- | --- |
| 1 | `migrate providers zero` crashed: the catalogue sync ran after every migrate, with its table already dropped. A bad `providers` migration could not have been rolled back | the new migration rollback check | The sync does nothing when the table doesn't exist. Pinned by `tests/test_migrations.py` |
| 2 | Accent and status colours failed WCAG contrast as text: dark mode 2.9 to 4.4:1 (accent, red, grey), light mode 2.7 to 3.8:1 (green, amber, red on their tinted badges); white on the red fill was 3.76:1 | axe, on every route, both themes | A separate set of "ink" colours for text (`text-ink-*`), computed to clear 4.5:1 on every surface and tint; fills keep the originals; the dark red fill is darker so white text reads. A status page's owner-chosen colour is pulled toward the page's text colour when used as text |
| 3 | Links inside sentences were told apart by colour alone (WCAG 1.4.1) | axe | Links in paragraphs are underlined |
| 4 | **Settings → Security** promised "a history of sign-ins will appear in a later phase", while the plan lists an audit log | reading the route map against the screens | `GET /api/v1/auth/audit-log/` (your own events, newest first, details withheld) and a **Recent activity** list in plain words, sensitive events flagged |
| 5 | The frontend CI job would have failed on its first run: it ran a WebKit (phone) project with only Chromium installed, and `tsc` needs route types nothing generated | reading the CI file against the config (nothing had ever been pushed, so it had never run) | Installs both browsers; `typecheck` generates the types first; also builds, audits, and uploads the Playwright report on failure |
| 6 | The backend CI ran one Python version and no coverage, audit or rollback checks | review | A 3.12 and 3.14 matrix, a coverage floor (95%), `pip-audit`, `check --deploy`, the migration rollback on a Postgres service, the Worker's tests, and a nightly run of the live tests |
| 7 | First-compile time made the first test to touch a route fail now and then | e2e history | A 60 s test timeout |

## Backend coverage, read for gaps

The overall figure is not the point; each uncovered region was read. They fall into four kinds:

- **Code that only exists to talk to the real network** (DNS lookup, TLS handshake, response body reading, RDAP): not covered by stubbed tests by design. Covered instead by `tests/live/` against real services, which is how the Phase 17 definition of done asks for it.
- **Behaviour that deserved a test, and now has one** (`tests/test_qa_gaps.py`): the engine loop recovering from a dropped database connection, the status page mail command, every kind of postmortem timeline event and the wording of durations.
- **Defensive branches** (a `LookupError` for an unknown charset, a race between two statements): left uncovered; testing them would mean testing the test.
- **Boilerplate** (`__str__`, admin classes, a management command that writes keys to a file): left.

## The live tests (`pytest -m live tests/live`, nightly in CI)

Real targets, chosen to be boring: `example.com` (reserved by IANA for this), `github.com`, `wikipedia.org`, and the public names `localtest.me` and `*.nip.io` that resolve to private addresses, which is the real-world form of the SSRF trick. They cover: an HTTP page, a keyword, a missing page, a real redirect (followed, and not followed), a nonexistent name; a TCP port; a real TLS certificate and its warning threshold; real DNS records, then a changed set; a real RDAP registration; the validator refusing public names that point inside, at creation and at check time, and loopback in every spelling with the real resolver; and **all 20 curated provider status feeds through the real adapters** (the test that would have caught Anthropic's status page moving). A test skips, rather than fails, when there is no internet.

## The end-to-end scenarios (`scripts/qa_scenarios.py`)

Run against the running stack, using only the public API and an API key, so it exercises exactly what a customer could do. No alert contact is attached to anything it makes, and it removes everything it created. Result on the local stack, real targets: **11 of 11**.

1. A full key signs in; a made-up key is refused.
2. Five private or internal targets are refused at creation, including `localtest.me` and `0x7f.0.0.1`.
3. Eight monitor types are created (HTTP, keyword, port, SSL, DNS, domain, heartbeat).
4. The real engine checks example.com (up, in about a second) and a missing page (down, `status_mismatch`).
5. A confirmed failure opens exactly one incident with its cause (404).
6. Keyword, port and SSL monitors report real results.
7. A signed deploy webhook is accepted, a forged one refused (401), and a monitor that fails right after the deploy is linked to it.
8. A status page is published, shows the real outage and the real "operational", never leaks a monitor's target or id, shows a note when made visible and drops it when switched off, and an unpublished page is a 404.
9. The PDF and CSV reports download; analytics answers.
10. All 20 provider feeds are read live.
11. A read-only key can look and not change, and a full key cannot make keys.

## Migration rollback

`python manage.py check_migrations_reversible` migrates every app to zero in reverse dependency order, then forward again, then checks for drift between migrations and models. It **drops every table**, so it refuses unless the database is SQLite or named like a scratch database (`test_...` or containing `scratch`). Run on SQLite in the test suite, and on a scratch database on real Postgres: all 19 migrations reversible. No migration uses `RunPython` or `RunSQL`, so Django reverses them itself; a test fails if one is ever added without a reverse.

## Scheduler load test (`scripts/loadtest_scheduler.py`)

60 monitors, due at once, pointed at a local server answering like a mix of real sites (70% fast, 15% slowish, 5% four seconds, 10% erroring), on real Postgres, with a **207 ms database round trip** (a laptop reaching Neon: roughly 40 times slower than the API will be next to its database, so a worst case). One real `run_scheduler_once` per pool size:

| Workers | 1 | 5 | 10 | 20 | 40 |
| --- | --- | --- | --- | --- | --- |
| Wall time | 172.6 s | 65.9 s | 43.4 s | **37.8 s** | 31.9 s |

Every run checked all 60 (54 up, 6 down), wrote 66 checks (60 plus six confirmation re-checks), released none and had no errors. The pool of 20 from `01-tech-stack.md` holds: the pass is well inside its 80 s budget and the five-minute interval even at this latency, going from 20 to 40 workers buys 6 s, and 20 connections is modest for Postgres (check it against your Neon plan's connection limit when deploying). At ordinary latency (single-digit milliseconds) the pass is dominated by the slowest target plus the five-second re-check, about ten seconds. Because a pass is bounded by `CHECK_PASS_BUDGET_SECONDS`, monitors it can't reach are handed back unchecked, not lost.

## What is not done, and why

The roadmap's definition of done also asks for staging "exercised with real traffic for at least a few days" and a clean full CI run there. Those need the deployed system and time, and can't be done from here:

1. **A few days on staging.** Deploy to the staging environment, run `scripts/qa_scenarios.py` against it with a key from the staging app, and leave the nightly CI (which includes the live tests) running while real monitors accumulate checks. Watch the scheduler's run history and the rollups.
2. **Real alert channels.** No test sends to a real Telegram, Slack, Discord, email or push destination (they need your accounts). Each is tested against stubbed HTTP, and the delivery code is shared. Once the production setup is done, send one real test alert per channel from Settings → Notifications.
3. **The Cloudflare probers.** Their code is tested against the same fixtures as the API, but not deployed. After deploying them, confirm a monitor's failure is confirmed from more than one region (the incident timeline says which regions agreed).
4. **Email delivery.** Subscriber and sign-in email go through whatever SMTP is configured; confirm delivery and that links are `https` in staging.
5. **WebKit.** The phone project runs in CI; it can't be installed on this machine, so locally the phone layouts are checked at 375 px in Chromium.
