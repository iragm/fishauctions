from django.core.management.base import BaseCommand

from auctions.models import Lot


class Command(BaseCommand):
    help = "Backfill bap_auto_reason for sold lots in BAP-enabled club auctions, and auto-award points where eligible."

    def handle(self, *args, **options):
        lots_to_check = (
            Lot.objects.filter(
                is_deleted=False,
                auction__club__enable_breeder_award_program=True,
            )
            .filter(
                winning_price__isnull=False,
                auctiontos_winner__isnull=False,
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
        awarded = 0

        def flush_updates():
            nonlocal updated
            if not updates:
                return
            Lot.objects.bulk_update(updates, ["bap_auto_reason"], batch_size=500)
            updated += len(updates)
            updates.clear()

        for lot in lots_to_check.iterator(chunk_size=500):
            checked += 1
            reason = lot.sold_lot_no_bap_reason
            new_reason = reason or ""
            if lot.bap_auto_reason != new_reason:
                lot.bap_auto_reason = new_reason
                updates.append(lot)
                if len(updates) >= 500:
                    flush_updates()

            # For eligible lots in auto-add clubs, create the award with correct category points
            if not reason and lot.auction.club.auto_add_points:
                flush_updates()  # flush first so bap_auto_reason is saved before auto_award reads it
                lot.auto_award_bap_points()
                awarded += 1

        flush_updates()

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: checked {checked} lot(s), updated reason on {updated}, auto-awarded {awarded}."
            )
        )
