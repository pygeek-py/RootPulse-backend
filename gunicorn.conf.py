"""gunicorn settings for the API container (Render sets $PORT).

Sized for Render's free instance (0.1 CPU, 512 MB): two workers with a few threads each is
plenty for one person's traffic and keeps memory low. The timeout is long on purpose: the
scheduler trigger (`/internal/run-due-checks/`) runs a whole pass inside one request, bounded by
its own time budget (monitoring/internal_views.py), and gunicorn must not kill it first.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "170"))
graceful_timeout = 30
keepalive = 5
# Recycle workers now and then so a slow leak can't fill a 512 MB instance.
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
# Don't put query strings (tokens, codes) in the access log.
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(M)sms'
