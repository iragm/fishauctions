#!/bin/sh

# If we're root and not in CI, fix permissions on docker bind mounts, then re-run this script as app
if [ "$(id -u)" = "0" ] && [ -z "$CI" ]; then
  echo "Fixing ownership on media/static..."
  #chown -R app:app /home/app/web/mediafiles /home/app/web/staticfiles || true
  #chown -R app:app /home/app/web/logs || true

  # Drop privileges and re-exec this script as app
  exec su app -c "/entrypoint.sh $*"
fi

echo "Running as user: $(id -un)"

# Wait for MariaDB to be ready
python << END
import sys
import time
import MySQLdb
suggest_unrecoverable_after = 20
start = time.time()
while True:
    try:
        _db = MySQLdb._mysql.connect(
            host="${DATABASE_HOST:-db}",
            user="${DATABASE_USER-mysqluser}",
            password="${DATABASE_PASSWORD-unsecure}",
            database="${DATABASE_NAME-auctions}",
            port=int("${DATABASE_PORT-3306}")
        )
        break
    except MySQLdb._exceptions.OperationalError as error:
        sys.stderr.write("Waiting for MySQL to become available...\n")
        if time.time() - start > suggest_unrecoverable_after:
            sys.stderr.write("  This is taking longer than expected. The following exception may be indicative of an unrecoverable error: '{}'\n".format(error))
    time.sleep(1)
END

# Run migrations and start the server
python manage.py migrate --no-input
python manage.py collectstatic --no-input > /dev/null 2>&1

if [ "${DEBUG}" = "True" ]; then
    echo Starting in development mode, cron jobs must be run manually
    #exec daphne -b 0.0.0.0 -p 8000 fishauctions.asgi:application
    exec uvicorn fishauctions.asgi:application --host 0.0.0.0 --port 8000 --reload --reload-include '*.py' --reload-include '*.html' --reload-include '*.js'
else
    echo Starting fishauctions in production mode
    crontab /etc/cron.d/django-cron
    service cron start
    #exec daphne -b 0.0.0.0 -p 8000 fishauctions.asgi:application
    exec gunicorn fishauctions.asgi:application -k uvicorn.workers.UvicornWorker -w 8 -b 0.0.0.0:8000
fi
