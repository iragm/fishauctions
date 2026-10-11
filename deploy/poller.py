"""The production server's half of a deploy: picks up a signed request from GitHub and runs update.sh.

Nothing reaches into the server. A systemd timer runs this once a minute; it asks GitHub for the newest
deployment the "Deploy production" workflow requested, and runs ``update.sh`` on master when that
request checks out. The workflow only gets that far after Ira approved the run in GitHub and a fresh
Hostinger snapshot finished, so the server never needs the Hostinger token and nothing listens on a
port for this.

What a request has to pass, and the attack each check is for:

- **A valid signature.** Anybody who can write to the repository can create a deployment, a
  workflow pushed to a branch included. Only a job in the protected ``production`` environment (master
  only, Ira approves each run) can read ``DEPLOY_SIGNING_KEY``, so only it can sign.
- **Freshness and once only.** The payload is public, so an old one could be copied into a new
  deployment. It is refused when older than :data:`MAX_AGE`, and its workflow run id is remembered.
- **Exactly master.** The signed commit must be ``origin/master`` now, and update.sh is told to deploy
  master and nothing else. Nothing in the request is passed to a shell; it can't choose what runs.
- **One at a time.** A lock, and requests already answered (any status) are never picked up again.

The worst a forged or replayed request can do is nothing: it is answered with a failure status. The
worst a stolen signing key can do is redeploy master, which only Ira merges to, without a snapshot.

Statuses go back to GitHub so the workflow (and whoever started it) sees the result: ``error`` for a
request refused before anything ran, ``failure`` only when update.sh ran and failed, which is what
stops the workflow snapshotting over the last good snapshot. The repository is public, so a status
says only what happened, never log text; the log is ``logs/deploy.log``.

Install, once, on the server (as the user that owns the checkout and runs ``update.sh``)::

    install -d -m 700 ~/.config/fishauctions-deploy
    # DEPLOY_SIGNING_KEY: the same value as the production environment's secret in GitHub.
    # GITHUB_TOKEN: a fine-grained token for this repository only, "Deployments: read and write" and
    # nothing else.
    printf 'DEPLOY_SIGNING_KEY=...\\nGITHUB_TOKEN=...\\n' > ~/.config/fishauctions-deploy/env
    chmod 600 ~/.config/fishauctions-deploy/env
    sudo cp deploy/fishauctions-deploy.service deploy/fishauctions-deploy.timer /etc/systemd/system/
    sudoedit /etc/systemd/system/fishauctions-deploy.service   # User=, WorkingDirectory=, EnvironmentFile=
    sudo systemctl daemon-reload && sudo systemctl enable --now fishauctions-deploy.timer

``python3 deploy/poller.py sign`` is the workflow's half: it prints the payload to send.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY = "iragm/fishauctions"
ENVIRONMENT = "production"
#: Marks a deployment as a request for this poller, not the one GitHub records for the job itself.
TASK = "deploy:server"
BRANCH = "master"
#: How old a request may be when picked up. The poller runs every minute; anything older is stale,
#: most likely a request that sat while the server was down, and should not fire by surprise later.
MAX_AGE = timedelta(minutes=20)
#: A clock a little ahead of the server's is fine; one far ahead is a forged timestamp.
MAX_SKEW = timedelta(minutes=2)
#: update.sh builds images and waits for health checks; past this it is hung.
DEPLOY_TIMEOUT = 55 * 60
#: Requests looked at per poll, newest first. Older ones are answered as superseded.
PAGE = 10

CHECKOUT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("DEPLOY_STATE_DIR") or Path.home() / ".local/state/fishauctions-deploy")


class Rejected(Exception):
    """A request that fails a check. Its message becomes the public failure status."""


# --- signing ------------------------------------------------------------------------------------------


def _message(sha: str, run_id: str, requested_at: str) -> bytes:
    return "\n".join((REPOSITORY, ENVIRONMENT, TASK, sha, run_id, requested_at)).encode()


def signature(key: str, sha: str, run_id: str, requested_at: str) -> str:
    return hmac.new(key.encode(), _message(sha, run_id, requested_at), hashlib.sha256).hexdigest()


def make_payload(key: str, sha: str, run_id: str, now: datetime | None = None) -> dict[str, str]:
    requested_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "sha": sha,
        "run_id": run_id,
        "requested_at": requested_at,
        "signature": signature(key, sha, run_id, requested_at),
    }


def verify(key: str, deployment: dict, handled: set[str], now: datetime | None = None) -> dict[str, str]:
    """The payload of a request that passes every check but "is it master now", else :class:`Rejected`."""
    now = now or datetime.now(timezone.utc)
    payload = deployment.get("payload")
    if isinstance(payload, str):  # GitHub returns what it was given; a string if it was sent as one
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        raise Rejected("no payload")
    fields = {name: payload.get(name) for name in ("sha", "run_id", "requested_at", "signature")}
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise Rejected("payload incomplete")
    expected = signature(key, fields["sha"], fields["run_id"], fields["requested_at"])
    if not hmac.compare_digest(expected, fields["signature"]):
        raise Rejected("bad signature")
    if deployment.get("sha") != fields["sha"]:
        raise Rejected("signed commit is not the deployment's commit")
    try:
        requested = datetime.strptime(fields["requested_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise Rejected("bad timestamp")
    if requested > now + MAX_SKEW:
        raise Rejected("timestamp in the future")
    if now - requested > MAX_AGE:
        raise Rejected("request expired")
    if fields["run_id"] in handled:
        raise Rejected("workflow run already handled")
    return fields


# --- GitHub ---------------------------------------------------------------------------------------------


def _github(token: str, method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fishauctions-deploy-poller",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- a fixed https URL
        return json.load(response)


def _status(token: str, deployment_id: int, state: str, description: str) -> None:
    _github(
        token,
        "POST",
        f"/repos/{REPOSITORY}/deployments/{deployment_id}/statuses",
        {"state": state, "description": description[:140], "auto_inactive": False},
    )


# --- state ----------------------------------------------------------------------------------------------


def _handled_file() -> Path:
    return STATE_DIR / "handled.json"


def load_handled() -> set[str]:
    try:
        return set(json.loads(_handled_file().read_text()))
    except (OSError, ValueError):
        return set()


def remember(handled: set[str], *keys: str) -> None:
    """Written before anything runs, so a crash mid-deploy can't make the next poll start it again."""
    handled.update(keys)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = _handled_file().with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(handled)[-500:]))
    temporary.replace(_handled_file())


# --- the deploy -----------------------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed arguments, no shell
        ["git", *args],  # noqa: S607 -- the server's own git, on PATH
        cwd=CHECKOUT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


def run_update(log: Path) -> int:
    environment = {**os.environ, "DEPLOY_BRANCH": BRANCH, "DEPLOY_SNAPSHOT_TAKEN": "1"}
    for secret in ("DEPLOY_SIGNING_KEY", "GITHUB_TOKEN"):  # update.sh and docker need neither
        environment.pop(secret, None)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as output:
        output.write(f"\n===== deploy started {datetime.now(timezone.utc).isoformat()} =====\n")
        output.flush()
        try:
            return subprocess.run(  # noqa: S603 -- fixed arguments, no shell
                [str(CHECKOUT / "update.sh")],
                cwd=CHECKOUT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=DEPLOY_TIMEOUT,
            ).returncode
        except subprocess.TimeoutExpired:
            output.write("\n===== update.sh timed out =====\n")
            return 124


def poll(key: str, token: str) -> str:
    """Answer every unanswered request; deploy the newest valid one. Returns what happened, for the journal."""
    deployments = _github(
        token,
        "GET",
        f"/repos/{REPOSITORY}/deployments?environment={ENVIRONMENT}&task={TASK}&per_page={PAGE}",
    )
    handled = load_handled()
    deployed = None
    for deployment in deployments:  # newest first
        deployment_key = f"deployment:{deployment['id']}"
        if deployment_key in handled:
            continue
        if _github(token, "GET", f"/repos/{REPOSITORY}/deployments/{deployment['id']}/statuses?per_page=1"):
            remember(handled, deployment_key)  # answered already, by an earlier poll or by hand
            continue
        if deployed is not None:
            remember(handled, deployment_key)
            _status(token, deployment["id"], "error", "superseded by a newer request")
            continue
        try:
            fields = verify(key, deployment, handled)
        except Rejected as reason:
            remember(handled, deployment_key)
            _status(token, deployment["id"], "error", f"rejected: {reason}")
            continue
        remember(handled, deployment_key, fields["run_id"])
        deployed = deployment["id"]
        _git("fetch", "--quiet", "origin", BRANCH)
        if _git("rev-parse", f"origin/{BRANCH}") != fields["sha"]:
            _status(token, deployment["id"], "error", "master moved after the snapshot; run the workflow again")
            continue
        _status(token, deployment["id"], "in_progress", "update.sh running")
        code = run_update(CHECKOUT / "logs" / "deploy.log")
        if code == 0:
            _status(token, deployment["id"], "success", f"deployed {fields['sha'][:12]}")
        else:
            _status(token, deployment["id"], "failure", f"update.sh exited {code}; see logs/deploy.log on the server")
    return f"deployed request {deployed}" if deployed else "nothing to deploy"


def main(argv: list[str]) -> int:
    if argv[1:2] == ["sign"]:  # the workflow: python3 deploy/poller.py sign <sha> <run id>
        sha, run_id = argv[2], argv[3]
        print(json.dumps(make_payload(os.environ["DEPLOY_SIGNING_KEY"], sha, run_id)))
        return 0
    key, token = os.environ.get("DEPLOY_SIGNING_KEY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not key or not token:
        print("DEPLOY_SIGNING_KEY and GITHUB_TOKEN must both be set.", file=sys.stderr)
        return 2
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another poll is running")
            return 0
        try:
            print(poll(key, token))
        except (urllib.error.URLError, OSError, subprocess.SubprocessError) as error:
            print(f"poll failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
