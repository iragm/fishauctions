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

# The cloud session's HTTPS goes through a proxy that re-signs TLS with its own CA, so pip inside
# the build fails certificate checks. Cloud-only and invisible to git (never on staging or prod): a
# copy of the Dockerfile that trusts the proxy's bundle after every FROM, and the override file
# compose loads on its own.
ca=/root/.ccr/ca-bundle.crt
if [ -f "$ca" ]; then
  cp "$ca" .cloud-ca.crt
  sed '/^FROM /a COPY .cloud-ca.crt /etc/ssl/certs/cloud-ca.crt\nENV PIP_CERT=/etc/ssl/certs/cloud-ca.crt SSL_CERT_FILE=/etc/ssl/certs/cloud-ca.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/cloud-ca.crt' \
    Dockerfile >Dockerfile.cloud
  cat >docker-compose.override.yaml <<'YAML'
services:
  web: {build: {dockerfile: Dockerfile.cloud}}
  celery_worker: {build: {dockerfile: Dockerfile.cloud}}
  celery_documents: {build: {dockerfile: Dockerfile.cloud}}
  celery_beat: {build: {dockerfile: Dockerfile.cloud}}
  test: {build: {dockerfile: Dockerfile.cloud}}
YAML
  for name in .cloud-ca.crt Dockerfile.cloud docker-compose.override.yaml; do
    grep -qxF "$name" .git/info/exclude 2>/dev/null || echo "$name" >>.git/info/exclude
  done
fi

# Docker Hub rate-limits the shared egress IP (429), so pull through Google's public mirror of it.
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  echo '{"registry-mirrors": ["https://mirror.gcr.io"]}' >/etc/docker/daemon.json
  # a running daemon reads it only at start
  pkill -x dockerd && while pgrep -x dockerd >/dev/null; do sleep 1; done
fi

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
