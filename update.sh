#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

ENV_FILE="$ROOT_DIR/.env"
EXAMPLE_ENV_FILE="$ROOT_DIR/.env.example"

set_env_value() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        local escaped_value
        # Escape sed replacement metacharacters: backslash first, then the @ delimiter and &.
        # Without the backslash, a value containing '\' or '@' corrupted the written line.
        escaped_value="$(printf '%s' "$value" | sed 's/[\\@&]/\\&/g')"
        sed -i "s@^${key}=.*@${key}=${escaped_value}@" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

get_env_value() {
    local key="$1"
    python3 - <<'PY' "$ENV_FILE" "$key"
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
key = sys.argv[2]
value = ""
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    if raw_line.startswith(f"{key}="):
        value = raw_line.split("=", 1)[1].strip().strip("'\"")
        break
print(value)
PY
}

ensure_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        cp "$EXAMPLE_ENV_FILE" "$ENV_FILE"
        echo "Created .env from .env.example"
    fi
}

generate_missing_values() {
    while IFS='=' read -r key value; do
        [ -n "$key" ] || continue
        set_env_value "$key" "$value"
    done < <(python3 - <<'PY' "$ENV_FILE"
from base64 import urlsafe_b64encode
from pathlib import Path
import os
import secrets
import sys

env_path = Path(sys.argv[1])
existing = {}
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    if "=" not in raw_line or raw_line.lstrip().startswith("#"):
        continue
    key, value = raw_line.split("=", 1)
    existing[key.strip()] = value.strip().strip("'\"")

def missing(name):
    return existing.get(name, "").strip() in {"", "secret", "public-key", "private-key", "unsecure"}

generated = {}
if missing("SECRET_KEY"):
    generated["SECRET_KEY"] = secrets.token_urlsafe(50)
if missing("REDIS_PASSWORD"):
    generated["REDIS_PASSWORD"] = secrets.token_urlsafe(24)
if missing("INBOUND_ROUTING_SECRET"):
    generated["INBOUND_ROUTING_SECRET"] = secrets.token_urlsafe(32)
if missing("FIELD_ENCRYPTION_KEY"):
    generated["FIELD_ENCRYPTION_KEY"] = urlsafe_b64encode(os.urandom(32)).decode()
if missing("VAPID_PUBLIC_KEY") or missing("VAPID_PRIVATE_KEY"):
    try:
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
        public_numbers = private_key.public_key().public_numbers()
        public_value = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
        generated["VAPID_PUBLIC_KEY"] = urlsafe_b64encode(public_value).rstrip(b"=").decode()
        generated["VAPID_PRIVATE_KEY"] = urlsafe_b64encode(private_value).rstrip(b"=").decode()
    except ImportError:
        print("Warning: cryptography is not installed locally; generated fallback VAPID placeholders.", file=sys.stderr)
        generated["VAPID_PUBLIC_KEY"] = secrets.token_urlsafe(48)
        generated["VAPID_PRIVATE_KEY"] = secrets.token_urlsafe(24)

for key, value in generated.items():
    print(f"{key}=\"{value}\"")
PY
)
}

ensure_db_credentials() {
    local current_db_pass current_root_pass is_placeholder
    current_db_pass="$(get_env_value "DATABASE_PASSWORD")"
    current_root_pass="$(get_env_value "DATABASE_ROOT_PASSWORD")"

    is_placeholder() {
        local v="$1"
        [[ -z "$v" || "$v" == "secret" || "$v" == "unsecure" ]]
    }

    if ! is_placeholder "$current_db_pass" && ! is_placeholder "$current_root_pass"; then
        return
    fi

    # If a MariaDB data volume already exists, the database was already initialized with
    # whatever password is currently set. Changing it here would break the database.
    if docker volume ls --format '{{.Name}}' 2>/dev/null | grep -q "mariadb_data"; then
        echo ""
        echo "WARNING: DATABASE_PASSWORD is set to a default placeholder, but a MariaDB data"
        echo "volume already exists. Keeping the existing password to avoid breaking the database."
        echo "To secure your installation, update DATABASE_PASSWORD and DATABASE_ROOT_PASSWORD"
        echo "in .env and change the password inside MariaDB before restarting."
        echo ""
        return
    fi

    # No existing data volume — safe to generate fresh credentials.
    if is_placeholder "$current_db_pass"; then
        set_env_value "DATABASE_PASSWORD" "\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')\""
    fi
    if is_placeholder "$current_root_pass"; then
        set_env_value "DATABASE_ROOT_PASSWORD" "\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')\""
    fi
}

prompt_for_site_domain() {
    local current_domain
    current_domain="$(get_env_value "SITE_DOMAIN")"
    if [ -n "$current_domain" ] && [ "$current_domain" != "127.0.0.1" ] && [ "$current_domain" != "example.com" ]; then
        return
    fi

    if [ -t 0 ]; then
        printf "Enter the site domain to use [127.0.0.1]: "
        read -r site_domain
    else
        site_domain="127.0.0.1"
    fi
    site_domain="${site_domain:-127.0.0.1}"
    set_env_value "SITE_DOMAIN" "\"$site_domain\""
}

ensure_permissions() {
    local puid
    local pgid
    # Only dirs the containers WRITE to. auctions/static is no longer listed: the
    # container just reads it (collectstatic source); STATIC_ROOT is a named volume.
    local writable_paths=(./mediafiles ./logs)
    puid="$(get_env_value "PUID")"
    pgid="$(get_env_value "PGID")"
    puid="${puid:-1000}"
    pgid="${pgid:-1000}"
    mkdir -p "${writable_paths[@]}"
    chmod -R 777 logs || true
    if ! chown -R "$puid:$pgid" "${writable_paths[@]}" 2>/dev/null; then
        echo "Could not change ownership on media/static/log directories."
        echo "Please rerun with sudo if needed:"
        echo "  sudo chown -R $puid:$pgid ${writable_paths[*]}"
    fi

    # Wallet credential files are copied into the repo root by hand, often as root, which leaves
    # them unreadable by the container's app user and breaks pass signing until somebody chowns
    # them. Fix them here.
    local wallet_file
    for wallet_file in "$(get_env_value "APPLE_WALLET_CERT_FILE")" \
                       "$(get_env_value "APPLE_WALLET_WWDR_FILE")" \
                       "$(get_env_value "GOOGLE_WALLET_KEYFILE")"; do
        if [ -n "$wallet_file" ] && [ -f "./$wallet_file" ]; then
            if ! chown "$puid:$pgid" "./$wallet_file" 2>/dev/null || ! chmod 600 "./$wallet_file"; then
                echo "Could not fix ownership of ./$wallet_file (wallet credential)."
                echo "  Run: sudo chown $puid:$pgid ./$wallet_file && sudo chmod 600 ./$wallet_file"
            fi
        fi
    done
}

render_nginx_domain() {
    # Render nginx.prod.conf from the tracked template, substituting __SITE_DOMAIN__. The output is
    # gitignored, which is the point: `git restore .` above can't revert it to a placeholder and
    # take the site down, and rendering can't commit the live domain by accident. Idempotent, since
    # it reads the template rather than the current rendered value.
    local template="./nginx.prod.conf.template"
    local output="./nginx.prod.conf"
    local site_domain escaped_site_domain
    # A docker compose command run while the rendered config is missing (a fresh clone) makes
    # Docker create the bind-mount source as a DIRECTORY, and swag then silently serves its own
    # default config. Drop it -- it holds swag's droppings at most, never operator data -- and
    # render.
    if [ -d "$output" ]; then
        echo "WARNING: $output is a directory -- docker compose ran before the nginx config was"
        echo "         rendered, so Docker created the missing bind-mount source as a directory."
        echo "         Removing it and rendering the real config."
        rm -rf "$output"
    fi
    site_domain="$(get_env_value "SITE_DOMAIN")"
    if [ ! -f "$template" ]; then
        echo "WARNING: $template not found; $output not rendered."
        return
    fi
    if ! grep -q "__SITE_DOMAIN__" "$template"; then
        echo "WARNING: __SITE_DOMAIN__ placeholder not found in $template; $output not rendered."
        echo "         Is the file checked out cleanly?"
        return
    fi
    # An empty SITE_DOMAIN renders `server_name ;`, which nginx rejects: abort rather than
    # overwrite a possibly-good config. Git has already advanced, so the old containers keep
    # serving until SITE_DOMAIN is fixed and ./update.sh re-run.
    if [ -z "$site_domain" ]; then
        echo "Update failed: SITE_DOMAIN is empty; refusing to render $output (would break nginx)."
        echo "Set SITE_DOMAIN in .env and re-run ./update.sh. Containers were NOT restarted."
        exit 1
    fi
    # Escape sed replacement metacharacters (& and the @ delimiter).
    escaped_site_domain="$(printf '%s' "$site_domain" | sed 's/[@&\\]/\\&/g')"
    sed "s@__SITE_DOMAIN__@${escaped_site_domain}@g" "$template" > "$output"
}

backup_certs() {
    # Snapshot the cert store before any deploy touches containers: swag revokes and deletes the
    # live cert on a cert-param change or a failed start-time chain check, and the CA rate-limits
    # reissues -- a week with no cert in the 2026-07-20 outage. Archives are a few KB, so keep them
    # all. Failure is a warning: the param guard below is the protection, this is the recovery.
    local backup_dir="$HOME/cert-backups"
    local members=()
    [ -d ./swag/etc/letsencrypt ] && members+=(swag/etc/letsencrypt)
    [ -f ./swag/.donoteditthisfile.conf ] && members+=(swag/.donoteditthisfile.conf)
    if [ "${#members[@]}" -gt 0 ]; then
        mkdir -p "$backup_dir"
        if tar czf "$backup_dir/letsencrypt-$(date +%Y%m%d-%H%M%S).tgz" "${members[@]}" 2>/dev/null; then
            echo "Cert store backed up to $backup_dir"
        else
            echo "WARNING: could not back up ./swag/etc/letsencrypt (run with sudo to fix). Continuing."
        fi
    fi
}

# Preflight: a custom NGINX_IMAGE (prod uses swag) with no NGINX_TAG resolves to a tag that only
# exists for the stock dev nginx image, so the deploy dies at `docker compose pull` -- after git
# has already advanced. Fail here, before anything changes.
if [ -f "$ENV_FILE" ]; then
    nginx_image="$(get_env_value "NGINX_IMAGE")"
    nginx_tag="$(get_env_value "NGINX_TAG")"
    if [ -n "$nginx_image" ] && [ "$nginx_image" != "nginx" ] && [ -z "$nginx_tag" ]; then
        echo "Update failed: NGINX_IMAGE is set to '$nginx_image' but NGINX_TAG is not set."
        echo "Set NGINX_TAG in .env to the release this host currently runs; find it with:"
        echo "  docker inspect nginx --format '{{ index .Config.Labels \"org.opencontainers.image.version\" }}'"
        echo "then add e.g. NGINX_TAG='2.11.0' to .env and re-run. Nothing was changed."
        exit 1
    fi
fi

# Preflight: swag revokes and deletes the live certificate whenever a cert parameter differs from
# the previous run (recorded in swag/.donoteditthisfile.conf), and CA rate limits can then leave
# the site without HTTPS for days (the 2026-07-20 outage). Compare what this deploy would send
# against what swag recorded, and refuse on any drift. The file only exists where swag has run, so
# dev skips this; sourcing it is safe, being one line of ORIG*="..." written by swag itself.
if [ -f "$ENV_FILE" ] && [ -f ./swag/.donoteditthisfile.conf ]; then
    # shellcheck source=/dev/null
    . ./swag/.donoteditthisfile.conf
    cert_param_drift=""
    check_cert_param() {
        local label="$1" sending="$2" recorded="$3"
        if [ "$sending" != "$recorded" ]; then
            cert_param_drift="${cert_param_drift}  ${label}: swag recorded '${recorded}', this deploy would send '${sending}'"$'\n'
        fi
    }
    # These mirror the nginx service env in docker-compose.yaml: URL and EMAIL from .env,
    # VALIDATION hardcoded, CERTPROVIDER passed through, and the rest never set. Update the list
    # if compose changes.
    check_cert_param "URL (SITE_DOMAIN in .env)" "$(get_env_value "SITE_DOMAIN")" "${ORIGURL-}"
    check_cert_param "EMAIL (ADMIN_EMAIL in .env)" "$(get_env_value "ADMIN_EMAIL")" "${ORIGEMAIL-}"
    check_cert_param "CERTPROVIDER (in .env)" "$(get_env_value "CERTPROVIDER")" "${ORIGCERTPROVIDER-}"
    check_cert_param "VALIDATION" "http" "${ORIGVALIDATION-}"
    check_cert_param "SUBDOMAINS" "" "${ORIGSUBDOMAINS-}"
    check_cert_param "EXTRA_DOMAINS" "" "${ORIGEXTRA_DOMAINS-}"
    if [ -n "$cert_param_drift" ] && [ "${ALLOW_CERT_PARAM_CHANGE-}" != "1" ]; then
        echo "Update failed: this deploy would change swag's certificate parameters:"
        printf '%s' "$cert_param_drift"
        echo "swag responds to ANY such change by revoking and deleting the live certificate"
        echo "before requesting a new one, and CA rate limits can then leave the site without"
        echo "HTTPS for days. If the change is intentional, re-run with:"
        echo "  ALLOW_CERT_PARAM_CHANGE=1 ./update.sh"
        echo "Nothing was changed."
        exit 1
    fi
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
deploy_branch="${DEPLOY_BRANCH:-$current_branch}"

echo "Deploying branch: $deploy_branch"
echo "This will erase any local uncommited changes. Did you make a snapshot? (y/n)"
if [ -t 0 ]; then
    read -r response
else
    echo "Non-interactive mode detected; cancelling update by default."
    response="n"
fi

if [[ "$response" != "y" ]]; then
    echo "Cancelled"
    exit 0
fi

git restore .
# A plain `git pull` silently deploys whatever branch is checked out. Pin the ref (override with
# DEPLOY_BRANCH), and use --ff-only so a diverged branch fails instead of merging on the server.
if [ "$deploy_branch" != "$current_branch" ]; then
    if ! git checkout "$deploy_branch"; then
        echo "Update failed: could not checkout '$deploy_branch'. Docker services were not restarted."
        exit 1
    fi
fi
if ! git pull --ff-only origin "$deploy_branch"; then
    echo "Update failed: origin/$deploy_branch could not be fast-forwarded (diverged branch or network error)."
    echo "Docker services were not restarted."
    exit 1
fi

ensure_env_file
prompt_for_site_domain
generate_missing_values
ensure_db_credentials
ensure_permissions
set_env_value "SETUP_COMPLETE" "\"1\""
render_nginx_domain
backup_certs

# Refresh base images so security patches actually arrive: `up --build` never re-pulls tags that
# already exist locally, so mariadb, redis, nginx/swag and the python base in our FROM would be
# frozen at whatever was first pulled. `pull` refreshes the pre-built service images; `build
# --pull` re-pulls the FROM base before building ours.
#
# --ignore-buildable is REQUIRED. web/celery_worker/celery_beat carry an explicit
# `image: ${APP_IMAGE-fishauctions-app}` so CI can build that tag once and all three find it. It
# is pushed to no registry, so a plain pull tries Docker Hub and the deploy dies on "pull access
# denied" -- after git has advanced. The flag skips every service with a build section and still
# fails loudly on a real registry error, which --ignore-pull-failures would have swallowed.
#
# From here on git has advanced, so every failure below says so: the old containers are still
# serving the previous build, and re-running ./update.sh after fixing the error is the recovery.
if ! docker compose pull --ignore-buildable; then
    echo "Update failed during 'docker compose pull' (registry error or bad image tag in .env)."
    echo "Code was already updated by git, but containers were NOT restarted -- the site is"
    echo "still running the previous build. Fix the error above and re-run ./update.sh."
    exit 1
fi
if ! docker compose build --pull; then
    echo "Update failed during 'docker compose build'. Code was already updated by git, but"
    echo "containers were NOT restarted -- the site is still running the previous build."
    echo "Fix the error above and re-run ./update.sh."
    exit 1
fi
# --force-recreate is REQUIRED. App code is bind-mounted rather than baked into the image, so a
# code-only deploy rebuilds a byte-identical image and a plain `up -d` recreates NOTHING --
# gunicorn and celery keep serving the pre-pull code. Worse, nginx proxies to a static
# `proxy_pass http://web:8000` (nginx_fishauctions.conf) and resolves web's IP once at startup, so
# replacing web without restarting nginx leaves nginx pointed at a dead IP. Recreating the whole
# graph fixes both: nginx depends_on web, so it comes up after it and re-resolves.
#
# --wait blocks until every service is running -- healthy, where there is a healthcheck -- and
# fails otherwise, rather than returning success with half the graph down. That case is real:
# after a force-recreate mariadb can spend minutes on crash recovery, the dependency wait times
# out, and compose abandons web and nginx. The manual `docker compose up -d` that used to fix it
# is the single retry below, by which point the slow dependency is usually healthy.
if ! docker compose up -d --force-recreate --wait --wait-timeout 600; then
    echo "'docker compose up' did not reach a healthy state. Current status:"
    docker compose ps --all
    echo "Retrying once with a plain 'docker compose up -d' to start anything the"
    echo "recreate abandoned (e.g. services skipped because a dependency was slow"
    echo "to report healthy)..."
    if ! docker compose up -d --wait --wait-timeout 300; then
        echo "Update failed during 'docker compose up'. Some services may not have restarted;"
        echo "check 'docker compose ps' and the container logs, then re-run ./update.sh."
        exit 1
    fi
fi

# Proof: hit the site through nginx from the host. Anything below 500 counts, the https redirect
# and the login page included; the point is catching "deploy finished but the site is down" while
# the operator is still at the keyboard.
if command -v curl >/dev/null; then
    site_port="$(get_env_value "HTTP_PORT")"
    site_port="${site_port:-80}"
    site_code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "http://127.0.0.1:${site_port}/")" || site_code="000"
    case "$site_code" in
        2*|3*|4*)
            echo "Deploy complete: site answering on port ${site_port} (HTTP ${site_code})."
            ;;
        *)
            echo "Deploy INCOMPLETE: containers are up but http://127.0.0.1:${site_port}/ returned"
            echo "'${site_code}'. Check 'docker compose ps', 'docker logs nginx', 'docker logs django'."
            exit 1
            ;;
    esac
else
    echo "Deploy complete (curl not installed; site check skipped)."
fi
