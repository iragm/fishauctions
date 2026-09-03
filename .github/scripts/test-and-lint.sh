#! /usr/bin/env bash

set -euo pipefail

RUFF_MODE=''
RUFF_FLAGS=''

usage() {
  cat << EOF >&2
Usage: $0 [OPTIONS]
Options:

--ci                  Run in CI mode: run all tests, lints, and formatting.
                      Fail if changes are required
-f, --format          Format the code
-F, --format-check    Run the formatter and fail if changes would be made
-l, --lint            Lint the code
-L, --lint-check      Run the linter and fail if changes would be made
-h, --help      Show this message and exit
EOF
}

process_args() {
    while test $# -gt 0
    do
      case "$1" in
          --ci) IS_CI='true'
              ;;
          --format | -f) RUFF_MODE='format'
              ;;
          --format-check | -F) RUFF_MODE='format'
              RUFF_FLAGS='--check'
              ;;
          --lint | -l) RUFF_MODE='check'
              RUFF_FLAGS='--fix'
              ;;
          --lint-check | -L) RUFF_MODE='check'
              ;;
          --verbose) VERBOSE='true'
              ;;
          --help | -h) usage;
              exit 0
              ;;
          *) echo "Unknown argument: $1" >&2
             usage
             exit 1
              ;;
      esac
      shift
  done
}

process_args "$@"

# Django template tags have to open and close on the same line or they render onto the page as
# text — see auctions/template_lint.py. Ruff can't see inside templates, so this runs alongside
# it. Same code the auctions.test_template_hygiene tests use.
check_templates() {
  python3 /home/app/web/auctions/template_lint.py /home/app/web
}

# docs/module_map.md is generated from the modules' own docstrings, so it can go stale the moment
# somebody adds a file. This regenerates it in memory and fails if the checked-in copy differs, and
# enforces the docstring and file-size rules in auctions/module_map.py. Same code the
# auctions.test_module_map tests use.
check_module_map() {
  python3 /home/app/web/auctions/module_map.py
}

if [ -z ${IS_CI+x} ]; then
  eval "ruff ${RUFF_MODE} /home/app/web ${RUFF_FLAGS}"
  if [ "${RUFF_MODE}" = 'check' ]; then
    check_templates
    check_module_map
  fi
else
  ruff format /home/app/web --check
  ruff check /home/app/web
  check_templates
  check_module_map
fi
