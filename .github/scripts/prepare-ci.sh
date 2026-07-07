#!/usr/bin/env bash
set -euo pipefail

# Provisions the throwaway CI .env (deterministic test credentials, DEBUG=True, no
# prod nginx settings). Non-destructive by default: a .env only exists when this is
# run locally (CI always does a clean checkout), so an existing one is KEPT as-is --
# never clobbering a real .env full of live secrets. Pass --force to overwrite; that
# path backs the old one up first to a timestamped .env.bak.* (gitignored), so it is
# always recoverable. CI passes --force to stay deterministic even on reused runners.
force=0
case "${1-}" in
    "") ;;
    --force) force=1 ;;
    *) echo "Usage: $0 [--force]" >&2; exit 2 ;;
esac

if [ -f .env ] && [ "$force" -ne 1 ]; then
    echo "prepare-ci: .env already exists -- keeping it (pass --force to overwrite)." >&2
else
    if [ -f .env ]; then
        backup=".env.bak.$(date +%Y%m%d%H%M%S)"
        cp .env "$backup"
        echo "prepare-ci: --force given; backed up existing .env -> $backup." >&2
    fi

    cp .env.example .env

    # .env.example sets DEBUG='True' for the dev workflow; CI runs in dev mode
    # (it expects EMAIL_BACKEND=console, PAYPAL_API_BASE=sandbox, etc.), so leave
    # the DEBUG line in place. Only strip the production-only nginx settings.
    sed -i "/^NGINX_IMAGE='lscr.io\/linuxserver\/swag'/d" .env
    sed -i "/^NGINX_CONF='.\/nginx.prod.conf'/d" .env
    sed -i "/^NGINX_CONF_LOCATION='\/config\/nginx\/site-confs\/default.conf'/d" .env

    cat <<'EOF' >> .env
SETUP_COMPLETE="1"
SINGLE_CLUB_MODE="False"
SECRET_KEY="ci-secret-key"
DATABASE_PASSWORD="ci-db-password"
DATABASE_ROOT_PASSWORD="ci-db-root-password"
REDIS_PASSWORD="ci-redis-password"
FIELD_ENCRYPTION_KEY="Zy8CqFzroFJjKjaDkOGn-iKtbqF6kU7cZCb4GQW0zI8="
VAPID_PUBLIC_KEY="ci-public-key"
VAPID_PRIVATE_KEY="ci-private-key"
EOF
fi

mkdir -p logs
chmod -R 777 logs
