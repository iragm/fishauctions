#!/bin/sh

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
    uvicorn fishauctions.asgi:application --reload --port 8000 --log-config=./uvicorn_log_config.yml
    #python manage.py runserver 0.0.0.0:8000
else
    echo Starting fishauctions in production mode
    cron -f & # run cron in the foreground, but & allows the script to continue
    exec gunicorn fishauctions.asgi:application -k uvicorn.workers.UvicornWorker -w 8 -b 0.0.0.0:8000
fi