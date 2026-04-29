#!/usr/bin/env bash

cp .env.example .env

# .env.example sets DEBUG='True' for the dev workflow; CI runs in dev mode
# (it expects EMAIL_BACKEND=console, PAYPAL_API_BASE=sandbox, etc.), so leave
# the DEBUG line in place. Only strip the production-only nginx settings.
sed -i "/^NGINX_IMAGE='lscr.io\/linuxserver\/swag'/d" .env
sed -i "/^NGINX_CONF='.\/nginx.prod.conf'/d" .env
sed -i "/^NGINX_CONF_LOCATION='\/config\/nginx\/site-confs\/default.conf'/d" .env

mkdir -p logs
chmod -R 777 logs
