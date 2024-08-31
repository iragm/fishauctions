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

eval "docker exec django pip-compile ./requirements.in ${UPGRADE_DEPS}"
eval "docker exec django pip-compile ./requirements-test.in ${UPGRADE_DEPS}"
