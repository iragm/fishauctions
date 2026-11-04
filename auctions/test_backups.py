"""Tests for the nightly offsite backup (auctions/backups.py)."""

import gzip
import io
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock, skipUnless
from zoneinfo import ZoneInfo

from django.apps import apps
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from auctions import backups
from auctions.mcp import admin
from auctions.models import Lot, PageView

CONFIGURED = {
    "BACKUP_S3_ENDPOINT": "https://example.r2.cloudflarestorage.com",
    "BACKUP_S3_BUCKET": "backups",
    "BACKUP_S3_ACCESS_KEY_ID": "id",
    "BACKUP_S3_SECRET_ACCESS_KEY": "secret",
    "BACKUP_AGE_RECIPIENT": "age1examplepublickey",
}
EASTERN = ZoneInfo("America/New_York")


class TableSplitTests(TestCase):
    def test_core_tables_keep_rows_and_excluded_ones_do_not(self):
        with_rows, schema_only = backups.table_split()
        self.assertIn(Lot._meta.db_table, with_rows)
        self.assertIn(PageView._meta.db_table, schema_only)
        self.assertIn("django_session", schema_only)
        self.assertFalse(set(with_rows) & set(schema_only))

    def test_many_to_many_of_an_excluded_model_is_excluded_too(self):
        club_history = apps.get_model("auctions.ClubHistory")
        through = club_history._meta.get_field("members").remote_field.through._meta.db_table
        self.assertIn(through, backups.schema_only_tables())

    def test_no_kept_row_points_at_a_row_left_out(self):
        """A kept foreign key into an excluded table would come back dangling after a restore."""
        excluded = {apps.get_model(label) for label in backups.SCHEMA_ONLY_MODELS}
        dangling = [
            f"{model.__name__}.{field.name}"
            for model in apps.get_models()
            if model not in excluded
            for field in model._meta.get_fields()
            if field.concrete and field.is_relation and field.related_model in excluded
        ]
        self.assertEqual(dangling, [])


class KeysTests(SimpleTestCase):
    @override_settings(TIME_ZONE="America/New_York")
    def test_each_reason_is_its_own_prefix(self):
        when = datetime(2026, 11, 15, 3, 30, tzinfo=EASTERN)
        self.assertEqual(backups.keys(when, "weekly"), ["weekly/2026-11-15T033000.sql.gz.age"])
        self.assertEqual(backups.keys(when, "deploy"), ["deploy/2026-11-15T033000.sql.gz.age"])

    @override_settings(TIME_ZONE="America/New_York")
    def test_first_weekly_run_of_a_month_is_also_monthly(self):
        found = backups.keys(datetime(2026, 11, 1, 3, 30, tzinfo=EASTERN), "weekly")
        self.assertEqual([key.split("/")[0] for key in found], ["weekly", "monthly"])
        self.assertEqual(len(backups.keys(datetime(2026, 11, 1, 3, 30, tzinfo=EASTERN), "deploy")), 1)

    def test_unknown_reason_is_refused(self):
        with self.assertRaises(backups.BackupError):
            backups.keys(datetime(2026, 11, 1, tzinfo=EASTERN), "../etc")


class RunTests(SimpleTestCase):
    def setUp(self):
        cache.delete(backups.RUNNING_KEY)
        self.addCleanup(cache.delete, backups.RUNNING_KEY)

    def test_unconfigured_does_nothing(self):
        with mock.patch.object(backups, "write_sealed") as dump:
            self.assertIsNone(backups.run())
        dump.assert_not_called()

    @override_settings(**{**CONFIGURED, "BACKUP_AGE_RECIPIENT": "not-a-key"})
    def test_refuses_to_upload_without_a_real_recipient(self):
        with mock.patch.object(backups, "upload") as upload, self.assertRaises(backups.BackupError):
            backups.run()
        upload.assert_not_called()

    @override_settings(**CONFIGURED)
    def test_dumps_encrypts_and_uploads(self):
        def fake_sealed(target, recipient, workdir):
            self.assertEqual(recipient, "age1examplepublickey")
            target.write_bytes(b"sealed!")
            return 3, 1

        with (
            mock.patch.object(backups, "write_sealed", side_effect=fake_sealed),
            mock.patch.object(backups, "upload") as upload,
        ):
            result = backups.run("deploy", datetime(2026, 11, 4, 3, 30, tzinfo=EASTERN))
        cfg, path, keys = upload.call_args.args
        self.assertEqual(cfg.bucket, "backups")
        self.assertEqual(keys, ["deploy/2026-11-04T033000.sql.gz.age"])
        self.assertEqual(result["bytes"], 7)
        self.assertEqual(result["tables_with_rows"], 3)
        self.assertIsNone(cache.get(backups.RUNNING_KEY))

    @override_settings(**CONFIGURED)
    def test_two_runs_never_overlap_and_a_failure_frees_the_next(self):
        cache.set(backups.RUNNING_KEY, "weekly/x", 60)
        with mock.patch.object(backups, "write_sealed") as sealed, self.assertRaises(backups.BackupError):
            backups.run()
        sealed.assert_not_called()
        cache.delete(backups.RUNNING_KEY)
        with mock.patch.object(backups, "write_sealed", side_effect=OSError("disk full")), self.assertRaises(OSError):
            backups.run()
        self.assertIsNone(cache.get(backups.RUNNING_KEY))

    @override_settings(**CONFIGURED)
    def test_command_reports_failure(self):
        with mock.patch.object(backups, "write_sealed", side_effect=backups.BackupError("dump exited 2")):
            with self.assertRaisesMessage(CommandError, "dump exited 2"):
                call_command("backup_database", stdout=io.StringIO())

    def test_command_if_configured_skips_quietly(self):
        out = io.StringIO()
        call_command("backup_database", "--reason", "deploy", "--if-configured", stdout=out)
        self.assertIn("skipped", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("backup_database", stdout=io.StringIO())


class AdminToolTests(TestCase):
    def setUp(self):
        cache.delete(admin._BACKUP_KEY)
        self.addCleanup(cache.delete, admin._BACKUP_KEY)

    def test_refused_when_not_configured(self):
        with mock.patch("auctions.tasks.backup_database.delay") as delay:
            self.assertIn("error", admin.backup_database(None, {}))
        delay.assert_not_called()

    @override_settings(**CONFIGURED)
    def test_queues_after_commit_and_one_per_six_hours(self):
        with mock.patch("auctions.tasks.backup_database.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertNotIn("error", admin.backup_database(None, {}))
                delay.assert_not_called()
            self.assertIn("error", admin.backup_database(None, {}))
        delay.assert_called_once_with("manual")


class DumpTests(TestCase):
    def test_password_stays_off_the_command_line_and_excluded_tables_are_schema_only(self):
        calls = []

        def fake_run(args, out, workdir):
            calls.append(args)
            out.write(b"-- dump\n")

        out = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(backups, "_run_dump", side_effect=fake_run):
            with mock.patch.object(backups, "_dump_binary", return_value="mariadb-dump"):
                backups.write_dump(out, Path(tmp))
        self.assertEqual(gzip.decompress(out.getvalue()), b"-- dump\n-- dump\n")
        data, schema = calls
        name = backups.settings.DATABASES["default"]["NAME"]
        password = backups.settings.DATABASES["default"]["PASSWORD"]
        self.assertIn(f"--ignore-table={name}.{PageView._meta.db_table}", data)
        self.assertIn("--no-data", schema)
        self.assertIn(PageView._meta.db_table, schema)
        self.assertNotIn(Lot._meta.db_table, schema)
        if password:
            self.assertFalse([arg for arg in data + schema if password in arg])

    @skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age is installed in the image")
    def test_round_trip_through_age_and_the_real_dump(self):
        age, keygen = shutil.which("age"), shutil.which("age-keygen")
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            key = str(workdir / "key.txt")
            subprocess.run([keygen, "-o", key], check=True, capture_output=True)  # noqa: S603
            recipient = subprocess.run([keygen, "-y", key], check=True, capture_output=True, text=True)  # noqa: S603
            sealed = workdir / "core.age"
            backups.write_sealed(sealed, recipient.stdout.strip(), workdir)
            self.assertFalse(sealed.read_bytes().startswith(b"\x1f\x8b"))
            plain = subprocess.run([age, "-d", "-i", key, str(sealed)], check=True, capture_output=True).stdout  # noqa: S603
        text = gzip.decompress(plain).decode()
        self.assertIn(f"CREATE TABLE `{Lot._meta.db_table}`", text)
        self.assertIn(f"CREATE TABLE `{PageView._meta.db_table}`", text)

    def test_a_failed_dump_raises_with_its_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesMessage(backups.BackupError, "dump exited 3: oops"):
                backups._run_dump(["sh", "-c", "echo oops >&2; exit 3"], io.BytesIO(), Path(tmp))


class UploadTests(SimpleTestCase):
    def test_size_mismatch_raises_before_copying(self):
        s3 = mock.Mock()
        s3.head_object.return_value = {"ContentLength": 1}
        cfg = backups.Config("e", "b", "i", "s", "age1x")
        with tempfile.NamedTemporaryFile() as handle, mock.patch.object(backups, "client", return_value=s3):
            handle.write(b"12345")
            handle.flush()
            with self.assertRaises(backups.BackupError):
                backups.upload(cfg, Path(handle.name), ["daily/a", "weekly/a"])
        s3.copy_object.assert_not_called()

    def test_copies_to_weekly_and_monthly(self):
        s3 = mock.Mock()
        cfg = backups.Config("e", "b", "i", "s", "age1x")
        with tempfile.NamedTemporaryFile() as handle, mock.patch.object(backups, "client", return_value=s3):
            handle.write(b"12345")
            handle.flush()
            s3.head_object.return_value = {"ContentLength": 5}
            backups.upload(cfg, Path(handle.name), ["daily/a", "weekly/a", "monthly/a"])
        self.assertEqual([c.kwargs["Key"] for c in s3.copy_object.call_args_list], ["weekly/a", "monthly/a"])
