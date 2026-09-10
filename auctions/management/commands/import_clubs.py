"""Import a curated CSV of aquarium clubs.  See auctions/club_import.py for why this is a CSV."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from auctions.club_import import CSV_COLUMNS, ingest, read_csv
from auctions.models import Club


class Command(BaseCommand):
    help = (
        "Import clubs from a CSV. Every club created is a prospect: not on the map, not in club "
        "search, not in any dropdown, until a person approves it.\n\n"
        f"Columns: {', '.join(CSV_COLUMNS)} -- only 'name' is required.\n"
        "contact_method is one of: " + ", ".join(choice for choice, _ in Club.CONTACT_METHOD_CHOICES if choice) + "\n\n"
        "  import_clubs clubs.csv --dry-run\n"
        "  import_clubs clubs.csv"
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to the CSV to import.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Say what would happen and write nothing.",
        )

    def handle(self, *args, **options):
        path = Path(options["csv_file"])
        if not path.is_file():
            msg = f"No such file: {path}"
            raise CommandError(msg)

        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows, complaints = read_csv(handle, source=path.name)

        for complaint in complaints:
            self.stdout.write(self.style.WARNING(f"  {complaint}"))
        if not rows:
            msg = f"Nothing to import from {path}."
            raise CommandError(msg)

        self.stdout.write(f"{len(rows)} row(s) read from {path}")
        if options["dry_run"]:
            # Say which rows are new without writing, by asking the same question ingest will.
            from auctions.club_import import find_existing

            clubs = list(Club.objects.all())
            for row in rows:
                existing = find_existing(row, clubs)
                verdict = f"matches '{existing}'" if existing else "new"
                self.stdout.write(f"  - {row.name} ({row.homepage or 'no homepage'}) -- {verdict}")
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
            return

        report = ingest(rows, source=path.name)
        self.stdout.write(self.style.SUCCESS(str(report)))
        if report.created:
            self.stdout.write(
                f"The {len(report.created)} new club(s) are prospects and appear nowhere public. "
                f"Approve them in the admin (outreach stage -> '{Club.LISTED}') once you have looked."
            )
            self.stdout.write("Run verify_club_links next: a researched URL that 404s is the usual bad row.")
