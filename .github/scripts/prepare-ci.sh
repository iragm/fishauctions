#!/usr/bin/env bash

cp .env.example .env

# Enable SSL bypass for CI environments where SSL inspection may cause issues
echo "DISABLE_PIP_SSL_VERIFY=1" >> .env

mkdir logs
chmod -R 777 logs
