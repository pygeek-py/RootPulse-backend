# RootPulse backend — API image, deployed on Render (docs/plan/01-tech-stack.md).
# The scheduler and notification dispatcher are NOT long-running processes
# here (Render's free tier has no always-on worker) — they're Django
# management commands invoked via a signed internal HTTP endpoint on a
# GitHub Actions cron schedule instead (Phase 6/8, docs/plan/06-roadmap.md).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput --skip-checks || true

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
