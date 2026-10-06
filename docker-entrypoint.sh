#!/bin/sh
# Container start: bring the database up to date, then hand over to gunicorn.
#
# Migrating here (rather than in a separate release step) is what a free Render service allows.
# It is safe to run on every start: with nothing to apply it does nothing, and Django takes a
# lock so two instances starting together don't both migrate. A migration that fails stops the
# container, so Render keeps the previous version serving instead of promoting a broken one.
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
