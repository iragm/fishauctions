#!/bin/sh

# Disable core dumps. The worker's CWD is the bind-mounted repo root, so a native
# crash (see the uvloop heap-corruption SIGABRTs) drops a ~200MB core file into the
# source tree and, at that size per crash, will fill the host disk. We keep the
# crash visible via logs/monitoring rather than 200MB forensic dumps in ./.
ulimit -c 0

check_writable_dir() {
  local dir="$1"
  local fix_hint="$2"
  if [ ! -w "$dir" ]; then
    owner_uid=$(stat -c "%u" "$dir")
    owner_gid=$(stat -c "%g" "$dir")
    echo "WARNING: User 'app' (UID: $(id -u), GID: $(id -g)) cannot write to $dir"
    echo "       Directory is owned by UID:$owner_uid GID:$owner_gid."
    echo "👉 $fix_hint"
    echo
  fi
}

setup_complete=$(
python << END
from fishauctions._env import parse_bool_env
import os

print("true" if parse_bool_env(os.environ.get("SETUP_COMPLETE"), default=False) else "false")
END
) || exit $?

if [ "$setup_complete" != "true" ]; then
    echo "Refusing to start containers before setup has been completed."
    echo "Run ./update.sh from the repository root, then start Docker again."
    exit 1
fi

echo Checking directory permissions...
check_writable_dir "/home/app/web/mediafiles" \
  "Fix on the host, from the project root: sudo chown -R $(id -u):$(id -g) ./mediafiles"
# staticfiles is a named volume (see docker-compose.yaml), NOT a bind mount --
# chowning something on the host filesystem cannot fix it.
check_writable_dir "/home/app/web/staticfiles" \
  "This is the 'staticfiles' named volume. Fix its ownership from the host: docker run --rm -v <compose-project>_staticfiles:/v alpine chown -R $(id -u):$(id -g) /v (find the exact name with: docker volume ls)"
# /home/logs is the bind mount of the host's ./logs and is where Django writes its
# log files (settings.py LOG_DIR). Unwritable is non-fatal: settings.py falls back
# to a container-internal dir, so the site boots but logs stop reaching the host.
check_writable_dir "/home/logs" \
  "Fix on the host, from the project root: sudo chown -R $(id -u):$(id -g) ./logs"

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

echo "Applying database migrations..."
if ! python manage.py migrate --no-input; then
    echo "FATAL: 'manage.py migrate' failed -- refusing to start on a half-migrated" >&2
    echo "database (serving against a mismatched schema causes opaque 500s). Fix the" >&2
    echo "failing migration and redeploy. With restart:always this container will keep" >&2
    echo "restarting and re-printing the traceback above until migrations apply." >&2
    exit 1
fi
# Do NOT silence this: STATIC_ROOT is an empty named volume on first boot and the
# third-party statics (admin/, summernote/, ...) are no longer in git, so a failed
# collectstatic means an unstyled site with no other trace. --verbosity 0 keeps the
# per-file spam out of the logs while leaving errors on stderr. Failure is loud but
# non-fatal: on redeploys the volume still holds the previous run's statics, and a
# stale-CSS site beats a down site.
echo "Collecting static files..."
if ! python manage.py collectstatic --no-input --verbosity 0; then
    echo "ERROR: collectstatic failed (see traceback above). Static assets in the" >&2
    echo "'staticfiles' volume are missing or stale; pages will load unstyled if the" >&2
    echo "volume is empty. Starting anyway -- fix the error above and redeploy." >&2
fi
python manage.py setup_celery_beat > /dev/null 2>&1 || true
python manage.py ensure_site_defaults
python manage.py load_demo_data

debug_mode=$(
python << END
from fishauctions._env import parse_bool_env
import os

print("true" if parse_bool_env(os.environ.get("DEBUG"), default=False) else "false")
END
) || exit $?

if [ "$debug_mode" = "true" ]; then
    echo Starting fishauctions in development mode
    # --loop asyncio: match production. Without it uvicorn's loop="auto" picks
    # uvloop (still installed via uvicorn[standard]) -- the exact library whose
    # heap-corruption SIGABRTs gunicorn.conf.py exists to avoid.
    exec uvicorn fishauctions.asgi:application --host 0.0.0.0 --port 8000 --loop asyncio --reload --reload-include '*.py' --reload-include '*.html' --reload-include '*.js'
else
    echo Starting fishauctions in production mode
    # Worker/loop config lives in gunicorn.conf.py -- it runs uvicorn on the
    # stdlib asyncio loop instead of uvloop (see that file for the crash history).
    exec gunicorn fishauctions.asgi:application -c gunicorn.conf.py
fi
