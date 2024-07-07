#!/bin/bash

# This file is called by cron jobs, it sets up the env for them, and then calls python manage.py with the argument passed to it.

if [ $# -eq 0 ]; then
    echo "No argument provided, specify which script to run" > /proc/1/fd/1 2>&1
    exit 1
fi

if [ -f /home/app/web/.env ]; then
    export $(grep -v '^#' /home/app/web/.env | xargs)
else
    echo "No .env file found, env will not be set" > /proc/1/fd/1 2>&1
fi

/usr/local/bin/python /home/app/web/manage.py $1 > /proc/1/fd/1 2>&1
