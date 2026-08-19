import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from auctions.models import Lot, normalize_email

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "One-off repair: fill in Lot.user for lots that were added through an AuctionTOS that had "
        "no linked account at the time. Those lots were saved with user=None, so their seller was "
        "refused by every check keyed on Lot.user -- they could see the lot on their selling "
        "dashboard and their invoice, but editing it said 'Only the lot creator can edit a lot'. "
        "Run relink_auctiontos_users first if orphaned AuctionTOS records are also expected."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without modifying the database.",
        )
        parser.add_argument(
            "--include-deleted",
            action="store_true",
            help="Also repair lots marked is_deleted (skipped by default).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        prefix = "[dry-run] " if dry_run else ""

        candidates = Lot.objects.filter(user__isnull=True, auctiontos_seller__isnull=False)
        if not options["include_deleted"]:
            candidates = candidates.filter(is_deleted=False)
        total = candidates.count()

        # Pass 1: the seller's TOS is already linked to an account, so the owner is unambiguous.
        # This is the whole fix for lots whose TOS was relinked after the lots were created.
        linked = candidates.filter(auctiontos_seller__user__isnull=False).select_related("auctiontos_seller")
        pass_1 = 0
        for lot in linked:
            owner = lot.auctiontos_seller.user
            self.stdout.write(
                f"{prefix}Lot {lot.pk} ({lot.lot_name}) -> user {owner.pk} ({owner}) via linked AuctionTOS "
                f"{lot.auctiontos_seller.pk}"
            )
            if not dry_run:
                Lot.objects.filter(pk=lot.pk).update(user=owner)
            pass_1 += 1

        # Pass 2: the TOS itself is still unlinked, but its email resolves to exactly one active
        # account. Same matching AuctionTOS.save() and relink_auctiontos_users use, so this claims
        # nothing they wouldn't have claimed; it just doesn't touch the TOS row.
        unlinked = candidates.filter(auctiontos_seller__user__isnull=True).select_related("auctiontos_seller")
        users_by_email = {}
        for email in {normalize_email(e) for e in unlinked.values_list("auctiontos_seller__email", flat=True) if e}:
            match = User.objects.filter(is_active=True, email=email)
            if match.count() == 1:
                users_by_email[email] = match.first()
            elif match.count() > 1:
                self.stdout.write(self.style.WARNING(f"Skipping {email}: {match.count()} active accounts share it"))
        pass_2 = 0
        for lot in unlinked:
            owner = users_by_email.get(normalize_email(lot.auctiontos_seller.email))
            if not owner:
                continue
            self.stdout.write(
                f"{prefix}Lot {lot.pk} ({lot.lot_name}) -> user {owner.pk} ({owner}) via email on unlinked "
                f"AuctionTOS {lot.auctiontos_seller.pk}"
            )
            if not dry_run:
                Lot.objects.filter(pk=lot.pk).update(user=owner)
            pass_2 += 1

        unmatched = total - pass_1 - pass_2
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{pass_1 + pass_2} of {total} lots without a user repaired "
                f"({pass_1} from a linked AuctionTOS, {pass_2} matched by email); "
                f"{unmatched} have no account to attribute them to"
            )
        )
