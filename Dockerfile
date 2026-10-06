# RootPulse backend — API image, deployed on Render (docs/plan/01-tech-stack.md).
# The scheduler and notification dispatcher are NOT long-running processes
# here (Render's free tier has no always-on worker) — they run inside a signed
# internal HTTP endpoint that a GitHub Actions cron calls every five minutes
# (docs/plan/09-deployment-runbook.md).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home rootpulse

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=rootpulse:rootpulse . .

# Static files (the Django admin's and the API docs') are collected at build time and served
# by WhiteNoise. A build that can't collect them should fail, not ship without styles.
# The values below exist only so settings load during the build; none is used at runtime.
RUN DJANGO_DEBUG=false DJANGO_SECRET_KEY=build-only-not-a-secret-build-only-not-a-secret \
    DATABASE_URL=sqlite:///:memory: \
    python manage.py collectstatic --noinput \
    && chown -R rootpulse:rootpulse /app/staticfiles \
    && chmod +x /app/docker-entrypoint.sh

USER rootpulse

EXPOSE 8000

# Is something listening? (A plain TCP check: the HTTP /health/ endpoint is checked by Render
# itself, and would refuse a request addressed to "127.0.0.1" under ALLOWED_HOSTS.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,socket;socket.create_connection(('127.0.0.1',int(os.environ.get('PORT','8000'))),4)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
