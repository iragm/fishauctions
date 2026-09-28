"""The buying dashboard: which lots it lists, the status badge, its keywords, and the one note above it."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Bid, Invoice, Lot, PickupLocation, Watch
from auctions.tests import StandardTestCase


class BuyingDashboardTests(StandardTestCase):
    """``self.userB`` (tosB) won lot, lotB and lotC in the ended online auction."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.userB)
        self.url = reverse("buying") + f"?auction={self.online_auction.slug}"

    def _lot(self, name, auction=None, **kwargs):
        auction = auction or self.online_auction
        seller = self.online_tos if auction == self.online_auction else self.admin_in_person_tos
        return Lot.objects.create(lot_name=name, auction=auction, auctiontos_seller=seller, quantity=1, **kwargs)

    def _badge(self, lot):
        html = self.client.get(self.url, {"query": lot.lot_name}).content.decode()
        return html.split("<tbody")[1].split("</td>")[0]

    def test_old_pages_redirect_to_the_filtered_dashboard(self):
        for name, keyword in (("watched", "watched"), ("won_lots", "won"), ("my_bids", "bids")):
            with self.subTest(page=name):
                response = self.client.get(reverse(name) + "?src=email")
                self.assertRedirects(
                    response, reverse("buying") + f"?src=email&query={keyword}", fetch_redirect_response=False
                )

    def test_filter_param_becomes_the_search_box(self):
        response = self.client.get(reverse("buying") + "?filter=watched")
        self.assertRedirects(response, reverse("buying") + "?query=watched", fetch_redirect_response=False)

    def test_anonymous_is_sent_to_log_in(self):
        self.client.logout()
        response = self.client.get(reverse("buying"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_lists_watched_bid_and_won_lots_in_this_auction_only(self):
        watched = self._lot("Watched duckweed", active=True)
        Watch.objects.create(user=self.userB, lot_number=watched)
        bid_on = self._lot("Bid on guppy", active=True)
        Bid.objects.create(user=self.userB, lot_number=bid_on, amount=5)
        self._lot("Nothing to do with me", active=True)
        elsewhere = self._lot("Other auction lot", auction=self.in_person_auction)
        Watch.objects.create(user=self.userB, lot_number=elsewhere)
        response = self.client.get(self.url)
        for name in ("Watched duckweed", "Bid on guppy", "A test lot", "B test lot"):
            self.assertContains(response, name)
        self.assertNotContains(response, "Nothing to do with me")
        self.assertNotContains(response, "Other auction lot")
        self.assertContains(response, f"{watched.lot_link}?src=buying")

    def test_keywords_combine_with_text(self):
        watched = self._lot("Watched duckweed", active=True)
        Watch.objects.create(user=self.userB, lot_number=watched)
        Watch.objects.create(user=self.userB, lot_number=self.lot)
        won = self.client.get(self.url, {"query": "won"}, HTTP_HX_REQUEST="true")
        self.assertContains(won, "A test lot")
        self.assertNotContains(won, "Watched duckweed")
        watched_duckweed = self.client.get(self.url, {"query": "duckweed watched"}, HTTP_HX_REQUEST="true")
        self.assertContains(watched_duckweed, "Watched duckweed")
        self.assertNotContains(watched_duckweed, "A test lot")

    def test_one_badge_for_where_you_stand(self):
        lost = self._lot("Someone else got it", winning_price=10, auctiontos_winner=self.tosC, active=False)
        Watch.objects.create(user=self.userB, lot_number=lost)
        watched = self._lot("Just watching", active=True)
        Watch.objects.create(user=self.userB, lot_number=watched)
        self.assertIn(">Won<", self._badge(self.lot))
        self.assertIn(">Lost<", self._badge(lost))
        self.assertIn(">Watched<", self._badge(watched))

    def test_outbid_while_bidding_is_open(self):
        now = timezone.now()
        self.online_auction.date_end = now + datetime.timedelta(days=1)
        self.online_auction.save()
        lot = self._lot("Open lot", active=True)
        Bid.objects.create(user=self.userB, lot_number=lot, amount=5)
        self.assertIn(">Bid<", self._badge(lot))
        Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=50)
        self.assertIn(">Outbid<", self._badge(lot))

    def test_defaults_to_the_last_auction_used(self):
        watched = self._lot("In person watched", auction=self.in_person_auction)
        Watch.objects.create(user=self.userB, lot_number=watched)
        self.userB.userdata.last_auction_used = self.in_person_auction
        self.userB.userdata.save()
        response = self.client.get(reverse("buying"))
        self.assertEqual(response.context["auction"], self.in_person_auction)
        self.assertContains(response, "In person watched")
        self.assertNotContains(response, "A test lot")

    def test_auction_dropdown_lists_joined_auctions(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["recent_auctions"], [self.online_auction])
        self.assertContains(response, f'data-buying-auction="{self.online_auction.slug}"')

    def test_no_auction_points_at_the_auction_list(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(reverse("buying"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["auction"])
        self.assertContains(response, f'href="{reverse("auctions")}"')

    def test_unpaid_invoice_after_an_online_auction_ends(self):
        invoice = Invoice.objects.get(auctiontos_user=self.tosB)
        self.assertLess(invoice.rounded_net_after_payments, 0)
        self.assertEqual(self.client.get(self.url).context["note"], "unpaid")
        invoice.status = "PAID"
        invoice.save()
        self.assertIsNone(self.client.get(self.url).context["note"])

    def test_pretty_much_over(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(reverse("buying") + f"?auction={self.in_person_auction.slug}")
        self.assertEqual(response.context["note"], "ended")
        self.assertContains(response, "has ended")


class BuyingDashboardPushNoteTests(StandardTestCase):
    """An in-person auction still running that pushes lots as they sell."""

    def setUp(self):
        super().setUp()
        start = timezone.now() + datetime.timedelta(days=1)
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Push auction",
            is_online=False,
            date_start=start,
            message_users_when_lots_sell=True,
        )
        location = PickupLocation.objects.create(name="here", auction=self.auction, pickup_time=start)
        self.buyer = User.objects.create_user(username="push_buyer", password="x", email="push@example.com")
        AuctionTOS.objects.create(user=self.buyer, auction=self.auction, pickup_location=location)
        self.client.force_login(self.buyer)
        self.url = reverse("buying") + f"?auction={self.auction.slug}"

    def test_offers_browser_push(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["note"], "push_offer")
        self.assertContains(response, "webpush-subscribe-button")

    def test_no_browser_push_offer_in_the_app(self):
        response = self.client.get(self.url, HTTP_USER_AGENT="FishAuctionsApp/1.0 (iOS)")
        self.assertIsNone(response.context["note"])
        self.assertNotContains(response, "webpush-subscribe-button")

    def test_app_push_on(self):
        self.buyer.userdata.push_notifications_when_lots_sell = True
        self.buyer.userdata.save()
        with patch("auctions.views.browse.user_has_app_push", return_value=True):
            response = self.client.get(self.url)
        self.assertEqual(response.context["note"], "push_in_app")
        self.assertContains(response, "notification in the app")

    def test_no_push_note_when_the_auction_does_not_push(self):
        self.auction.message_users_when_lots_sell = False
        self.auction.save()
        self.assertIsNone(self.client.get(self.url).context["note"])
