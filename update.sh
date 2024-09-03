#!/bin/bash

echo "This will erase any local uncommited changes. Did you make a snapshot? (y/n)"
read -r response

if [[ "$response" != "y" ]]; then
    echo "Cancelled"
    exit 0
fi

# Get latest changes
git restore .
git pull

# Load environment variables from .env file (really only need $SITE_DOMAIN)
if [ -f ./.env ]; then
    while IFS= read -r line; do
    if [ -n "$line" ] && [ "${line:0:1}" != "#" ]; then
        eval "export $line"
    fi
    done < ./.env
else
    echo ".env file not found!"
    # we could populate a default .env file here, not sure that makes sense though
    exit 1
fi

if [ -z "$SITE_DOMAIN" ]; then
    echo "SITE_DOMAIN is not set in the .env file!"
    exit 1
fi

# Replace server_name _; with server_name $SITE_DOMAIN;
sed -i "s/server_name _;/server_name $SITE_DOMAIN;/" "./nginx.prod.conf"

docker compose build
docker compose restart
