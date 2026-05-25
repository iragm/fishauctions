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
        updated = 0

        def flush_updates():
            nonlocal updated
            if not updates:
                return
            Lot.objects.bulk_update(updates, ["bap_auto_reason"], batch_size=500)
            updated += len(updates)
            updates.clear()

        try:
            for lot in lots_to_check.iterator(chunk_size=500):
                checked += 1
                reason = lot.sold_lot_no_bap_reason
                if reason:
                    lot.bap_auto_reason = reason
                    updates.append(lot)
                    if len(updates) >= 500:
                        flush_updates()
        except OperationalError:
            self.stdout.write(self.style.WARNING("Skipped: run migrations before executing backfill_bap_reasons."))
            return

        flush_updates()

        self.stdout.write(self.style.SUCCESS(f"Backfill complete: checked {checked} lot(s), updated {updated} lot(s)."))
