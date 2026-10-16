"""The X next to Join: undoing your own join, and the cases that send you to the organizer."""

import datetime

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import palette_actions
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Bid,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
)
from auctions.services import join_auction


class LeaveAuctionTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.organizer = User.objects.create_user(username="organizer", password="x", email="org@example.com")
        self.leaver = User.objects.create_user(username="leaver", password="x", email="leaver@example.com")
        self.auction = Auction.objects.create(
            created_by=self.organizer,
            title="Fall Fish Auction",
            is_online=False,
            date_start=now + datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=3, hours=6),
        )
        # In-person auctions get their pickup location from a signal; re-save to materialise it.
        self.auction.save()
        self.location = self.auction.location_qs.exclude(pickup_by_mail=True).first()
        self.tos, _created, _problem = join_auction(self.leaver, self.auction, self.location)
        self.client.force_login(self.leaver)

    def _page(self):
        return self.client.get(self.auction.get_absolute_url()).content.decode()

    def _leave(self):
        return self.client.post(reverse("auction_leave", kwargs={"slug": self.auction.slug}))

    def _still_in(self):
        return AuctionTOS.objects.filter(pk=self.tos.pk).exists()

    def test_your_own_join_can_be_undone(self):
        page = self._page()
        self.assertIn('data-bs-target="#leave-auction-modal"', page)
        self.assertIn("Yes, leave", page)
        response = self._leave()
        self.assertRedirects(response, self.auction.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(self._still_in())
        self.assertTrue(AuctionHistory.objects.filter(auction=self.auction, action__icontains="has left").exists())

    def test_no_x_until_you_have_joined(self):
        self.client.force_login(User.objects.create_user(username="stranger", password="x"))
        self.assertNotIn("leave-auction-modal", self._page())

    def test_an_empty_invoice_goes_with_you(self):
        Invoice.objects.create(auction=self.auction, auctiontos_user=self.tos)
        self._leave()
        self.assertFalse(self._still_in())
        self.assertFalse(Invoice.objects.filter(auction=self.auction).exists())

    def test_added_by_an_organizer_points_at_the_organizer(self):
        AuctionTOS.objects.filter(pk=self.tos.pk).update(manually_added=True)
        page = self._page()
        self.assertIn("An organizer added you", page)
        self.assertIn("mailto:org@example.com", page)
        self.assertNotIn("Yes, leave", page)
        self._leave()
        self.assertTrue(self._still_in())

    def assertActivityBlocks(self):
        page = self._page()
        self.assertIn("ask an organizer to take you out", page)
        self.assertIn("mailto:org@example.com", page)
        self._leave()
        self.assertTrue(self._still_in())

    def test_a_bid_blocks(self):
        seller = AuctionTOS.objects.create(auction=self.auction, pickup_location=self.location, name="Seller")
        lot = Lot.objects.create(lot_name="Guppies", auction=self.auction, auctiontos_seller=seller, quantity=1)
        Bid.objects.create(user=self.leaver, lot_number=lot, amount=5)
        self.assertActivityBlocks()

    def test_a_lot_you_are_selling_blocks(self):
        Lot.objects.create(lot_name="Plants", auction=self.auction, auctiontos_seller=self.tos, quantity=1)
        self.assertActivityBlocks()

    def test_a_lot_you_deleted_does_not_block(self):
        Lot.objects.create(
            lot_name="Plants", auction=self.auction, auctiontos_seller=self.tos, quantity=1, is_deleted=True
        )
        self._leave()
        self.assertFalse(self._still_in())

    def test_a_won_lot_blocks(self):
        seller = AuctionTOS.objects.create(auction=self.auction, pickup_location=self.location, name="Seller")
        Lot.objects.create(
            lot_name="Snails",
            auction=self.auction,
            auctiontos_seller=seller,
            auctiontos_winner=self.tos,
            winning_price=4,
            quantity=1,
        )
        self.assertActivityBlocks()

    def test_an_invoice_adjustment_blocks(self):
        invoice = Invoice.objects.create(auction=self.auction, auctiontos_user=self.tos)
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="ADD", amount=3, notes="Table fee")
        self.assertActivityBlocks()

    def test_being_checked_in_blocks(self):
        AuctionTOS.objects.filter(pk=self.tos.pk).update(checked_in=timezone.now())
        self.assertActivityBlocks()

    def test_a_paid_club_membership_is_warned_about(self):
        club = Club.objects.create(name="Fish Club")
        ClubMember.objects.create(club=club, user=self.organizer, permission_admin=True)
        self.auction.club = club
        self.auction.manage_users_through_club = "all"
        self.auction.save()
        AuctionTOS.objects.filter(pk=self.tos.pk).delete()
        self.tos, _created, _problem = join_auction(self.leaver, self.auction, self.location)
        self.assertIn("Yes, leave", self._page())
        self.assertNotIn("paid Fish Club membership", self._page())
        ClubMember.objects.filter(pk=self.tos.clubmember_id).update(
            membership_expiration_date=timezone.localdate() + datetime.timedelta(days=30)
        )
        self.assertIn("paid Fish Club membership", self._page())
        self._leave()
        self.assertFalse(self._still_in())
        self.assertTrue(ClubMember.objects.filter(pk=self.tos.clubmember_id).exists())

    def _run(self, params):
        request = RequestFactory().post("/")
        request.user = self.leaver
        request.palette_page = {}
        return palette_actions.run_action(request, "leave_auction", params)

    def test_the_assistant_leaves_the_same_way(self):
        result = self._run({"auction": self.auction.slug})
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(self._still_in())

    def test_the_assistant_is_refused_the_same_way(self):
        AuctionTOS.objects.filter(pk=self.tos.pk).update(manually_added=True)
        result = self._run({"auction": self.auction.slug})
        self.assertIn("organizer", result.get("error", ""), result)
        self.assertTrue(self._still_in())
