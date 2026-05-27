#!/bin/sh

set -e

echo "Waiting for postgres at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until nc -z "$POSTGRES_HOST" "$POSTGRES_PORT"; do
  sleep 0.2
done
echo "PostgreSQL started"

# Command override (e.g. a one-shot manage.py command) takes precedence.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

python manage.py migrate --noinput
exec python manage.py runserver 0.0.0.0:8000
