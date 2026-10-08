"""What a merge carries over: everything that points at the participant, club member or account it closes.

``AuctionTOS.merge_duplicate`` and ``ClubMember.merge_duplicate`` do the moving; the merge pages and the
account merge call them. ``MergeCoverageTests`` fails on a new relation until somebody decides where it goes.
"""

import datetime
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.account_merge import merge_accounts
from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Bid,
    Club,
    ClubMember,
    Invoice,
    InvoicePayment,
    Lot,
    PickupLocation,
    VolunteerJob,
    VolunteerSignup,
)


def relations_to(target):
    return {
        f"{model._meta.label}.{field.name}"
        for model in apps.get_models()
        for field in model._meta.get_fields()
        if field.is_relation
        and not field.auto_created
        and field.concrete
        and field.related_model is target
        and field.model is model
    }


class MergeCoverageTests(TestCase):
    """Every relation to a participant or a club member is moved by its merge or left behind on purpose."""

    PARTICIPANT_MOVED = {
        "auctions.Invoice.auctiontos_user",
        "auctions.Lot.auctiontos_seller",
        "auctions.Lot.auctiontos_winner",
        "auctions.PickupLocation.contact_person",
        "auctions.VolunteerSignup.auctiontos",
    }
    #: Names the row being deleted, so nothing to keep.
    PARTICIPANT_LEFT = {"auctions.AuctionTOS.possible_duplicate"}
    MEMBER_MOVED = {
        "auctions.AuctionTOS.clubmember",
        "auctions.BapAward.club_member",
        "auctions.Club.auction_email_member",
        "auctions.Club.contact_email_member",
        "auctions.Club.donation_email_member",
        "auctions.ClubMember.membership_carried_by",
        "auctions.Invoice.club_member",
        "auctions.InvoicePayment.club_member",
    }
    #: A phone holds the closed member's own pass, which deactivating voids; the duplicate flag is cleared.
    MEMBER_LEFT = {"auctions.AppleDeviceRegistration.member", "auctions.ClubMember.possible_duplicate"}

    def test_every_relation_is_decided(self):
        self.assertEqual(
            sorted(relations_to(AuctionTOS)),
            sorted(self.PARTICIPANT_MOVED | self.PARTICIPANT_LEFT),
            "Decide what AuctionTOS.merge_duplicate does with a new relation, then list it here.",
        )
        self.assertEqual(
            sorted(relations_to(ClubMember)),
            sorted(self.MEMBER_MOVED | self.MEMBER_LEFT),
            "Decide what ClubMember.merge_duplicate does with a new relation, then list it here.",
        )


class ParticipantMergeTests(TestCase):
    """Merging two participants in one auction."""

    def setUp(self):
        now = timezone.now()
        self.organizer = User.objects.create_user(username="organizer", password="pw", email="organizer@example.com")
        self.old_account = User.objects.create_user(username="old_account", password="pw", email="old@example.com")
        self.new_account = User.objects.create_user(username="new_account", password="pw", email="new@example.com")
        self.auction = Auction.objects.create(
            created_by=self.organizer,
            title="Merge auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=1),
            pre_register_lot_discount_percent=10,
        )
        self.location = PickupLocation.objects.create(
            name="Hall", auction=self.auction, pickup_time=now + datetime.timedelta(days=2)
        )
        self.kept = self.participant("Kept Person", "new@example.com", "1", user=self.new_account)
        self.duplicate = self.participant("Duplicate Person", "old@example.com", "2", user=self.old_account)

    def participant(self, name, email, number, user=None):
        return AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, name=name, email=email, bidder_number=number, user=user
        )

    def lot(self, **kwargs):
        return Lot.objects.create(lot_name="Guppies", auction=self.auction, quantity=1, **kwargs)

    def test_the_lots_accounts_follow_the_kept_participant(self):
        added_themselves = self.lot(
            auctiontos_seller=self.duplicate,
            user=self.old_account,
            added_by=self.old_account,
            label_first_printed_by=self.old_account,
        )
        added_for_them = self.lot(auctiontos_seller=self.duplicate, user=self.old_account, added_by=self.organizer)
        seller = self.participant("Seller", "seller@example.com", "3")
        won = self.lot(
            auctiontos_seller=seller, auctiontos_winner=self.duplicate, winner=self.old_account, winning_price=5
        )

        self.kept.merge_duplicate(self.duplicate)

        for lot in (added_themselves, added_for_them, won):
            lot.refresh_from_db()
        self.assertEqual(
            (added_themselves.user, added_themselves.added_by, added_themselves.label_first_printed_by),
            (self.new_account, self.new_account, self.new_account),
        )
        self.assertTrue(added_themselves.pre_registered)
        self.assertEqual((added_for_them.user, added_for_them.added_by), (self.new_account, self.organizer))
        self.assertEqual(won.winner, self.new_account)
        self.assertEqual(AuctionTOS.objects.get(pk=self.kept.pk).self_submitted_unbanned_lot_count, 1)

    def test_bids_here_become_the_kept_participants(self):
        now = timezone.now()
        online = Auction.objects.create(
            created_by=self.organizer,
            title="Online merge auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=1),
        )
        porch = PickupLocation.objects.create(
            name="Porch", auction=online, pickup_time=now + datetime.timedelta(days=2)
        )
        kept, duplicate, seller = (
            AuctionTOS.objects.create(auction=online, pickup_location=porch, name=name, email=email, user=user)
            for name, email, user in (
                ("Kept", "new@example.com", self.new_account),
                ("Duplicate", "old@example.com", self.old_account),
                ("Seller", "seller@example.com", None),
            )
        )
        winning, both = (
            Lot.objects.create(lot_name=name, auction=online, auctiontos_seller=seller, quantity=1)
            for name in ("Winning", "Both")
        )
        Bid.objects.create(user=self.old_account, lot_number=winning, amount=10)
        higher = Bid.objects.create(user=self.old_account, lot_number=both, amount=20)
        later = higher.bid_time + datetime.timedelta(minutes=1)
        lower = Bid.objects.create(user=self.new_account, lot_number=both, amount=15)
        Bid.objects.filter(pk=lower.pk).update(bid_time=later, last_bid_time=later)
        elsewhere = Bid.objects.create(
            user=self.old_account, lot_number=self.lot(auctiontos_seller=self.kept), amount=5
        )

        kept.merge_duplicate(duplicate)

        winning = Lot.objects.get(pk=winning.pk)
        self.assertEqual(winning.high_bidder, self.new_account)
        winning.sell_to_online_high_bidder()
        self.assertEqual(winning.auctiontos_winner, kept)
        # Their better bid on a lot both accounts bid on is the one left standing.
        self.assertEqual([(bid.user, bid.amount) for bid in Lot.objects.get(pk=both.pk).bids], [(self.new_account, 20)])
        # Only this auction's.
        self.assertEqual(Bid.objects.get(pk=elsewhere.pk).user, self.old_account)

    def test_lots_added_before_the_kept_participant_had_an_account_are_claimed(self):
        walk_in = self.participant("Walk In", "", "4")
        lot = self.lot(auctiontos_seller=walk_in, added_by=self.organizer)

        walk_in.merge_duplicate(self.duplicate)

        lot.refresh_from_db()
        self.assertEqual(lot.user, self.old_account)

    def test_volunteer_signups_move_rather_than_cascade(self):
        register = VolunteerJob.objects.create(auction=self.auction, description="Register")
        tables = VolunteerJob.objects.create(auction=self.auction, description="Tables")
        VolunteerSignup.objects.create(job=register, auctiontos=self.duplicate)
        VolunteerSignup.objects.create(job=tables, auctiontos=self.duplicate)
        VolunteerSignup.objects.create(job=tables, auctiontos=self.kept)

        self.kept.merge_duplicate(self.duplicate)

        jobs = VolunteerSignup.objects.filter(auctiontos=self.kept).values_list("job__description", flat=True)
        self.assertEqual(sorted(jobs), ["Register", "Tables"])

    def test_pickup_contact_check_in_and_door_prize_carry_over(self):
        earlier = timezone.now() - datetime.timedelta(hours=2)
        AuctionTOS.objects.filter(pk=self.duplicate.pk).update(checked_in=earlier, door_prize_called=earlier)
        self.duplicate.refresh_from_db()
        PickupLocation.objects.filter(pk=self.location.pk).update(contact_person=self.duplicate)

        self.kept.merge_duplicate(self.duplicate)

        self.kept.refresh_from_db()
        self.location.refresh_from_db()
        self.assertEqual(self.location.contact_person, self.kept)
        self.assertEqual((self.kept.checked_in, self.kept.door_prize_called), (earlier, earlier))

    def test_the_merge_page_keeps_the_account_and_memo_the_kept_participant_lacks(self):
        walk_in = self.participant("Walk In", "walkin@example.com", "4")
        AuctionTOS.objects.filter(pk=self.duplicate.pk).update(memo="Pays cash")
        self.client.force_login(self.organizer)

        response = self.client.post(
            reverse("auctiontosdelete", kwargs={"pk": self.duplicate.pk}) + "?action=merge",
            {
                "action": "merge",
                "step": "review",
                "target": walk_in.pk,
                "name": "Walk In",
                "email": "walkin@example.com",
                "pickup_location": self.location.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        walk_in.refresh_from_db()
        self.assertEqual((walk_in.user, walk_in.memo), (self.old_account, "Pays cash"))


class MemberMergeTests(TestCase):
    """Merging two club members, from the member list, an account merge or a participant merge."""

    def setUp(self):
        now = timezone.now()
        self.today = timezone.localdate()
        self.club = Club.objects.create(name="Merge Club", membership_system="rolling", membership_annual_fee=20)
        self.admin = User.objects.create_user(username="club_admin", password="pw", email="admin@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Club Admin", permission_admin=True)
        self.auction = Auction.objects.create(
            created_by=self.admin,
            title="Club auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=1),
            club=self.club,
            manage_users_through_club="all",
        )
        PickupLocation.objects.create(name="Hall", auction=self.auction, pickup_time=now + datetime.timedelta(days=2))
        self.person = User.objects.create_user(username="person", password="pw", email="person@example.com")
        self.kept = ClubMember.objects.create(
            club=self.club, name="Kept Person", email="kept@example.com", bidder_number="11"
        )
        # Its email is an account's, so saving links it.
        self.duplicate = ClubMember.objects.create(
            club=self.club, name="Duplicate Person", email="person@example.com", bidder_number="22"
        )

    def days(self, count):
        return self.today + datetime.timedelta(days=count)

    def merge_on_the_page(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": self.duplicate.pk}),
            {"step": "review", "target": self.kept.pk, "name": self.kept.name, "email": self.kept.email},
        )
        self.assertEqual(response.status_code, 302)
        self.kept.refresh_from_db()
        self.duplicate.refresh_from_db()

    def test_the_account_link_moves_to_the_kept_member(self):
        self.assertEqual(self.duplicate.user, self.person)
        self.merge_on_the_page()
        self.assertEqual((self.kept.user, self.duplicate.user), (self.person, None))

    def test_the_later_paid_through_date_wins(self):
        ClubMember.objects.filter(pk=self.kept.pk).update(
            membership_last_paid=self.days(-465), membership_expiration_date=self.days(-100)
        )
        ClubMember.objects.filter(pk=self.duplicate.pk).update(
            membership_last_paid=self.days(-100), membership_expiration_date=self.days(265)
        )
        self.merge_on_the_page()
        self.assertEqual(
            (self.kept.membership_last_paid, self.kept.membership_expiration_date), (self.days(-100), self.days(265))
        )

    def test_points_are_recounted_from_both_members_awards(self):
        BapAward.objects.create(club_member=self.kept, date=self.today, points=10)
        BapAward.objects.create(club_member=self.duplicate, date=self.today, points=5)
        self.merge_on_the_page()
        self.assertEqual(self.kept.bap_points, 15)

    def test_dues_invoices_email_routing_and_the_paypal_subscription_follow(self):
        invoice = Invoice.objects.create(
            club=self.club, club_member=self.duplicate, status="UNPAID", renewal_needed=True
        )
        Club.objects.filter(pk=self.club.pk).update(
            contact_email_member=self.duplicate, donation_email_member=self.duplicate
        )
        ClubMember.objects.filter(pk=self.duplicate.pk).update(paypal_subscription_id="I-RENEWS")

        self.merge_on_the_page()

        invoice.refresh_from_db()
        self.club.refresh_from_db()
        self.assertEqual(invoice.club_member, self.kept)
        self.assertEqual((self.club.contact_email_member, self.club.donation_email_member), (self.kept, self.kept))
        self.assertEqual(self.kept.paypal_subscription_id, "I-RENEWS")

    def test_one_participant_per_auction_with_the_kept_members_number(self):
        self.merge_on_the_page()
        rows = AuctionTOS.objects.filter(auction=self.auction, clubmember=self.kept)
        self.assertEqual(
            [(row.bidder_number, row.name, row.user) for row in rows], [("11", "Kept Person", self.person)]
        )

    def test_a_participant_merge_brings_the_members_awards_and_payments_but_not_roles(self):
        BapAward.objects.create(club_member=self.duplicate, date=self.today, points=5)
        payment = InvoicePayment.objects.create(
            club_member=self.duplicate, payment_target="CLUB_MEMBER", amount=Decimal(20), payment_method="cash"
        )
        ClubMember.objects.filter(pk=self.duplicate.pk).update(permission_admin=True)
        kept_row = AuctionTOS.objects.get(auction=self.auction, clubmember=self.kept)
        duplicate_row = AuctionTOS.objects.get(auction=self.auction, clubmember=self.duplicate)

        kept_row.merge_duplicate(duplicate_row)

        self.kept.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((self.kept.bap_points, payment.club_member), (5, self.kept))
        # Same-email merges happen on save, with nobody deciding.
        self.assertFalse(self.kept.permission_admin)

    def test_a_check_in_carried_over_lets_them_bid(self):
        now = timezone.now()
        auction = Auction.objects.create(
            created_by=self.admin,
            title="Check-in auction",
            is_online=False,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=1),
            club=self.club,
            manage_users_through_club="checkin",
        )
        location = PickupLocation.objects.create(name="Door", auction=auction, pickup_time=now)
        kept_row = AuctionTOS.objects.create(
            auction=auction, pickup_location=location, clubmember=self.kept, name="Kept Person", bidding_allowed=False
        )
        checked_in = AuctionTOS.objects.create(
            auction=auction, pickup_location=location, name="At the door", checked_in=now, bidding_allowed=True
        )

        kept_row.merge_duplicate(checked_in)

        kept_row.refresh_from_db()
        self.assertEqual((kept_row.checked_in, kept_row.bidding_allowed), (now, True))

    def test_an_account_merge_keeps_the_later_dues_the_roles_and_who_the_member_carried(self):
        other = User.objects.create_user(username="other_account", password="pw", email="other@example.com")
        theirs = ClubMember.objects.create(
            club=self.club, user=other, name="Other Account", membership_expiration_date=self.days(-10)
        )
        ClubMember.objects.filter(pk=self.duplicate.pk).update(
            membership_expiration_date=self.days(300), permission_manage_bap=True
        )
        household = ClubMember.objects.create(club=self.club, name="Household", membership_carried_by=self.duplicate)

        merge_accounts(self.person, other)

        theirs.refresh_from_db()
        self.assertTrue(theirs.permission_manage_bap)
        household.refresh_from_db()
        self.assertEqual(theirs.membership_expiration_date, self.days(300))
        self.assertEqual(household.membership_carried_by, theirs)
