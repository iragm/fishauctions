#!/bin/bash
cd /opt/fishauctions
git pull origin main
docker compose pull
docker compose up -d