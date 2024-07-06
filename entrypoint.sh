#!/bin/sh

# Wait for MariaDB to be ready
DB_HOST=${DATABASE_HOST:-db}
DB_PORT=${DATABASE_PORT:-3306}
until nc -z -v -w30 $DB_HOST $DB_PORT; do
  echo "Waiting for MariaDB..."
  sleep 5
done

# Run migrations and start the server
python manage.py migrate --no-input
python manage.py collectstatic --no-input

if [ "${DEBUG}" = "False" ]; then
    echo Starting fishauctions in productions mode
    cron -f & # run cron in the foreground, but & allows the script to continue
    exec gunicorn fishauctions.asgi:application -k uvicorn.workers.UvicornWorker -w 8 -b 0.0.0.0:8000
else
    echo Starting in development mode, cron jobs must be run manually
    python manage.py createsuperuser --username admin --email admin@example.com --noinput
    uvicorn fishauctions.asgi:application --reload --port 8000
    #python manage.py runserver 0.0.0.0:8000
fi