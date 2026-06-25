import logging

from django.core.management.base import BaseCommand

from auctions.models import SQUARE_TAP_TO_PAY_SCOPE, SquareSeller

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "List Square sellers whose stored OAuth grant is missing the PAYMENTS_WRITE_IN_PERSON "
        "scope and therefore must reconnect before Tap to Pay works. Reads the recorded scopes "
        f"({SQUARE_TAP_TO_PAY_SCOPE!r} present or not) — no calls to Square."
    )

    def handle(self, *args, **options):
        # supports_tap_to_pay reads the recorded scopes; legacy connections have empty scopes.
        sellers = SquareSeller.objects.select_related("user", "club").order_by("user__username")
        impacted = [seller for seller in sellers if not seller.supports_tap_to_pay]

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Square Tap to Pay reconnect audit"))
        self.stdout.write(f"  Need to reconnect: {len(impacted)}")

        if not impacted:
            self.stdout.write(self.style.SUCCESS("No impacted sellers — everyone has the Tap to Pay scope."))
            return

        self.stdout.write("")
        for seller in impacted:
            self.stdout.write(self._describe(seller))

        # A copy-pasteable contact list for the reconnect email.
        emails = sorted({seller.user.email for seller in impacted if seller.user.email})
        if emails:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Email these sellers:"))
            self.stdout.write("  " + ", ".join(emails))

    @staticmethod
    def _describe(seller) -> str:
        user = seller.user
        name = f"{user.first_name} {user.last_name}".strip() or "(no name)"
        club = f" [club: {seller.club.name}]" if seller.club_id else ""
        return f"  - {user.username} <{user.email or 'no email'}>  {name}  (Square: {seller.payer_email or '?'}){club}"
