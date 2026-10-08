"""Count ``Lot.sales_recorded`` for in-person lots sold before it was counted, from their history.

A sale is either a winner message on the lot (``Lot.add_winner_message``, which is also what counts
them now), or an edit on the lot admin form that changed only the sell price: those wrote no lot
message before, just an auction history row. Price edits from before that row existed (July 2025)
are lost, so older auctions read a little more accurate than they were.

Safe to run again: the count is rebuilt from history, which every counted sale also writes.
"""

import datetime
import re
from collections import Counter, defaultdict

from django.core.cache import cache
from django.core.management.base import BaseCommand

from auctions.help_stats import SITE_CACHE_KEY
from auctions.models import Auction, AuctionHistory, Lot, LotHistory

WINNER_MESSAGE = "as the winner of this lot"
EDITED_LOT = re.compile(r"^Edited lot (?P<lot>.+?): (?P<labels>.*)$", re.DOTALL)
# The prose half of the row is keyed on verbose names; "auctiontos winner" before the field had one.
WINNER_LABELS = {"Winner", "auctiontos winner"}
PRICE_LABEL = "winning price"
# The lot admin form writes its history row and the winner message in one request.
SAME_EDIT = datetime.timedelta(minutes=1)


def price_only_edit(action, changed_fields):
    """The lot number display a lot admin edit names, if it changed the sell price and not the winner."""
    match = EDITED_LOT.match(action or "")
    if not match:
        return None
    if changed_fields:
        fields = set(changed_fields)
        price, winner = "winning_price" in fields, "auctiontos_winner" in fields
    else:
        labels = set(match["labels"].split(", "))
        price, winner = PRICE_LABEL in labels, bool(labels & WINNER_LABELS)
    return match["lot"] if price and not winner else None


def count_sales(auction):
    """``({lot pk: sales recorded}, lots)`` for every lot in ``auction``, from its history."""
    lots = list(
        Lot.objects.filter(auction=auction).only(
            "pk", "auction", "custom_lot_number", "lot_number_int", "sales_recorded"
        )
    )
    by_display = {}
    for lot in lots:
        lot.auction = auction
        by_display[str(lot.lot_number_display)] = lot.pk

    messages = defaultdict(list)
    for lot_id, timestamp in LotHistory.objects.filter(
        lot__auction=auction, message__contains=WINNER_MESSAGE
    ).values_list("lot_id", "timestamp"):
        messages[lot_id].append(timestamp)
    counts = Counter({lot_id: len(times) for lot_id, times in messages.items()})

    for action, changed_fields, timestamp in AuctionHistory.objects.filter(
        auction=auction, applies_to="LOTS", action__startswith="Edited lot "
    ).values_list("action", "changed_fields", "timestamp"):
        lot_id = by_display.get(price_only_edit(action, changed_fields))
        if lot_id is None:
            continue
        # Since price edits write a winner message too, that message already counted it.
        if any(abs(timestamp - message) <= SAME_EDIT for message in messages[lot_id]):
            continue
        counts[lot_id] += 1
    return {lot.pk: counts[lot.pk] for lot in lots}, lots


class Command(BaseCommand):
    help = "Backfill how many times each in-person lot's sale was recorded, for bid recorder accuracy"

    def add_arguments(self, parser):
        parser.add_argument("--auction", help="Only this auction's slug")
        parser.add_argument("--dry-run", action="store_true", help="Count, but change nothing")

    def handle(self, *args, **options):
        auctions = Auction.objects.filter(is_online=False, is_deleted=False).order_by("date_start")
        if options["auction"]:
            auctions = auctions.filter(slug=options["auction"])
        changed_lots = 0
        for auction in auctions.iterator():
            counts, lots = count_sales(auction)
            stale = [lot for lot in lots if lot.sales_recorded != counts[lot.pk]]
            for lot in stale:
                lot.sales_recorded = counts[lot.pk]
            changed_lots += len(stale)
            if options["dry_run"]:
                accuracy = None
            else:
                Lot.objects.bulk_update(stale, ["sales_recorded"], batch_size=500)
                accuracy = auction.bid_recorder_accuracy
                if auction.cached_stats and "misc" in auction.cached_stats:
                    auction.cached_stats["misc"]["bid_recorder_accuracy"] = accuracy
                    # update(), not save(): nothing else on the auction changed.
                    Auction.objects.filter(pk=auction.pk).update(cached_stats=auction.cached_stats)
            if stale or accuracy:
                summary = f"{accuracy['percent']}% of {accuracy['lots']} unchanged" if accuracy else ""
                self.stdout.write(f"{auction.slug}: {len(stale)} lots updated. {summary}".strip())
        if not options["dry_run"]:
            cache.delete(SITE_CACHE_KEY)
        verb = "would change" if options["dry_run"] else "changed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {changed_lots} lots"))
