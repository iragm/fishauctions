"""Deploys: the poller refusing what it should, update.sh asking first, and the deploy window.

The server's poller is ``deploy/poller.py``; update.sh only skips its questions when told to; and
:mod:`auctions.deploy_window` says when a deploy would hurt."""

import datetime
import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from auctions import deploy_window
from auctions.models import Auction, Lot, PageView

_spec = importlib.util.spec_from_file_location("deploy_poller", Path(settings.BASE_DIR) / "deploy" / "poller.py")
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

KEY = "a-signing-key"
SHA = "a" * 40
NOW = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=datetime.timezone.utc)


def _deployment(payload, sha=SHA, deployment_id=1):
    return {"id": deployment_id, "sha": sha, "payload": payload}


class PollerVerifyTests(SimpleTestCase):
    def payload(self, **changes):
        payload = poller.make_payload(KEY, SHA, "77", now=NOW - datetime.timedelta(minutes=1))
        payload.update(changes)
        return payload

    def test_a_request_the_workflow_signed_passes(self):
        fields = poller.verify(KEY, _deployment(self.payload()), set(), now=NOW)
        self.assertEqual(fields["sha"], SHA)
        self.assertEqual(fields["run_id"], "77")

    def test_github_handing_the_payload_back_as_a_string_still_passes(self):
        poller.verify(KEY, _deployment(json.dumps(self.payload())), set(), now=NOW)

    def assertRejected(self, deployment, reason, handled=frozenset()):
        with self.assertRaisesMessage(poller.Rejected, reason):
            poller.verify(KEY, deployment, set(handled), now=NOW)

    def test_anyone_with_repo_write_but_not_the_key_is_refused(self):
        forged = poller.make_payload("a-guessed-key", SHA, "77", now=NOW)
        self.assertRejected(_deployment(forged), "bad signature")

    def test_changing_any_signed_field_breaks_the_signature(self):
        for field, value in (("sha", "b" * 40), ("run_id", "78"), ("requested_at", "2026-10-10T11:59:30Z")):
            with self.subTest(field=field):
                self.assertRejected(
                    _deployment(self.payload(**{field: value}), sha=self.payload(**{field: value})["sha"]),
                    "bad signature",
                )

    def test_a_signed_payload_on_another_commit_is_refused(self):
        self.assertRejected(_deployment(self.payload(), sha="c" * 40), "not the deployment's commit")

    def test_an_old_payload_copied_into_a_new_deployment_is_refused(self):
        stale = poller.make_payload(KEY, SHA, "77", now=NOW - datetime.timedelta(minutes=21))
        self.assertRejected(_deployment(stale), "expired")

    def test_a_timestamp_from_the_future_is_refused(self):
        early = poller.make_payload(KEY, SHA, "77", now=NOW + datetime.timedelta(minutes=10))
        self.assertRejected(_deployment(early), "future")

    def test_a_workflow_run_deploys_once(self):
        self.assertRejected(_deployment(self.payload()), "already handled", handled={"77"})

    def test_missing_or_partial_payloads_are_refused(self):
        self.assertRejected(_deployment(None), "no payload")
        self.assertRejected(_deployment("not json"), "no payload")
        self.assertRejected(_deployment({"sha": SHA}), "incomplete")


class PollerPollTests(SimpleTestCase):
    """:func:`poll` against a fake GitHub: what it deploys, and what it answers without deploying."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(poller, "STATE_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.statuses = []
        self.deployments = []
        self.ran = []

    def github(self, token, method, path, body=None):
        if method == "POST":
            self.statuses.append((int(path.split("/")[-2]), body["state"], body["description"]))
            return {}
        if path.endswith("/statuses?per_page=1"):
            deployment_id = int(path.split("/")[-2])
            return [status for status in self.statuses if status[0] == deployment_id][:1]
        return self.deployments

    def run_poll(self, master=SHA):
        with (
            mock.patch.object(poller, "_github", side_effect=self.github),
            mock.patch.object(poller, "_git", side_effect=lambda *args: master if args[0] == "rev-parse" else ""),
            mock.patch.object(poller, "run_update", side_effect=lambda log: self.ran.append(log) or 0),
        ):
            return poller.poll(KEY, "token")

    def signed(self, deployment_id, run_id="77"):
        return _deployment(poller.make_payload(KEY, SHA, run_id), deployment_id=deployment_id)

    def test_a_valid_request_runs_update_once(self):
        self.deployments = [self.signed(5)]
        self.assertEqual(self.run_poll(), "deployed request 5")
        self.assertEqual(len(self.ran), 1)
        self.assertEqual([status[1] for status in self.statuses], ["in_progress", "success"])
        self.run_poll()
        self.assertEqual(len(self.ran), 1)

    def test_forged_requests_are_answered_and_nothing_runs(self):
        self.deployments = [_deployment(poller.make_payload("wrong", SHA, "1"), deployment_id=3)]
        self.assertEqual(self.run_poll(), "nothing to deploy")
        self.assertEqual(self.ran, [])
        self.assertEqual(self.statuses, [(3, "error", "rejected: bad signature")])

    def test_only_the_newest_request_runs(self):
        self.deployments = [self.signed(9, run_id="2"), self.signed(8, run_id="1")]
        self.run_poll()
        self.assertEqual(len(self.ran), 1)
        self.assertIn((8, "error", "superseded by a newer request"), self.statuses)

    def test_master_having_moved_since_the_snapshot_is_refused(self):
        self.deployments = [self.signed(4)]
        self.run_poll(master="d" * 40)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.statuses[-1][1], "error")

    def test_a_failed_update_is_a_failure_status(self):
        self.deployments = [self.signed(6)]
        with (
            mock.patch.object(poller, "_github", side_effect=self.github),
            mock.patch.object(poller, "_git", side_effect=lambda *args: SHA if args[0] == "rev-parse" else ""),
            mock.patch.object(poller, "run_update", return_value=1),
        ):
            poller.poll(KEY, "token")
        self.assertEqual(self.statuses[-1][1], "failure")

    def test_the_secrets_stay_out_of_update_sh(self):
        log = Path(poller.STATE_DIR) / "deploy.log"
        with (
            mock.patch.dict("os.environ", {"DEPLOY_SIGNING_KEY": "k", "GITHUB_TOKEN": "t"}),
            mock.patch.object(poller.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            poller.run_update(log)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("DEPLOY_SIGNING_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertEqual(environment["DEPLOY_BRANCH"], "master")
        self.assertEqual(environment["DEPLOY_SNAPSHOT_TAKEN"], "1")


class UpdateScriptPromptTests(SimpleTestCase):
    """``update.sh`` run without a terminal: cancels unless told it may go ahead unattended.

    A stub ``git`` logs each call and fails the pull, so the script stops before docker."""

    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash or not shutil.which("flock"):
            self.skipTest("bash or flock not installed")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copy(Path(settings.BASE_DIR) / "update.sh", self.root / "update.sh")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.git_log = self.root / "git.log"
        stub = bin_dir / "git"
        stub.write_text(
            f'#!/bin/sh\necho "$*" >> "{self.git_log}"\ncase "$1" in rev-parse) echo staging ;; pull) exit 1 ;; esac\n'
        )
        stub.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def run_update(self, *args, **env):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("DEPLOY_")}
        environment.update(PATH=self.path, **env)
        result = subprocess.run(  # noqa: S603 -- our own script, from a temp copy
            [self.bash, str(self.root / "update.sh"), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        calls = self.git_log.read_text().split("\n") if self.git_log.exists() else []
        return result, calls

    def test_without_a_terminal_it_cancels_and_touches_nothing(self):
        result, calls = self.run_update()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Cancelled", result.stdout)
        self.assertNotIn("restore .", calls)
        self.assertFalse([call for call in calls if call.startswith("pull")])

    def test_yes_flag_and_env_var_go_ahead(self):
        for args, env in ((("-y",), {}), (("--yes",), {}), ((), {"DEPLOY_UNATTENDED": "1"})):
            with self.subTest(args=args, env=env):
                self.git_log.unlink(missing_ok=True)
                result, calls = self.run_update(*args, **env)
                self.assertIn("y (unattended", result.stdout)
                self.assertIn("restore .", calls)
                self.assertIn("pull --ff-only origin staging", calls)
                self.assertEqual(result.returncode, 1)  # the stub's pull failed, before docker

    def test_an_unknown_option_or_value_changes_nothing(self):
        for args, env in ((("--force",), {}), ((), {"DEPLOY_UNATTENDED": "maybe"})):
            with self.subTest(args=args, env=env):
                result, calls = self.run_update(*args, **env)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_the_production_poller_answer_is_unchanged(self):
        result, calls = self.run_update(DEPLOY_SNAPSHOT_TAKEN="1")
        self.assertIn("y (snapshot taken by the deploy workflow)", result.stdout)
        self.assertIn("restore .", calls)

    def test_a_run_still_going_blocks_the_next(self):
        (self.root / "logs").mkdir()
        with (self.root / "logs" / ".update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result, calls = self.run_update("-y")
        self.assertEqual(result.returncode, 1)
        self.assertIn("still running", result.stderr)
        self.assertNotIn("restore .", calls)


class DeployWindowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="seller", password="x")

    def views(self, hours_ago, count):
        when = timezone.now() - datetime.timedelta(hours=hours_ago, minutes=30)
        PageView.objects.bulk_create([PageView(url="/") for _ in range(count)])
        PageView.objects.filter(date_start__gte=timezone.now() - datetime.timedelta(minutes=1)).update(date_start=when)

    def auction(self, is_online, start, end):
        auction = Auction.objects.create(
            created_by=self.user, title="An auction", is_online=is_online, date_start=start, date_end=end
        )
        Lot.objects.create(lot_name="a lot", auction=auction, quantity=1)
        return auction

    def window_for(self, current, history):
        with mock.patch.object(deploy_window, "hourly_views", return_value=[current, *history]):
            return deploy_window.deploy_window()

    def test_page_views_are_counted_by_the_hour(self):
        self.views(0, 3)
        self.views(2, 5)
        counts = deploy_window.hourly_views()
        self.assertEqual(len(counts), deploy_window.HISTORY_HOURS + 1)
        self.assertEqual((counts[0], counts[1], counts[2]), (3, 0, 5))

    def test_a_quiet_hour_with_nothing_running_is_quiet(self):
        window = self.window_for(40, [30] * 100 + [400] * 68)
        self.assertEqual(window["verdict"], "quiet", window)
        self.assertEqual(window["typical_low_per_hour"], 30)
        self.assertIn("fine time", deploy_window.summary(window))

    def test_traffic_well_over_the_lows_is_busy_but_only_advice(self):
        window = self.window_for(200, [30] * 100 + [400] * 68)
        self.assertEqual(window["verdict"], "busy")
        self.assertEqual(window["busiest_hour_last_week"], 400)
        advice = deploy_window.summary(window)
        self.assertIn("Probably not a good time", advice)
        self.assertIn("usually quietest around", advice)

    def test_a_dead_quiet_week_doesnt_make_a_handful_of_views_busy(self):
        self.assertEqual(self.window_for(8, [0] * 168)["verdict"], "quiet")

    def test_auctions_about_to_end_or_running_in_person_are_in_play(self):
        now = timezone.now()
        self.auction(True, now - datetime.timedelta(days=7), now + datetime.timedelta(minutes=40))
        self.auction(False, now - datetime.timedelta(hours=2), now + datetime.timedelta(hours=1))
        self.auction(True, now - datetime.timedelta(days=7), now + datetime.timedelta(days=2))  # ends later
        self.auction(False, now - datetime.timedelta(days=2), now - datetime.timedelta(days=2))  # long over
        empty = Auction.objects.create(
            created_by=self.user, title="No lots", is_online=True, date_start=now, date_end=now
        )
        in_play = deploy_window.auctions_in_play()
        self.assertEqual(len(in_play), 2, in_play)
        self.assertNotIn(empty.slug, [auction["slug"] for auction in in_play])
        self.assertEqual(deploy_window.deploy_window()["verdict"], "busy")

    def test_it_is_one_query_for_the_traffic(self):
        with self.assertNumQueries(1):
            deploy_window.hourly_views()
