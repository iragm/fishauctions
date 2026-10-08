from django.core.management.base import BaseCommand

from auctions.models import UserData


class Command(BaseCommand):
    help = "Turn the library (/library/ and its MCP tools) on or off for all users"

    def add_arguments(self, parser):
        parser.add_argument(
            "state",
            choices=["on", "off"],
            help="Set 'on' or 'off'.",
        )

    def handle(self, *args, **options):
        state = options["state"] == "on"

        count = UserData.objects.update(library_enabled=state)
        self.stdout.write(
            self.style.SUCCESS(
                f"Library {'ENABLED' if state else 'DISABLED'} for {count} users.  "
                "Set LIBRARY_ENABLED_FOR_USERS in your .env so new users get the same."
            )
        )
