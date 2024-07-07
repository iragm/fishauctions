#!/bin/bash

if [ $# -eq 0 ]; then
    echo "No argument provided, specify which script to run" >> /var/log/cron.log 2>&1
    exit 1
fi

if [ -f /home/app/web/.env ]; then
    export $(grep -v '^#' /home/app/web/.env | xargs)
else
    echo "No .env file found, env will not be set" >> /var/log/cron.log 2>&1
fi

/usr/local/bin/python /home/app/web/manage.py $1 >> /var/log/cron.log 2>&1
