from django.core.management.base import BaseCommand
from django.db.utils import OperationalError

from auctions.models import Lot


class Command(BaseCommand):
    help = "Backfill bap_auto_reason for sold lots in BAP-enabled club auctions."

    def handle(self, *args, **options):
        lots_to_check = (
            Lot.objects.filter(
                is_deleted=False,
                bap_auto_reason="",
                auction__club__enable_breeder_award_program=True,
            )
            .exclude(bap_award__isnull=False)
            .select_related(
                "auction__club",
                "auctiontos_seller__user",
                "species_category",
                "winner",
                "auctiontos_winner",
            )
        )

        updates = []
        checked = 0

        try:
            for lot in lots_to_check.iterator(chunk_size=500):
                checked += 1
                reason = lot.sold_lot_no_bap_reason
                if reason:
                    lot.bap_auto_reason = reason
                    updates.append(lot)
        except OperationalError:
            self.stdout.write(self.style.WARNING("Skipped: database schema not ready for this backfill."))
            return

        if updates:
            Lot.objects.bulk_update(updates, ["bap_auto_reason"], batch_size=500)

        self.stdout.write(
            self.style.SUCCESS(f"Backfill complete: checked {checked} lot(s), updated {len(updates)} lot(s).")
        )
