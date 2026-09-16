"""The vocabulary the app matches spoken words against, for one auction.

Backs ``GET /api/mobile/auctions/<slug>/voice/vocabulary/``. The accuracy strategy inverts the usual
one: instead of transcribing freely and repairing the text, the app expands the values that exist in
*this* auction into their spoken forms and matches against those. "Fifteen" and "fifty" are a coin
flip acoustically, but if only one is a real bidder here the ambiguity disappears.

**These are strings and nothing normalizes them.** ``AuctionTOS.bidder_number`` is routinely text,
and in seller-dash auctions it spills into lot numbers, so ``BOB-1`` and ``3-1`` are both ordinary
lot numbers. Values go out exactly as stored; the app owns the expansion into spoken forms.

**Auction-scoped, never club-wide.** A bidder number that isn't legal here is a wrong answer the
matcher would produce with full confidence.
"""

from auctions.models import AuctionTOS, ClubMember

# AuctionTOS.save() falls back to this literal when it cannot generate a unique bidder number: a
# broken row, not a bidder, and a word an auctioneer might say out loud.
INVALID_BIDDER_NUMBER = "ERROR"


def _unique(values):
    """Order-preserving de-dupe of already-stringified values, dropping blanks."""
    seen = {}
    for value in values:
        text = "" if value is None else str(value).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def lot_numbers(auction):
    """Lot numbers still a legal answer to "which lot are we selling?".

    Unsold only: a lot with a winner and a price is refused by ``DynamicSetLotWinner.validate_lot``, so
    offering it would only invite a rejected command. A lot ended unsold, or with a price but no
    bidder, can still be sold here.
    """
    lots = (
        auction.lots_qs.exclude(banned=True)
        .exclude(auctiontos_winner__isnull=False, winning_price__isnull=False)
        .order_by("lot_number_int", "custom_lot_number", "pk")
        .values_list("custom_lot_number", "lot_number_int", "lot_number")
    )
    use_custom = auction.use_seller_dash_lot_numbering
    # Mirrors Lot.lot_number_display without loading whole Lot objects: this is every unsold lot,
    # re-fetched on a timer while selling runs.
    numbers = []
    for custom_lot_number, lot_number_int, lot_number in lots:
        if use_custom and custom_lot_number:
            numbers.append(custom_lot_number)
        elif lot_number_int:
            numbers.append(lot_number_int)
        else:
            numbers.append(lot_number)
    return _unique(numbers)


def bidder_numbers(auction):
    """Bidder numbers the set-winners page would accept as a winner.

    Includes the club's members in a club-managed auction, because ``validate_winner`` falls back to
    ``ClubMember`` there (creating a shadow AuctionTOS), so a member who hasn't checked in is still a
    legal answer.
    """
    numbers = list(
        AuctionTOS.objects.filter(auction=auction)
        .exclude(bidder_number="")
        .exclude(bidder_number=INVALID_BIDDER_NUMBER)
        .order_by("bidder_number")
        .values_list("bidder_number", flat=True)
    )
    if auction.is_club_managed and auction.club_id:
        numbers += list(
            ClubMember.objects.filter(club_id=auction.club_id, is_deleted=False)
            .exclude(bidder_number="")
            .exclude(bidder_number=INVALID_BIDDER_NUMBER)
            .order_by("bidder_number")
            .values_list("bidder_number", flat=True)
        )
    return _unique(numbers)


def build_vocabulary(auction):
    """Everything the app needs to match an utterance against this auction.

    The three settings that ride along stop the app proposing a value the page will refuse: half-dollar
    amounts in a whole-dollar auction, and digit-only lot numbers in a seller-dash auction.
    """
    return {
        "lot_numbers": lot_numbers(auction),
        "bidder_numbers": bidder_numbers(auction),
        "only_whole_dollar_bids": auction.only_whole_dollar_bids,
        "use_seller_dash_lot_numbering": auction.use_seller_dash_lot_numbering,
        "currency_symbol": auction.currency_symbol,
    }
