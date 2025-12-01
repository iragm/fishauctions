#!/bin/sh

check_writable_dir() {
  local dir="$1"
  local host_dir="$2"
  if [ ! -w "$dir" ]; then
    # Get UID/GID of directory owner
    owner_uid=$(stat -c "%u" "$dir")
    owner_gid=$(stat -c "%g" "$dir")
    echo "WARNING: User 'app' (UID: $(id -u), GID: $(id -g)) cannot write to $dir"
    echo "       Directory is owned by UID:$owner_uid GID:$owner_gid on the host."
    echo "👉 Fix on the host by running (from your project root, the same directory as update.sh):"
    echo "   sudo chown -R $(id -u):$(id -g) $host_dir"
    echo
  fi
}

echo Checking directory permissions...
check_writable_dir "/home/app/web/mediafiles"   "./mediafiles"
check_writable_dir "/home/app/web/staticfiles"  "./auctions/static"
check_writable_dir "/home/app/web/logs"         "./logs"

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

# Run migrations and setup
python manage.py migrate --no-input
python manage.py collectstatic --no-input > /dev/null 2>&1

# Setup Celery Beat periodic tasks in database (idempotent - safe to run multiple times)
python manage.py setup_celery_beat > /dev/null 2>&1 || true

# Load demo data if in DEBUG mode and database is empty (idempotent - safe to run multiple times)
python manage.py load_demo_data

if [ "${DEBUG}" = "True" ]; then
    echo Starting fishauctions in development mode
    exec uvicorn fishauctions.asgi:application --host 0.0.0.0 --port 8000 --reload --reload-include '*.py' --reload-include '*.html' --reload-include '*.js'
else
    echo Starting fishauctions in production mode
    exec gunicorn fishauctions.asgi:application -k uvicorn.workers.UvicornWorker -w 8 -b 0.0.0.0:8000
fi
