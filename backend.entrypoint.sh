#!/bin/sh
set -eu

if [ "${1:-}" = "gunicorn" ]; then
    SECRET_KEY="${SECRET_KEY:-${DJANGO_SECRET_KEY:-}}"
    DB_PASSWORD="${DB_PASSWORD:-${POSTGRES_PASSWORD:-}}"
    : "${SECRET_KEY:?Set SECRET_KEY in .env}"
    : "${DB_PASSWORD:?Set DB_PASSWORD in .env}"
    export SECRET_KEY DB_PASSWORD
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput

    if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] \
        && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ] \
        && [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ]; then
        python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

user_model = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
if not user_model.objects.filter(username=username).exists():
    user_model.objects.create_superuser(
        email=os.environ["DJANGO_SUPERUSER_EMAIL"],
        password=os.environ["DJANGO_SUPERUSER_PASSWORD"],
        username=username,
    )
PY
    else
        echo "Skipping automatic superuser creation; set all DJANGO_SUPERUSER_* values."
    fi
    # Start RQ with the web process as in the course's three-service setup.
    python manage.py rqworker default &
fi

exec "$@"
