# RootPulse backend — API + scheduler image (docs/plan/01-tech-stack.md, 05-testing-deployment-devex.md)
# Same image, different entrypoint command per Fly.io process group:
#   api:       gunicorn config.wsgi
#   scheduler: python manage.py run_scheduler
#   notifier:  python manage.py run_notifications

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
