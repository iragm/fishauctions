#!/bin/sh
# Celery worker entrypoint script

# Disable core dumps: CWD is the bind-mounted repo root, so a native crash would
# drop a large core file into the source tree. See entrypoint.sh for context.
ulimit -c 0

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

echo "Starting Celery worker..."
cd /home/app/web
# CELERY_WORKER_QUEUES picks the queue; the celery_documents service sets it to "documents" (see
# fishauctions/celery.py), with one task per process so a big file's memory goes back afterwards.
if [ "${CELERY_WORKER_QUEUES:-}" = "documents" ]; then
    exec celery -A fishauctions worker --loglevel=info -Q documents --concurrency=1 --max-tasks-per-child=1 -n documents@%h
fi
exec celery -A fishauctions worker --loglevel=info --concurrency=2
