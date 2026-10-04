# RootPulse prober (Cloudflare Worker)

A tiny Worker the API calls to re-check a monitor that just failed. A failure only
counts when **at least two regions agree**, which filters out the blips a single
network path produces (`docs/plan/03-monitoring-engine.md`, section 8).

```
POST /probe          body: {"type": "http|keyword|ping|port", "target": "...", "config": {...}}
```

Every request must carry `X-RootPulse-Signature` (an HMAC over a fresh timestamp and the
body, made with `PROBER_SHARED_SECRET`), and every reply is signed the same way. Nothing
unsigned gets an answer.

## What it checks

HTTP(S), keyword, and TCP connect (the Port monitor and "Ping", which is also a TCP
connect: Workers have no ICMP). SSL, domain, DNS and heartbeat monitors aren't geographic,
so they never call a prober.

## Safety

The same rules as the API's `monitoring/target_validation.py`: no loopback, private,
link-local (cloud metadata), reserved or IPv6-embedded addresses; no credentials in URLs;
no non-http(s) schemes; every redirect hop re-vetted. Both implementations are tested
against `tests/fixtures/ssrf_cases.json`, and the signing code against
`tests/fixtures/signing_vector.json`, so they cannot drift apart.

One honest limit: a Worker's `fetch()` resolves names itself, so unlike the API it cannot
pin the connection to the address it vetted. Workers run on Cloudflare's edge with no route
to RootPulse's database or any private network, so the worst a DNS-rebinding trick can do
is make the prober fetch another public address.

## Tests

```bash
cd workers/prober
npm test        # node's built-in test runner; no dependencies to install
```

## Deploying (you do this; it needs your Cloudflare account)

1. Create a free Cloudflare account and run `npm install` in this folder (installs Wrangler).
2. `npx wrangler login`
3. Deploy a copy per region you want, each under its own name:

   ```bash
   npx wrangler deploy --name rootpulse-prober-a --var PROBER_NAME:region-a
   npx wrangler deploy --name rootpulse-prober-b --var PROBER_NAME:region-b
   ```

4. Give every copy the **same** secret. Use the value of `PROBER_SHARED_SECRET` from the API
   (on Render it is generated for you):

   ```bash
   npx wrangler secret put PROBER_SHARED_SECRET --name rootpulse-prober-a
   npx wrangler secret put PROBER_SHARED_SECRET --name rootpulse-prober-b
   ```

5. On the API (Render environment), set `PROBER_URLS` to
   `region-a=https://rootpulse-prober-a.<you>.workers.dev,region-b=https://rootpulse-prober-b.<you>.workers.dev`

### Getting real geographic diversity

Cloudflare runs a Worker in the data centre **nearest the caller**, and the caller is your
API, so two probers deployed with no further hints may both run close to Render. Each reply
includes `detail.colo` (the data centre that actually ran the check), visible on a check in
the API or Django admin, so you can verify the spread. To place copies in different regions,
use Cloudflare's placement hints in `wrangler.toml` (for example `[placement]` with a
`region` for each copy); see Cloudflare's "Smart Placement" documentation for the options
available on your plan. Until probers are configured, the API re-checks a failure from its
own region after a short delay, which still filters out one-off blips.
