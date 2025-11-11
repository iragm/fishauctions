#!/usr/bin/env bash

cp .env.example .env

sed -i "/^DEBUG='False'/d" .env
sed -i "/^NGINX_IMAGE='lscr.io\/linuxserver\/swag'/d" .env
sed -i "/^NGINX_CONF='.\/nginx.prod.conf'/d" .env
sed -i "/^NGINX_CONF_LOCATION='\/config\/nginx\/site-confs\/default.conf'/d" .env

mkdir logs
chmod -R 777 logs
