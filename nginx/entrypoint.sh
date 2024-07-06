#!/bin/bash
set -e

until curl -s "web:8000" >/dev/null; do
    >&2 echo "waiting for Django"
    sleep 1
done
>&2 echo "Django is up - starting Nginx"
exec nginx -g 'daemon off;'