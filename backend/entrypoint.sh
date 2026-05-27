#!/bin/sh
# Production entrypoint: gunicorn + collectstatic only.
# Local development uses entrypoint.local.sh (runserver).
# Migrations are expected to run as a one-shot pipeline step, not on boot.
# Set RUN_MIGRATIONS=1 to opt in to boot-time migration.

set -e

echo "Waiting for postgres at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until nc -z "$POSTGRES_HOST" "$POSTGRES_PORT"; do
  sleep 0.2
done
echo "PostgreSQL started"

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  python manage.py migrate --noinput
fi

python manage.py collectstatic --noinput

exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --timeout "${GUNICORN_TIMEOUT:-60}" \
  --access-logfile - \
  --error-logfile -
