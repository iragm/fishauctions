#!/bin/sh
# Celery beat entrypoint script

echo "Waiting for database..."
python << END
import sys
import time
import MySQLdb
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
    time.sleep(1)
END

echo "Setting up Celery beat periodic tasks..."
cd /home/app/web
python manage.py setup_celery_beat

echo "Starting Celery beat scheduler..."
exec celery -A fishauctions beat --loglevel=info
