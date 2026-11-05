"""Nightly offsite backup of the core tables: dumped, gzipped, encrypted with ``age``, uploaded to R2.

The Hostinger snapshot is the only other copy, it has one slot, and the hPanel token can delete it along
with the VM. This copy lives on another vendor, is encrypted to a public key whose private half never
touches the server, and is written by a key that a bucket lock keeps from deleting anything young.

What is in a dump: every table's schema, and every table's rows except :data:`SCHEMA_ONLY_MODELS` and
:data:`SCHEMA_ONLY_TABLES` (page views, the changelog and search history tables, caches and logs).
``LotHistory`` is kept: despite its name it is the lot chat and the bid trail. New models are backed up by
default. Image files live in Cloudflare Images and mediafiles and are not in the database at all.

When it runs: weekly from Celery beat (Sunday 3:30 AM), before every deploy (``update.sh``), and when
asked through the admin MCP's ``backup_database``. Each run is ``<reason>/<stamp>.sql.gz.age``, and the
first weekly run of a month is also copied server-side to ``monthly/``. Retention is the bucket's job,
set once in the R2 dashboard:

- Object lifecycle rules: delete ``weekly/`` after 57 days, ``deploy/`` after 31, ``manual/`` after 8,
  ``monthly/`` after 366.
- Bucket lock rules: ``weekly/`` 56 days, ``deploy/`` 30 days, ``manual/`` 7 days, ``monthly/`` 365 days.

Configured by ``BACKUP_S3_ENDPOINT``, ``BACKUP_S3_BUCKET``, ``BACKUP_S3_ACCESS_KEY_ID``,
``BACKUP_S3_SECRET_ACCESS_KEY`` and ``BACKUP_AGE_RECIPIENT`` (see ``.env.example``). With any of them
unset nothing is backed up and nothing breaks: each run logs a "No offsite backups" warning, the deploy
goes on, and ``site_health`` says so. A failed run raises, which
``site_health`` reports as a failing periodic task.

Restoring (snapshot prod first, as for any prod change). Load into an empty database, so tables from
newer migrations don't survive alongside the restored ``django_migrations``::

    age -d -i backup-key.txt weekly/<stamp>.sql.gz.age | gunzip > dump.sql
    docker exec db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mariadb -uroot -e "DROP DATABASE auctions; CREATE DATABASE auctions"'
    docker exec -i db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mariadb -uroot auctions' < dump.sql
    docker exec django python3 manage.py migrate

``manage.py backup_database`` runs one now; ``--tables`` prints what would be dumped without dumping.
Restore the newest object of ``weekly/``, ``deploy/`` and ``manual/``.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Models whose rows are left out: Ira asked for the core models only (2026-11-04).
SCHEMA_ONLY_MODELS = (
    "auctions.PageView",
    "auctions.AuctionHistory",
    "auctions.ClubHistory",
    "auctions.SearchHistory",
    "auctions.SpeciesSearchCache",
    "auctions.CommandPaletteSearch",
    "auctions.MCPCall",
    "auctions.LLMUsage",
    "auctions.VoiceCommandLog",
    "auctions.PushNotificationSent",
)

#: Tables with no model here whose rows are left out.
SCHEMA_ONLY_TABLES = ("django_session",)

SUFFIX = ".sql.gz.age"

#: Held while a backup runs, so a weekly, a deploy and an asked-for run never overlap.
RUNNING_KEY = "backups:running"
RUNNING_SECONDS = 2 * 60 * 60

#: Why a backup ran; each is its own prefix in the bucket, so each can have its own retention.
REASONS = ("weekly", "deploy", "manual")


class BackupError(RuntimeError):
    """A step of the backup failed; the message says which."""


@dataclass
class Config:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    recipient: str


#: The settings a backup needs, in the order a warning names them.
SETTINGS = (
    "BACKUP_S3_ENDPOINT",
    "BACKUP_S3_BUCKET",
    "BACKUP_S3_ACCESS_KEY_ID",
    "BACKUP_S3_SECRET_ACCESS_KEY",
    "BACKUP_AGE_RECIPIENT",
)


def missing() -> list[str]:
    """The backup settings left unset; empty when backups are configured."""
    return [name for name in SETTINGS if not getattr(settings, name, "")]


def warning() -> str:
    """One line saying backups are off and why, or "" when they're on. Self-hosted sites often run without."""
    unset = missing()
    if not unset:
        return ""
    return f"No offsite backups: {', '.join(unset)} not set (see .env.example)."


def config() -> Config | None:
    """The backup's settings, or None when any is unset."""
    values = Config(
        endpoint=getattr(settings, "BACKUP_S3_ENDPOINT", ""),
        bucket=getattr(settings, "BACKUP_S3_BUCKET", ""),
        access_key_id=getattr(settings, "BACKUP_S3_ACCESS_KEY_ID", ""),
        secret_access_key=getattr(settings, "BACKUP_S3_SECRET_ACCESS_KEY", ""),
        recipient=getattr(settings, "BACKUP_AGE_RECIPIENT", ""),
    )
    # missing() is the one test of "configured", so the warning and the backup never disagree.
    return None if missing() else values


def schema_only_tables() -> list[str]:
    """The tables dumped without rows: the listed models, their many-to-many tables, and the extras."""
    tables = set(SCHEMA_ONLY_TABLES)
    for label in SCHEMA_ONLY_MODELS:
        model = apps.get_model(label)
        tables.add(model._meta.db_table)
        for field in model._meta.local_many_to_many:
            if field.remote_field.through._meta.auto_created:
                tables.add(field.remote_field.through._meta.db_table)
    return sorted(tables)


def table_split() -> tuple[list[str], list[str]]:
    """``(with_rows, schema_only)`` over the tables that exist in the database."""
    existing = set(connection.introspection.table_names())
    schema_only = [table for table in schema_only_tables() if table in existing]
    with_rows = sorted(existing - set(schema_only))
    return with_rows, schema_only


def keys(now: datetime, reason: str) -> list[str]:
    """Object keys for a run at ``now``: ``<reason>/``, plus ``monthly/`` for a month's first weekly run."""
    if reason not in REASONS:
        msg = f"reason must be one of {', '.join(REASONS)}"
        raise BackupError(msg)
    now = timezone.localtime(now)
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    found = [f"{reason}/{stamp}{SUFFIX}"]
    if reason == "weekly" and now.day <= 7:
        found.append(f"monthly/{stamp}{SUFFIX}")
    return found


def _dump_binary() -> str:
    found = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not found:
        msg = "no mariadb-dump or mysqldump on PATH"
        raise BackupError(msg)
    return found


def _dump_args(db: dict, extra: list[str]) -> list[str]:
    return [
        _dump_binary(),
        f"--host={db['HOST']}",
        f"--port={db['PORT'] or 3306}",
        f"--user={db['USER']}",
        "--default-character-set=utf8mb4",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--no-tablespaces",
        "--skip-ssl",
        *extra,
    ]


def _run_dump(args: list[str], out, workdir: Path) -> None:
    """Stream one dump into ``out``; the password goes in the environment, never on the command line."""
    env = {**os.environ, "MYSQL_PWD": settings.DATABASES["default"]["PASSWORD"] or ""}
    with (workdir / "dump.err").open("w+b") as err:
        # The arguments are built in this module and run without a shell.
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err, env=env)  # noqa: S603
        try:
            shutil.copyfileobj(process.stdout, out, 1024 * 1024)
        except BaseException:
            # A time limit or a full disk: don't leave the dump holding its transaction open.
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        code = process.wait()
        if code:
            err.seek(0)
            msg = f"dump exited {code}: {err.read()[-500:].decode(errors='replace').strip()}"
            raise BackupError(msg)


def write_dump(out, workdir: Path) -> tuple[int, int]:
    """Write the gzipped dump to the binary stream ``out``; returns ``(tables with rows, schema-only)``."""
    db = settings.DATABASES["default"]
    name = db["NAME"]
    with_rows, schema_only = table_split()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6) as zipped:
        data_args = [*(f"--ignore-table={name}.{table}" for table in schema_only), name]
        _run_dump(_dump_args(db, data_args), zipped, workdir)
        if schema_only:
            _run_dump(_dump_args(db, ["--no-data", name, *schema_only]), zipped, workdir)
    return len(with_rows), len(schema_only)


def write_sealed(target: Path, recipient: str, workdir: Path) -> tuple[int, int]:
    """Dump, gzip and encrypt straight into ``target``: no unencrypted copy ever touches the disk."""
    age = shutil.which("age")
    if not age:
        msg = "age is not installed"
        raise BackupError(msg)
    with target.open("wb") as sealed, (workdir / "age.err").open("w+b") as err:
        # Argument list, no shell; the recipient was checked to be an age public key.
        process = subprocess.Popen([age, "-r", recipient], stdin=subprocess.PIPE, stdout=sealed, stderr=err)  # noqa: S603
        try:
            counts = write_dump(process.stdin, workdir)
            process.stdin.close()
        except BaseException:
            process.kill()
            process.wait()
            raise
        code = process.wait()
        if code:
            err.seek(0)
            msg = f"age exited {code}: {err.read()[-300:].decode(errors='replace').strip()}"
            raise BackupError(msg)
    return counts


def client(cfg: Config):
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
    )


def upload(cfg: Config, path: Path, object_keys: list[str]) -> None:
    """Upload to the first key, copy to the rest, and check the stored size matches."""
    s3 = client(cfg)
    first, *copies = object_keys
    s3.upload_file(str(path), cfg.bucket, first)
    size = path.stat().st_size
    stored = s3.head_object(Bucket=cfg.bucket, Key=first)["ContentLength"]
    if stored != size:
        msg = f"uploaded {first} is {stored} bytes, expected {size}"
        raise BackupError(msg)
    for key in copies:
        s3.copy_object(Bucket=cfg.bucket, Key=key, CopySource={"Bucket": cfg.bucket, "Key": first})


def run(reason: str = "manual", now: datetime | None = None) -> dict | None:
    """Back up now. None when not configured; raises :class:`BackupError` (or boto's) on failure."""
    cfg = config()
    if cfg is None:
        logger.warning(warning())
        return None
    if not cfg.recipient.startswith("age1"):
        msg = "BACKUP_AGE_RECIPIENT is not an age public key (age1...)"
        raise BackupError(msg)
    object_keys = keys(now or timezone.now(), reason)
    if not cache.add(RUNNING_KEY, object_keys[0], RUNNING_SECONDS):
        msg = f"another backup is running ({cache.get(RUNNING_KEY)})"
        raise BackupError(msg)
    try:
        with tempfile.TemporaryDirectory(prefix="backup-") as tmp:
            workdir = Path(tmp)
            sealed = workdir / f"core{SUFFIX}"
            with_rows, schema_only = write_sealed(sealed, cfg.recipient, workdir)
            upload(cfg, sealed, object_keys)
            size = sealed.stat().st_size
    finally:
        cache.delete(RUNNING_KEY)
    logger.info("Backup uploaded to %s (%s bytes)", ", ".join(object_keys), size)
    return {"keys": object_keys, "bytes": size, "tables_with_rows": with_rows, "tables_schema_only": schema_only}
