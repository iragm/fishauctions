"""Fill an empty test auction with invented people and lots, so an admin can see the site working.

The offer is the banner on the auction page (``AuctionInfo``) when
:func:`auctions.club_health.is_test_auction` says the auction is named like a trial, nobody has added a
lot, and it has not ended. The button posts to ``AuctionSampleData``, which calls :func:`load_sample_data`.

The data is ``auctions/data/sample_auction.json``: ten participants with made-up names and twenty lots
spread across them. The people have no email, phone or address, so nothing the site sends can reach a
real inbox, and they are added the way an admin adds somebody by hand (``manually_added``), which is
also how ``bulk_add_lots`` makes a seller. Lot prices are scaled up to the auction's minimum bid, and
kept whole dollars when the auction only takes whole-dollar bids.

Two cases are refused with a sentence rather than half-done. An auction with no pickup location cannot
hold a participant (``AuctionTOS.pickup_location`` is required), so the admin is told to add one first.
A club-managed auction is refused too: its participants are the club's ``ClubMember`` rows, and ten
invented people would land in the club's real member list.
"""

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from django.db import transaction

from auctions.club_health import is_test_auction
from auctions.models import Auction, AuctionTOS, Lot

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent / "data" / "sample_auction.json"


@dataclass(frozen=True)
class SampleResult:
    """What :func:`load_sample_data` did: counts, or ``problem`` saying why it did nothing."""

    people: int = 0
    lots: int = 0
    problem: str = ""

    @property
    def message(self):
        """One sentence for the admin, either way."""
        if self.problem:
            return self.problem
        return f"Added {self.people} sample people and {self.lots} lots."


def load_fixture():
    """The parsed sample file."""
    with DATA_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def auction_has_lots(auction):
    """Whether the auction holds any lot that has not been deleted."""
    return Lot.objects.filter(auction=auction, is_deleted=False).exists()


def offer_sample_data(auction, user):
    """Whether the auction page should offer the sample data to *user*."""
    return bool(
        user.is_authenticated
        and is_test_auction(auction)
        and not auction.closed
        and not auction.manage_users_through_club
        and auction.permission_check(user)
        and not auction_has_lots(auction)
    )


def scaled_price(price, auction):
    """*price* raised to the auction's minimum bid if lower, and whole dollars when the auction requires it."""
    amount = max(Decimal(str(price)), Decimal(str(auction.minimum_bid)))
    if auction.only_whole_dollar_bids:
        amount = amount.to_integral_value(rounding=ROUND_CEILING)
    return amount.quantize(Decimal("0.01"))


def load_sample_data(auction, user):
    """Add the sample people and lots to *auction* for *user*, an admin. Returns a :class:`SampleResult`.

    Does nothing unless the auction is test-named, open, has a pickup location, is not club-managed and
    has no lots, and *user* may change it. All or nothing; a second call finds the lots and does nothing.
    """
    if not user.is_authenticated or not auction.permission_check(user):
        return SampleResult(problem="Only an admin of this auction can add sample data.")
    if not is_test_auction(auction):
        return SampleResult(problem="Sample data is only offered on test auctions.")
    if auction.closed:
        return SampleResult(problem="This auction has ended, so sample data was not added.")
    if auction.is_club_managed:
        return SampleResult(problem="This auction's people are managed through the club, so sample data was not added.")
    location = auction.location_qs.first()
    if location is None:
        return SampleResult(problem="Add a pickup location first; every person in an auction needs one.")
    data = load_fixture()
    with transaction.atomic():
        # Locked so two clicks at once cannot both find the auction empty.
        locked = Auction.objects.select_for_update().get(pk=auction.pk)
        if auction_has_lots(locked):
            return SampleResult(problem="This auction already has lots, so sample data was not added.")
        sellers = [
            AuctionTOS.objects.create(auction=auction, pickup_location=location, manually_added=True, name=row["name"])
            for row in data["participants"]
        ]
        for row in data["lots"]:
            # Lot.save numbers the lot under the auction row lock, as it does for every other path.
            Lot.objects.create(
                auction=auction,
                auctiontos_seller=sellers[row["seller"]],
                lot_name=row["lot_name"],
                summernote_description=row["description"],
                quantity=row["quantity"],
                reserve_price=scaled_price(row["reserve_price"], auction),
                added_by=user,
            )
        auction.create_history(
            applies_to="LOTS",
            action=f"Added {len(sellers)} sample people and {len(data['lots'])} sample lots",
            user=user,
        )
    return SampleResult(people=len(sellers), lots=len(data["lots"]))
