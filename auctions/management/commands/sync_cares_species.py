"""Run the weekly CARES species sync now: ``manage.py sync_cares_species``."""

from django.core.management.base import BaseCommand, CommandError

from auctions import cares


class Command(BaseCommand):
    help = "Mark CARES species on the species list from aquaristspecies.net, adding any it lacks."

    def handle(self, *args, **options):
        if not cares.enabled():
            msg = "CARES_PARTNER_KEY is not set."
            raise CommandError(msg)
        try:
            counts = cares.sync()
        except cares.CaresError as error:
            msg = f"The CARES API refused: {error}"
            raise CommandError(msg) from error
        self.stdout.write(", ".join(f"{key}: {value}" for key, value in sorted(counts.items())))
