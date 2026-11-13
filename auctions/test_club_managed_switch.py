"""Switching a copied club auction from copying people to managing them through the club, club-managed
auctions with several pickup locations, and the join card's buttons.
"""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.bidding import PICK_LOCATION_FIRST, check_bidding_permissions
from auctions.forms import AuctionEditForm
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubMember,
    Lot,
    PickupLocation,
)
from auctions.services import join_auction, lot_add_block
from auctions.views.base import _upsert_clubmember_shadow_tos


def make_auction(owner, club, *, is_online=False, **extra):
    now = timezone.now()
    auction = Auction.objects.create(
        created_by=owner,
        title="Spring Swap",
        is_online=is_online,
        club=club,
        date_start=now + datetime.timedelta(days=3),
        date_end=now + datetime.timedelta(days=4),
        **extra,
    )
    auction.save()
    return auction


@override_settings(SINGLE_CLUB_MODE=False)
class ClubManagedSwitchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw")
        self.club = Club.objects.create(name="Fish Club")
        ClubMember.objects.create(club=self.club, user=self.owner, name="Owner", permission_admin=True)
        self.auction = make_auction(self.owner, self.club, copy_users_when_copying_this_auction=True)
        self.location = self.auction.location_qs.first() or PickupLocation.objects.create(
            auction=self.auction, name="Hall", pickup_time=self.auction.date_start
        )
        # As the copy left them: one person the club already has, one it doesn't, one with only a name.
        self.known = ClubMember.objects.create(club=self.club, name="Known Person", email="known@example.com")
        for name, email, number in (
            ("Known Person", "KNOWN@example.com", "7"),
            ("New Person", "new@example.com", "42"),
            ("Paper Only", "", "43"),
        ):
            AuctionTOS.objects.create(
                auction=self.auction,
                pickup_location=self.location,
                name=name,
                email=email,
                bidder_number=number,
                manually_added=True,
            )
        self.client.force_login(self.owner)

    def page(self):
        return self.client.get(self.auction.get_absolute_url()).content.decode()

    def fresh(self):
        return Auction.objects.get(pk=self.auction.pk)

    def test_the_banner_takes_the_place_of_the_checklist(self):
        body = self.page()
        self.assertIn("There's a better way to do this", body)
        self.assertIn('value="switch_to_club_managed"', body)
        self.assertNotIn("Finish setting up", body)

    def test_no_banner_once_anything_has_changed(self):
        for change in ("lot", "join", "history", "dismissed"):
            with self.subTest(change=change):
                if change == "lot":
                    lot = Lot.objects.create(lot_name="Guppies", auction=self.auction, quantity=1)
                elif change == "join":
                    joiner = User.objects.create_user("joiner", "joiner@example.com", "pw")
                    join_auction(joiner, self.auction, self.location)
                elif change == "history":
                    self.auction.create_history(applies_to="USERS", action="Edited someone", user=self.owner)
                else:
                    Auction.objects.filter(pk=self.auction.pk).update(dismissed_club_managed_banner=True)
                self.assertFalse(self.fresh().offers_club_managed_switch)
                if change == "lot":
                    lot.delete()
                elif change == "join":
                    AuctionTOS.objects.filter(user=joiner).delete()
                elif change == "history":
                    AuctionHistory.objects.filter(auction=self.auction, applies_to="USERS").delete()

    def test_no_banner_with_several_locations(self):
        PickupLocation.objects.create(auction=self.auction, name="Second hall", pickup_time=self.auction.date_start)
        self.assertFalse(self.fresh().offers_club_managed_switch)

    def test_no_banner_for_an_admin_without_club_permission(self):
        helper = User.objects.create_user("helper", "helper@example.com", "pw")
        AuctionTOS.objects.filter(email="new@example.com").update(user=helper, is_admin=True)
        self.client.force_login(helper)
        self.assertNotIn("There's a better way to do this", self.page())
        response = self.client.post(
            reverse("auction_page_action", kwargs={"slug": self.auction.slug}), {"action": "switch_to_club_managed"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.fresh().manage_users_through_club)

    def test_enable_now_adds_everyone_to_the_club_then_turns_it_on(self):
        self.client.post(
            reverse("auction_page_action", kwargs={"slug": self.auction.slug}), {"action": "switch_to_club_managed"}
        )
        auction = self.fresh()
        self.assertEqual(auction.manage_users_through_club, "all")
        self.assertFalse(auction.copy_users_when_copying_this_auction)
        # Matched by email, so not added twice.
        self.assertEqual(ClubMember.objects.filter(club=self.club, email__iexact="known@example.com").count(), 1)
        new = ClubMember.objects.get(club=self.club, email="new@example.com")
        self.assertEqual(new.bidder_number, "42")
        self.assertTrue(ClubMember.objects.filter(club=self.club, name="Paper Only").exists())
        # Everyone in the auction is now somebody's club record.
        self.assertFalse(AuctionTOS.objects.filter(auction=auction, clubmember__isnull=True).exists())
        self.assertEqual(
            AuctionTOS.objects.filter(auction=auction).count(),
            ClubMember.objects.filter(club=self.club, is_deleted=False).count(),
        )
        self.assertNotIn("There's a better way to do this", self.page())

    def test_helpers_stay_admins_and_namesakes_stay_two_people(self):
        AuctionTOS.objects.filter(email="new@example.com").update(is_admin=True)
        AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, name="Paper Only", bidder_number="44"
        )
        url = reverse("auction_page_action", kwargs={"slug": self.auction.slug})
        self.client.post(url, {"action": "switch_to_club_managed"})
        new = ClubMember.objects.get(club=self.club, email="new@example.com")
        self.assertTrue(AuctionTOS.objects.get(auction=self.auction, clubmember=new).is_admin)
        self.assertEqual(ClubMember.objects.filter(club=self.club, name="Paper Only").count(), 2)
        # A second press (a double click, another admin's stale tab) is not an error.
        self.assertEqual(self.client.post(url, {"action": "switch_to_club_managed"}).status_code, 302)

    def test_the_sites_own_history_lines_are_not_edits(self):
        self.auction.create_history(applies_to="USERS", action="Automatically added an admin", user=None)
        self.assertTrue(self.fresh().offers_club_managed_switch)

    def test_dont_show_again(self):
        self.client.post(
            reverse("auction_page_action", kwargs={"slug": self.auction.slug}),
            {"action": "dismiss_club_managed_banner"},
        )
        self.assertTrue(self.fresh().dismissed_club_managed_banner)
        self.assertFalse(self.fresh().manage_users_through_club)


@override_settings(SINGLE_CLUB_MODE=False)
class SeveralLocationsTests(TestCase):
    """A club-managed online auction with two locations: the club's rows wait for a location pick."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw")
        self.member_user = User.objects.create_user("member", "member@example.com", "pw")
        self.club = Club.objects.create(name="Fish Club")
        ClubMember.objects.create(club=self.club, user=self.owner, name="Owner", permission_admin=True)
        self.member = ClubMember.objects.create(
            club=self.club, user=self.member_user, name="Member", email="member@example.com"
        )
        self.auction = make_auction(self.owner, self.club, is_online=True)
        self.north = PickupLocation.objects.create(
            auction=self.auction, name="North", pickup_time=self.auction.date_end
        )
        self.south = PickupLocation.objects.create(
            auction=self.auction, name="South", pickup_time=self.auction.date_end
        )
        Auction.objects.filter(pk=self.auction.pk).update(manage_users_through_club="all")
        self.auction = Auction.objects.get(pk=self.auction.pk)
        AuctionEditForm._rebuild_auctiontos_from_club(self.auction)
        self.tos = AuctionTOS.objects.get(auction=self.auction, clubmember=self.member)

    def test_the_club_row_has_to_pick_a_location_before_bidding_or_selling(self):
        self.assertTrue(self.tos.needs_pickup_location)
        self.assertFalse(self.tos.can_bid_in_auction)
        self.assertFalse(self.tos.can_sell)
        lot = Lot.objects.create(lot_name="Guppies", auction=self.auction, quantity=1, user=self.owner)
        self.assertEqual(check_bidding_permissions(lot, self.member_user), PICK_LOCATION_FIRST)
        self.assertIsNotNone(lot_add_block(self.auction, self.tos, False))

    def test_the_auction_page_asks_them_to_join(self):
        self.client.force_login(self.member_user)
        body = self.client.get(self.auction.get_absolute_url()).content.decode()
        self.assertIn('value="Join auction"', body)
        self.assertNotIn("leave-auction-modal", body)

    def test_joining_picks_the_location_and_lets_them_bid(self):
        join_auction(self.member_user, self.auction, self.south)
        self.tos.refresh_from_db()
        self.assertEqual(self.tos.pickup_location, self.south)
        self.assertFalse(self.tos.needs_pickup_location)
        self.assertTrue(self.tos.can_bid_in_auction)

    def test_the_club_keeps_a_picked_location(self):
        join_auction(self.member_user, self.auction, self.south)
        _upsert_clubmember_shadow_tos(self.auction, self.member)
        self.tos.refresh_from_db()
        self.assertEqual(self.tos.pickup_location, self.south)

    def test_a_check_in_or_a_single_location_auction_needs_no_pick(self):
        other = ClubMember.objects.create(club=self.club, name="Door", email="door@example.com")
        tos = _upsert_clubmember_shadow_tos(self.auction, other, checked_in_at=timezone.now())
        self.assertFalse(tos.needs_pickup_location)
        self.south.delete()
        single = Auction.objects.get(pk=self.auction.pk)
        late = ClubMember.objects.create(club=self.club, name="Late", email="late@example.com")
        self.assertTrue(AuctionTOS.objects.get(auction=single, clubmember=late).pickup_location_confirmed)

    def test_a_single_location_never_waits(self):
        self.south.delete()
        tos = AuctionTOS.objects.get(pk=self.tos.pk)
        self.assertFalse(tos.needs_pickup_location)


@override_settings(SINGLE_CLUB_MODE=False)
class JoinCardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw")
        self.user = User.objects.create_user("bidder", "bidder@example.com", "pw")
        self.auction = make_auction(self.owner, None)
        self.location = self.auction.location_qs.first()
        self.client.force_login(self.user)

    def page(self):
        return self.client.get(self.auction.get_absolute_url()).content.decode()

    def test_join_then_view_lots(self):
        self.assertIn('value="Join auction"', self.page())
        join_auction(self.user, self.auction, self.location)
        body = self.page()
        self.assertIn('value="View lots"', body)
        self.assertIn("</i> Leave</button>", body)

    def test_in_person_pickup_reads_time(self):
        body = self.page()
        self.assertIn("<strong>Time:</strong>", body)
        self.assertNotIn("<strong>Pickup:</strong>", body)
