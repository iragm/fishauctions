from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone

from auctions.models import BapAward, Lot


class Command(BaseCommand):
    help = "Backfill bap_auto_reason for sold lots in BAP-enabled club auctions, and auto-award points where eligible."

    def handle(self, *args, **options):
        # ── Step 1: Live food cultures in culture-program clubs ─────────────────
        # When a club has separate_cap enabled, "Live food cultures" lots are
        # eligible for CAP (culture) points.  Lots submitted before the club
        # turned on separate_cap may have i_bred_this_fish=False.  Set it to
        # True so downstream reason/award logic can treat them correctly.
        culture_lots = Lot.objects.filter(
            is_deleted=False,
            auction__club__enable_breeder_award_program=True,
            auction__club__separate_cap=True,
            species_category__name="Live food cultures",
            i_bred_this_fish=False,
        ).select_related("auction__club", "species_category")

        culture_updated = culture_lots.update(i_bred_this_fish=True)
        self.stdout.write(f"Step 1: set i_bred_this_fish=True on {culture_updated} live-food-culture lot(s).")

        # ── Step 2: Fill lot.date_end for sold breeder-points lots missing it ───
        # Lots submitted outside an auction (date_end=None) need a date_end so
        # BapAward.date can be derived from it.  Use date_posted as a proxy.
        lots_missing_end = Lot.objects.filter(
            is_deleted=False,
            i_bred_this_fish=True,
            date_end__isnull=True,
            auctiontos_winner__isnull=False,
            winning_price__isnull=False,
        )
        end_filled = lots_missing_end.update(date_end=F("date_posted"))
        self.stdout.write(f"Step 2: filled date_end from date_posted on {end_filled} sold lot(s).")

        # ── Step 3: Fix BapAward.date when lot had no date_end ──────────────────
        # Awards created before Step 2 may have an incorrect date (e.g. today's
        # date from timezone.now()).  Re-derive the date from lot.date_posted for
        # any award whose lot still has no date_end (or whose award date doesn't
        # match the lot's creation date when date_end was absent at award time).
        awards_to_fix = (
            BapAward.objects.filter(
                lot__isnull=False,
                lot__date_end__isnull=True,
            )
            .select_related("lot")
            .exclude(lot__is_deleted=True)
        )
        award_updated = 0
        award_updates = []
        for award in awards_to_fix.iterator(chunk_size=500):
            new_date = award.lot.date_posted.date() if award.lot.date_posted else timezone.now().date()
            if award.date != new_date:
                award.date = new_date
                award_updates.append(award)
                if len(award_updates) >= 500:
                    BapAward.objects.bulk_update(award_updates, ["date"], batch_size=500)
                    award_updated += len(award_updates)
                    award_updates.clear()
        if award_updates:
            BapAward.objects.bulk_update(award_updates, ["date"], batch_size=500)
            award_updated += len(award_updates)
        self.stdout.write(f"Step 3: corrected date on {award_updated} BapAward(s).")

        # ── Step 4: Backfill bap_auto_reason and auto-award ─────────────────────
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

        reason_updates = []
        checked = 0
        updated = 0
        awarded = 0

        def flush_updates():
            nonlocal updated
            if not reason_updates:
                return
            Lot.objects.bulk_update(reason_updates, ["bap_auto_reason"], batch_size=500)
            updated += len(reason_updates)
            reason_updates.clear()

        for lot in lots_to_check.iterator(chunk_size=500):
            checked += 1
            reason = lot.sold_lot_no_bap_reason
            new_reason = reason or ""
            if lot.bap_auto_reason != new_reason:
                lot.bap_auto_reason = new_reason
                reason_updates.append(lot)
                if len(reason_updates) >= 500:
                    flush_updates()

            # For eligible lots in auto-add clubs, create the award with correct category points
            if not reason and lot.auction.club.auto_add_points:
                flush_updates()  # flush first so bap_auto_reason is saved before auto_award reads it
                lot.auto_award_bap_points()
                awarded += 1

        flush_updates()

        self.stdout.write(
            self.style.SUCCESS(
                f"Step 4 complete: checked {checked} lot(s), updated reason on {updated}, auto-awarded {awarded}."
            )
        )
