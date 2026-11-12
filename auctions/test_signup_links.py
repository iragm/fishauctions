"""Sign-in links, the one-tap claim on an invoice, and the shortcuts on the selling, buying, lots and new-lot pages."""

import datetime

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from auctions.models import Auction, AuctionTOS, Club, Invoice, PickupLocation
from auctions.tests import StandardTestCase

GOOGLE = {"GOOGLE_OAUTH_LINK": "1234-real.apps.googleusercontent.com"}


@override_settings(**GOOGLE)
class SocialButtonNextTests(TestCase):
    def test_no_next_means_no_next_none(self):
        for name in ("account_signup", "account_login"):
            body = self.client.get(reverse(name)).content.decode()
            self.assertIn('href="/google/login/?process=login"', body)
            self.assertNotIn("next=None", body)

    def test_next_is_encoded(self):
        body = self.client.get(reverse("account_signup"), {"next": "/lots/?a=1&b=2"}).content.decode()
        self.assertIn("process=login&next=/lots/%3Fa%3D1%26b%3D2", body)

    def test_navbar_sign_in_links_are_nofollow(self):
        body = self.client.get(reverse("allLots")).content.decode()
        self.assertIn("class='nav-link' rel='nofollow'>Create account", body)
        self.assertIn("class='nav-link' rel='nofollow'>Sign in", body)


@override_settings(**GOOGLE)
class InvoiceClaimTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.added = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Added by the club",
            email="added@example.com",
            manually_added=True,
            bidder_number="777",
        )
        self.invoice = Invoice.for_participant(self.added, self.online_auction)
        self.url = reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link})

    def test_signed_out_club_added_person_gets_the_buttons(self):
        body = self.client.get(self.url).content.decode()
        self.assertIn('id="invoice-claim-google"', body)
        self.assertIn(f"next={self.url}", body)

    def test_not_for_someone_with_an_account(self):
        AuctionTOS.objects.filter(pk=self.added.pk).update(user=self.user_who_does_not_join)
        self.assertNotContains(self.client.get(self.url), "invoice-claim")

    def test_not_when_signed_in(self):
        self.client.login(username="no_tos", password="testpassword")
        self.assertNotContains(self.client.get(self.url), "invoice-claim")

    def test_not_in_the_app(self):
        self.assertNotContains(self.client.get(self.url, HTTP_USER_AGENT="FishAuctionsApp/1.0"), "invoice-claim")


class _OpenAuctionMixin:
    def open_auction(self, title="Coming up", locations=1):
        now = timezone.now()
        auction = Auction.objects.create(
            created_by=self.admin_user,
            title=title,
            is_online=False,
            date_start=now + datetime.timedelta(days=5),
            date_end=now + datetime.timedelta(days=6),
            lot_submission_start_date=now - datetime.timedelta(days=1),
            lot_submission_end_date=now + datetime.timedelta(days=4),
        )
        for number in range(locations):
            PickupLocation.objects.create(
                name=f"place {number}", auction=auction, pickup_time=now + datetime.timedelta(days=5)
            )
        return auction


class SellingAddLotTests(_OpenAuctionMixin, StandardTestCase):
    def test_add_lot_aims_at_the_auction_joined_last(self):
        auction = self.open_auction()
        AuctionTOS.objects.create(
            user=self.user, auction=auction, pickup_location=auction.location_qs.first(), bidder_number="901"
        )
        self.client.login(username="my_lot", password="testpassword")
        self.assertContains(self.client.get(reverse("selling")), f'href="{auction.add_lot_link}"')

    def test_plain_add_lot_without_an_open_auction(self):
        self.client.login(username="no_joins", password="testpassword")
        self.assertContains(self.client.get(reverse("selling")), f'href="{reverse("new_lot")}"')


class EndedAuctionLinksCurrentTests(_OpenAuctionMixin, StandardTestCase):
    def test_ended_auction_links_the_clubs_current_one(self):
        current = self.open_auction("Next club auction")
        club = Club.objects.create(name="Lots club", current_auction=current)
        Auction.objects.filter(pk=self.online_auction.pk).update(club=club)
        self.assertContains(self.client.get(self.online_auction.view_lot_link), escape(current.view_lot_link))

    def test_not_when_the_current_one_is_this_one(self):
        club = Club.objects.create(name="Lots club", current_auction=self.online_auction)
        Auction.objects.filter(pk=self.online_auction.pk).update(club=club)
        self.assertIsNone(self.client.get(self.online_auction.view_lot_link).context["club_current_auction"])


class NewLotJoinsSilentlyTests(_OpenAuctionMixin, StandardTestCase):
    def setUp(self):
        super().setUp()
        self.seller = self.user_who_does_not_join
        self.seller.first_name, self.seller.last_name = "New", "Seller"
        self.seller.save()
        self.seller.userdata.address = "1 Fish St"
        self.seller.userdata.save()
        self.client.login(username="no_joins", password="testpassword")

    def joined(self, auction):
        return AuctionTOS.objects.filter(user=self.seller, auction=auction).exists()

    def test_add_lot_from_the_site_joins_and_skips_the_question(self):
        auction = self.open_auction()
        response = self.client.get(auction.add_lot_link, HTTP_SEC_FETCH_SITE="same-origin")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.joined(auction))
        self.assertTrue(response.context["auction_chosen"])
        self.assertFalse(response.context["show_no_auction_modal"])

    def test_same_site_referer_counts(self):
        auction = self.open_auction()
        self.client.get(auction.add_lot_link, HTTP_REFERER=f"http://testserver{auction.get_absolute_url()}")
        self.assertTrue(self.joined(auction))

    def test_a_link_from_elsewhere_never_joins(self):
        auction = self.open_auction()
        for headers in ({}, {"HTTP_SEC_FETCH_SITE": "cross-site", "HTTP_REFERER": "https://evil.example/"}):
            response = self.client.get(auction.add_lot_link, **headers)
            self.assertRedirects(response, auction.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(self.joined(auction))

    def test_a_choice_of_pickup_location_still_goes_to_join(self):
        auction = self.open_auction(locations=2)
        response = self.client.get(auction.add_lot_link, HTTP_SEC_FETCH_SITE="same-origin")
        self.assertRedirects(response, auction.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(self.joined(auction))

    def test_plain_new_lot_still_asks(self):
        response = self.client.get(reverse("new_lot"))
        self.assertFalse(response.context["auction_chosen"])
