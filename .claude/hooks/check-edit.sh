#!/usr/bin/env bash
# PostToolUse hook: lint the one file that was just edited, and say so straight away.
#
# All three checks below already run in `docker compose run --rm test --ci` and in pre-commit. The
# point of running them here is *when*: at the edit, while the reasoning that produced the file is
# still in context, instead of at the commit after a dozen more edits have landed on top. Exit
# code 2 is the one that feeds stderr back to Claude, so a failure is a correction rather than a
# message nobody reads.
#
# Fast on purpose -- ruff on a single file is milliseconds and needs no container. Nothing here
# runs Django, touches the database or starts docker; if it ever needs to, it belongs in `--ci`
# instead.

set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-/workspace}" || exit 0

file=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null)
[ -n "$file" ] || exit 0
[ -f "$file" ] || exit 0

case "$file" in
  */.claude/*) exit 0 ;;
  */vendor/*|*.min.js|*.min.css) exit 0 ;;
esac

problems=""

if [[ "$file" == *.py ]]; then
  if ! out=$(ruff check --quiet --no-cache "$file" 2>&1); then
    problems+="ruff check:\n$out\n"
  fi
  if ! out=$(ruff format --check --quiet --no-cache "$file" 2>&1); then
    problems+="ruff format: this file is not formatted. Run: ruff format $file\n"
  fi
fi

# Django's lexer has no re.DOTALL, so a template tag split over two lines renders onto the page as
# text with no error anywhere. See auctions/template_lint.py.
if [[ "$file" == *templates/*.html || "$file" == *templates/*.txt ]]; then
  if ! out=$(python3 auctions/template_lint.py "$file" 2>&1); then
    problems+="template tags:\n$out\n"
  fi
fi

if [ -n "$problems" ]; then
  printf 'Problems in %s:\n\n' "$file" >&2
  printf '%b' "$problems" >&2
  exit 2
fi
exit 0
