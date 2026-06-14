#!/usr/bin/env bash

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

mkdir -p logs
chmod -R 777 logs
