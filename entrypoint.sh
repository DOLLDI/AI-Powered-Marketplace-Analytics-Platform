#!/bin/bash
set -e

if [ "${START_CLOUDFLARED:-false}" = "true" ] && [ -z "${PUBLIC_WEBHOOK_BASE_URL:-}" ]; then
    echo "Starting Cloudflare Quick Tunnel..."
    while true; do
        cloudflared tunnel --no-autoupdate --metrics 0.0.0.0:4040 --url http://127.0.0.1:8000
        echo "Cloudflare Quick Tunnel stopped; restarting in 5 seconds..."
        sleep 5
    done &
fi

if [ "${DB_BACKEND:-}" = "postgres" ]; then
    echo "Waiting for PostgreSQL..."
    while ! nc -z "${DATABASE_HOST:-postgres}" "${DATABASE_PORT:-5432}"; do
        sleep 1
    done
    echo "PostgreSQL is ready"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
