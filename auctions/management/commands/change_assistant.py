import logging

from django.core.management.base import BaseCommand

from auctions.models import UserData

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Turn the AI assistant (command palette and connected apps) on or off for all users"

    def add_arguments(self, parser):
        parser.add_argument(
            "state",
            choices=["on", "off"],
            help="Set 'on' or 'off'.",
        )

    def handle(self, *args, **options):
        state = options["state"] == "on"

        count = UserData.objects.update(use_llm_search=state)
        self.stdout.write(
            self.style.SUCCESS(
                f"AI assistant {'ENABLED' if state else 'DISABLED'} for {count} users.  "
                "Set ASSISTANT_ENABLED_FOR_USERS in your .env so new users get the same."
            )
        )
