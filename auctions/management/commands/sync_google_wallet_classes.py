from django.core.management.base import BaseCommand

from auctions.google_wallet import is_configured
from auctions.models import Club
from auctions.tasks import create_google_wallet_class_for_club


class Command(BaseCommand):
    help = "Dispatch Google Wallet GenericClass create tasks for every club. Idempotent (409 = already exists)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run inline instead of dispatching to Celery (slower; useful for one-off backfills).",
        )

    def handle(self, *args, **options):
        if not is_configured():
            self.stdout.write(self.style.WARNING("Google Wallet is not configured; nothing to do."))
            return
        clubs = Club.objects.all().order_by("pk")
        count = 0
        for club in clubs:
            if options["sync"]:
                create_google_wallet_class_for_club.apply(args=[club.pk])
            else:
                create_google_wallet_class_for_club.delay(club.pk)
            count += 1
        verb = "Ran" if options["sync"] else "Dispatched"
        self.stdout.write(self.style.SUCCESS(f"{verb} class-create tasks for {count} club(s)."))
