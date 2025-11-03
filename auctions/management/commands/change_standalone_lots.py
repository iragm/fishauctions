import logging

from django.core.management.base import BaseCommand

from auctions.models import UserData

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enable or disable standalone lot selling for all users"

    def add_arguments(self, parser):
        parser.add_argument(
            "state",
            choices=["on", "off"],
            help="Set 'on' or 'off'.",
        )

    def handle(self, *args, **options):
        state = options["state"] == "on"

        count = UserData.objects.update(can_submit_standalone_lots=state)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'ENABLED' if state else 'DISABLED'} for {count} users.  Make sure to update your .env for new users."
            )
        )
