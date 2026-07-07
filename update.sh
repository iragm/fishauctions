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
        # Escape sed replacement metacharacters: backslash FIRST, then the @
        # delimiter and & (whole-match backreference). Missing the backslash meant
        # a value containing '\' (or '@') silently corrupted the written line.
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
    local writable_paths=(./mediafiles ./logs ./auctions/static)
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
}

render_nginx_domain() {
    # Render the __SITE_DOMAIN__ placeholder in nginx.prod.conf from SITE_DOMAIN.
    # The committed file always holds the placeholder (and `git restore .` above
    # resets it before we run), so this is idempotent: no reliance on the current
    # rendered value, and re-running never corrupts the file. Changing SITE_DOMAIN
    # therefore actually takes effect -- the old sed keyed on `server_name _;`,
    # which no longer existed, so it silently did nothing.
    local site_domain escaped_site_domain
    site_domain="$(get_env_value "SITE_DOMAIN")"
    if [ -z "$site_domain" ]; then
        echo "WARNING: SITE_DOMAIN is empty; nginx.prod.conf placeholder left unrendered."
        return
    fi
    # Escape sed replacement metacharacters (& and the @ delimiter).
    escaped_site_domain="$(printf '%s' "$site_domain" | sed 's/[@&\\]/\\&/g')"
    if grep -q "__SITE_DOMAIN__" ./nginx.prod.conf; then
        sed -i "s@__SITE_DOMAIN__@${escaped_site_domain}@g" "./nginx.prod.conf"
    else
        echo "WARNING: __SITE_DOMAIN__ placeholder not found in nginx.prod.conf;"
        echo "         domain not rendered. Is the file checked out cleanly?"
    fi
}

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
# A plain `git pull` silently deploys whatever branch is checked out. Pin the ref
# (override with DEPLOY_BRANCH) so the deployed branch is explicit, and use
# --ff-only so a branch that has diverged from its remote fails loudly instead of
# creating a merge commit / conflict on the server.
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

# Refresh base images so security patches actually arrive. `up --build` alone
# never re-pulls tags that already exist locally, so pinned images (mariadb,
# redis, nginx/swag) and the python base in our Dockerfile's FROM would be frozen
# at whatever was first pulled. `pull` refreshes the pre-built service images;
# `build --pull` re-pulls the base image referenced by FROM before building ours.
docker compose pull
docker compose build --pull
docker compose up -d
