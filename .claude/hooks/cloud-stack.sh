#!/usr/bin/env bash
# SessionStart hook: in a cloud session (claude.ai/code, a routine, a project thread) bring up the
# same compose stack CI uses, in the background, so tests can run. Locally it does nothing.
#
# Ready when logs/.stack-ready exists; logs/.stack-up.log says why if it never appears. The build
# takes 5-10 minutes on a fresh VM -- start on the code, then wait for the marker before testing.

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
mkdir -p logs
rm -f logs/.stack-ready

nohup bash -c '
  if ! docker info >/dev/null 2>&1; then
    (dockerd >/tmp/dockerd.log 2>&1 &)
    for _ in $(seq 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  ./.github/scripts/prepare-ci.sh &&
    docker compose build &&
    docker compose up --detach --wait --wait-timeout 300 &&
    touch logs/.stack-ready
' >logs/.stack-up.log 2>&1 &
exit 0
