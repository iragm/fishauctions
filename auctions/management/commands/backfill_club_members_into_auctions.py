import logging

from django.core.management.base import BaseCommand

from auctions.models import Auction, AuctionTOS, ClubMember, PickupLocation
from auctions.services import CLUB_MANAGED_MODES, SHARED_MEMBER_FIELDS, clear_bidder_number_in

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "One-off repair for 'manage members through the club': give every club member a participant "
        "row in every one of their club's club-managed auctions, finished ones included. Members "
        "created before this rule existed only got rows in auctions that had not been invoiced yet, "
        "so an admin opening last year's auction could not find half the club there. New members get "
        "these rows automatically (signals.propagate_clubmember_to_shadow_tos); this is for the ones "
        "already in the database. Rows it creates carry no checked_in and no invoice, so they are "
        "somebody the admin can find rather than somebody recorded as having attended."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without modifying the database.",
        )
        parser.add_argument(
            "--club",
            default="",
            help="Limit to one club, by slug. Default: every club.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        prefix = "[dry-run] " if dry_run else ""

        auctions = Auction.objects.filter(
            is_deleted=False,
            club__isnull=False,
            manage_users_through_club__in=CLUB_MANAGED_MODES,
        ).select_related("club")
        if options["club"]:
            auctions = auctions.filter(club__slug=options["club"])

        created = 0
        skipped_no_location = 0
        for auction in auctions.order_by("pk"):
            location = PickupLocation.objects.filter(auction=auction).order_by("-is_default", "pk").first()
            if not location:
                # Nobody can be added to an auction with nowhere to collect from, and inventing a
                # pickup location for a finished auction would be worse than leaving it alone.
                skipped_no_location += 1
                self.stdout.write(f"  skipping {auction} -- no pickup location")
                continue
            already = set(
                AuctionTOS.objects.filter(auction=auction, clubmember__isnull=False).values_list(
                    "clubmember_id", flat=True
                )
            )
            members = ClubMember.objects.filter(club_id=auction.club_id, is_deleted=False).exclude(pk__in=already)
            for member in members.order_by("pk"):
                if not member.bidder_number:
                    if dry_run:
                        continue
                    member.generate_bidder_number(save=True)
                self.stdout.write(f"{prefix}adding {member.name or member.pk} to {auction}")
                created += 1
                if dry_run:
                    continue
                clear_bidder_number_in(auction, member.bidder_number)
                AuctionTOS.objects.create(
                    user=member.user,
                    auction=auction,
                    pickup_location=location,
                    clubmember=member,
                    bidder_number=member.bidder_number,
                    # Bidding is granted at the door in check-in mode and by the club everywhere
                    # else; a row backfilled into an auction that is over grants nothing either way.
                    bidding_allowed=False if auction.manage_users_through_club == "checkin" else member.bidding_allowed,
                    selling_allowed=member.selling_allowed,
                    manually_added=True,
                    **{field: getattr(member, field, None) or "" for field in SHARED_MEMBER_FIELDS},
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{created} participant row(s) created across {auctions.count()} club-managed auction(s)"
                + (
                    f"; {skipped_no_location} auction(s) skipped for having no pickup location"
                    if skipped_no_location
                    else ""
                )
            )
        )
