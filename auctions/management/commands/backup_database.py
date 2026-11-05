"""Back up the core tables to R2 now, or print which tables would be dumped. See auctions/backups.py."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Dump, encrypt and upload the core tables to the offsite bucket now."

    def add_arguments(self, parser):
        from auctions.backups import REASONS

        parser.add_argument("--tables", action="store_true", help="Print the table split and stop.")
        parser.add_argument("--reason", choices=REASONS, default="manual", help="The bucket prefix it goes under.")
        parser.add_argument(
            "--if-configured", action="store_true", help="Exit quietly when the BACKUP_* settings aren't set."
        )

    def handle(self, *args, **options):
        from auctions import backups

        if options["tables"]:
            with_rows, schema_only = backups.table_split()
            self.stdout.write(f"{len(with_rows)} tables with rows; schema only: {', '.join(schema_only)}")
            return
        try:
            result = backups.run(options["reason"])
        except backups.BackupError as exc:
            raise CommandError(str(exc)) from exc
        if result is None:
            if options["if_configured"]:
                self.stdout.write("Backup not configured; skipped.")
                return
            msg = "Not configured: set every BACKUP_* setting (see .env.example)."
            raise CommandError(msg)
        self.stdout.write(f"Uploaded {result['bytes']} bytes to {', '.join(result['keys'])}.")
