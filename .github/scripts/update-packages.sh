#! /usr/bin/env bash
set -euo pipefail

UPGRADE_DEPS=''

usage() {
  cat << EOF >&2
Usage: $0 [OPTIONS]
Options:
-u, --upgrade  Upgrade all dependencies to the latest version
               DEFAULT: Without setting this flag, only new dependencies are added
-h, --help     Show this message and exit
EOF
}

process_args() {
    while test $# -gt 0
    do
      case "$1" in
          --upgrade | -u) UPGRADE_DEPS='--upgrade'
              ;;
          --help | -h) usage;
              exit 0
              ;;
          *) usage;
              exit 1;
              ;;
      esac
      shift
  done
}

process_args "$@"

docker compose up -d

# Ensure pip is compatible with pip-tools 7.5.1
docker exec -u root django python -m pip install --upgrade --no-cache-dir "pip<25" setuptools wheel

# Run pip-compile inside the container
docker exec django pip-compile ./requirements.in $UPGRADE_DEPS
docker exec django pip-compile ./requirements-test.in $UPGRADE_DEPS
