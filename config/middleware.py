"""Headers every API response should carry, whatever the view did.

The API only ever returns JSON (or a file you asked for), so it can say so to a browser in the
strongest terms: nothing may be loaded from it, it may not be framed, and nobody in between may
keep a copy of an authenticated answer. The interactive docs page loads its own scripts and is
left alone; so are the Django admin and the public status page, which set their own caching.
"""

from __future__ import annotations

API_PREFIXES = ("/api/", "/internal/")
DOCS_PATHS = ("/api/v1/docs/",)

CSP = "default-src 'none'; frame-ancestors 'none'"


class ApiSecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if not path.startswith(API_PREFIXES):
            return response
        # Never let a shared cache keep an answer meant for one person. (Views that chose their
        # own policy, like the public status page, are left as they set it.)
        if "Cache-Control" not in response:
            response["Cache-Control"] = "no-store"
        if path not in DOCS_PATHS:
            response["Content-Security-Policy"] = CSP
            response["X-Content-Type-Options"] = "nosniff"
            response["Cross-Origin-Resource-Policy"] = "cross-origin"  # the frontend reads it
        return response
