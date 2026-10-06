Uptime, incident & dependency monitoring: REST API v1.

This is the API the RootPulse dashboard itself uses. There is no separate, smaller public API, so anything you can do in the dashboard you can script.

## Authentication

Make a key under **Settings, API keys** and send it as a bearer token:

```
curl -H "Authorization: Bearer rp_..." https://<api-host>/api/v1/monitors/
```

A key has one of two scopes:

- **read** can make `GET`, `HEAD` and `OPTIONS` requests.
- **full** can do everything except manage API keys (that needs a signed-in dashboard session).

A revoked key stops working immediately. The key is shown once, when it is made; only a hash is kept. Anything that isn't a valid key gets `401`; a read-only key making a change gets `403`.

## Rate limits

Requests are limited per account (a key counts as its account): 60 a minute. Every response says where you stand:

- `X-RateLimit-Limit`: how many requests you get in the window
- `X-RateLimit-Remaining`: how many are left
- `X-RateLimit-Reset`: seconds until the window frees up

Over the limit you get `429` with a `Retry-After` header.

## Errors and pagination

Errors are JSON: `{"detail": "...", "field_errors": {"field": ["..."]}}`.

Lists are paginated: `{"count", "next", "previous", "results"}`, with `?page=` and, where offered, `?page_size=`.
