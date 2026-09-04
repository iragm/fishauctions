"""Money into and out of a club: PayPal without OAuth, invoices, profit and seller splits."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Invoice,
    Lot,
    PickupLocation,
)
from auctions.tests import StandardTestCase


class NonOAuthPayPalTests(TestCase):
    """Club-supplied (non-OAuth) PayPal credentials.

    When an admin sets ``Club.allow_non_oauth_paypal``, the club enters its own PayPal REST
    API client ID/secret and they're used exactly like the site's PAYPAL_CLIENT_ID/SECRET:
    payments go straight to that PayPal account with no payee override or platform fee.
    """

    def setUp(self):
        self.client = Client()
        self.member_user = User.objects.create_user(username="nonoauth_member", password="pw", email="m@example.com")
        self.money_user = User.objects.create_user(username="nonoauth_money", password="pw", email="money@example.com")
        self.club = Club.objects.create(
            name="BYO PayPal Club",
            enable_membership=True,
            membership_annual_fee=Decimal("30.00"),
            membership_system="rolling",
        )
        ClubMember.objects.create(club=self.club, user=self.member_user, name="M", email="m@example.com")
        ClubMember.objects.create(club=self.club, user=self.money_user, permission_money=True)

    def _enable_credentials(self):
        self.club.allow_non_oauth_paypal = True
        self.club.paypal_client_id = "club-client-id"
        self.club.paypal_secret = "club-secret"
        self.club.save()

    def _make_club_invoice(self):
        return Invoice.objects.create(club=self.club, buyer=self.member_user, status="UNPAID", renewal_needed=True)

    # -- model properties ------------------------------------------------------

    def test_uses_own_credentials_requires_flag_and_both_values(self):
        self.club.paypal_client_id = "c"
        self.club.paypal_secret = "s"
        self.club.save()
        self.assertFalse(self.club.uses_own_paypal_credentials)  # flag still off
        self.club.allow_non_oauth_paypal = True
        self.club.save()
        self.assertTrue(self.club.uses_own_paypal_credentials)
        self.assertEqual(self.club.paypal_credentials, ("c", "s"))

    def test_uses_own_credentials_false_when_value_missing(self):
        self.club.allow_non_oauth_paypal = True
        self.club.paypal_client_id = "c"
        self.club.paypal_secret = ""
        self.club.save()
        self.assertFalse(self.club.uses_own_paypal_credentials)
        self.assertIsNone(self.club.paypal_credentials)

    def test_can_accept_paypal_via_own_credentials(self):
        self.assertFalse(self.club.can_accept_paypal)
        self._enable_credentials()
        self.assertTrue(self.club.can_accept_paypal)

    # -- invoice credential resolution + buttons -------------------------------

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_SECRET="")
    def test_membership_invoice_button_without_site_keys(self):
        self._enable_credentials()
        invoice = self._make_club_invoice()
        self.assertEqual(invoice.paypal_credentials, ("club-client-id", "club-secret"))
        self.assertTrue(invoice.show_paypal_button)
        self.assertTrue(invoice.show_payment_button)

    @override_settings(PAYPAL_CLIENT_ID="", PAYPAL_SECRET="")
    def test_no_button_when_flag_off(self):
        self.club.paypal_client_id = "c"
        self.club.paypal_secret = "s"  # flag still off => credentials not active
        self.club.save()
        invoice = self._make_club_invoice()
        self.assertIsNone(invoice.paypal_credentials)
        self.assertFalse(invoice.show_paypal_button)

    def test_auction_invoice_resolves_club_credentials_with_no_payee(self):
        self._enable_credentials()
        auction = Auction.objects.create(
            created_by=self.money_user,
            club=self.club,
            title="BYO Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        invoice = Invoice(auction=auction)
        self.assertEqual(invoice.paypal_credentials, ("club-client-id", "club-secret"))
        # No linked seller and not the site account => no payee merchant id, so the money
        # goes straight to the club's own PayPal account (same as the site keys do).
        self.assertIsNone(auction.paypal_information)

    # -- mixin auth resolution -------------------------------------------------

    @override_settings(PAYPAL_CLIENT_ID="site-id", PAYPAL_SECRET="site-secret")
    def test_paypal_auth_prefers_club_credentials(self):
        from auctions.views import PayPalAPIMixin

        mixin = PayPalAPIMixin()
        self.assertEqual(mixin._paypal_auth(), ("site-id", "site-secret"))
        mixin.club_paypal_credentials = ("club-client-id", "club-secret")
        self.assertEqual(mixin._paypal_auth(), ("club-client-id", "club-secret"))

    # -- credentials view ------------------------------------------------------

    def test_credentials_view_requires_flag(self):
        self.client.login(username="nonoauth_money", password="pw")  # has permission_money
        url = reverse("club_paypal_credentials", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"paypal_client_id": "x", "paypal_secret": "y"})
        self.assertEqual(response.status_code, 403)

    def test_credentials_view_requires_permission(self):
        self.club.allow_non_oauth_paypal = True
        self.club.save()
        self.client.login(username="nonoauth_member", password="pw")  # no money permission
        url = reverse("club_paypal_credentials", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"paypal_client_id": "x", "paypal_secret": "y"})
        self.assertEqual(response.status_code, 403)

    def test_credentials_view_saves(self):
        self.club.allow_non_oauth_paypal = True
        self.club.save()
        self.client.login(username="nonoauth_money", password="pw")
        url = reverse("club_paypal_credentials", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"paypal_client_id": "abc", "paypal_secret": "shh"})
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.paypal_client_id, "abc")
        self.assertEqual(self.club.paypal_secret, "shh")
        self.assertTrue(self.club.uses_own_paypal_credentials)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, applies_to="SETTINGS").exists())

    def test_credentials_view_blank_secret_keeps_existing(self):
        self._enable_credentials()
        self.client.login(username="nonoauth_money", password="pw")
        url = reverse("club_paypal_credentials", kwargs={"slug": self.club.slug})
        response = self.client.post(url, {"paypal_client_id": "new-client", "paypal_secret": ""})
        self.assertEqual(response.status_code, 302)
        self.club.refresh_from_db()
        self.assertEqual(self.club.paypal_client_id, "new-client")
        self.assertEqual(self.club.paypal_secret, "club-secret")  # secret unchanged

    # -- settings page UI ------------------------------------------------------

    @override_settings(PAYPAL_CLIENT_ID="site-id", PAYPAL_SECRET="site-secret")
    def test_settings_page_hides_oauth_shows_credentials_form(self):
        self.club.allow_non_oauth_paypal = True
        self.club.save()
        self.client.login(username="nonoauth_money", password="pw")
        url = reverse("club_membership_settings", kwargs={"slug": self.club.slug})
        content = self.client.get(url).content.decode()
        self.assertIn(reverse("club_paypal_credentials", kwargs={"slug": self.club.slug}), content)
        self.assertNotIn("Connect a PayPal account for this club", content)

    @override_settings(PAYPAL_CLIENT_ID="site-id", PAYPAL_SECRET="site-secret")
    def test_settings_page_shows_oauth_when_flag_off(self):
        # The OAuth "Connect" button is gated behind the user's paypal_enabled flag, which
        # defaults to PAYPAL_ENABLED_FOR_USERS (False). Enable it so the button can render.
        self.money_user.userdata.paypal_enabled = True
        self.money_user.userdata.save(update_fields=["paypal_enabled"])
        self.client.login(username="nonoauth_money", password="pw")
        url = reverse("club_membership_settings", kwargs={"slug": self.club.slug})
        content = self.client.get(url).content.decode()
        self.assertNotIn(reverse("club_paypal_credentials", kwargs={"slug": self.club.slug}), content)
        self.assertIn("Connect a PayPal account for this club", content)

    # -- refunds (forced manual for non-OAuth clubs) ---------------------------

    def test_refund_invoice_refuses_for_non_oauth_club(self):
        from auctions.views import PayPalAPIMixin

        self._enable_credentials()
        invoice = self._make_club_invoice()
        mixin = PayPalAPIMixin()
        # Returns a manual-refund message and never touches the PayPal API (no webhook
        # means an automated refund would go unrecorded).
        result = mixin.refund_invoice(invoice, Decimal("5.00"))
        self.assertIn("manually", result.lower())

    # -- in-person quick checkout (PayPal QR disabled for non-OAuth) -----------

    @override_settings(PAYPAL_CLIENT_ID="site-id", PAYPAL_SECRET="site-secret")
    def test_quick_checkout_suppresses_paypal_qr_for_non_oauth_club(self):
        self._enable_credentials()
        admin = User.objects.create_user(username="nonoauth_admin", password="pw", email="a@example.com")
        ClubMember.objects.create(club=self.club, user=admin, permission_admin=True)
        auction = Auction.objects.create(
            created_by=admin,
            club=self.club,
            title="In-person BYO Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=2),
            date_end=timezone.now() - datetime.timedelta(days=1),
            tax=0,
        )
        location = PickupLocation.objects.create(
            name="loc", auction=auction, pickup_time=timezone.now() + datetime.timedelta(days=1)
        )
        seller_tos = AuctionTOS.objects.create(
            user=admin, auction=auction, pickup_location=location, bidder_number="100", is_admin=True
        )
        buyer_tos = AuctionTOS.objects.create(
            user=self.member_user, auction=auction, pickup_location=location, bidder_number="777"
        )
        Lot.objects.create(
            lot_name="Sold lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            auctiontos_winner=buyer_tos,
            winning_price=10,
            quantity=1,
            active=False,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=buyer_tos)
        # The button itself is available and the club's own credentials resolve -- so the QR is
        # suppressed by the non-OAuth gate, not because PayPal is unavailable.
        self.assertTrue(invoice.show_paypal_button)
        self.assertIsNotNone(invoice.paypal_credentials)

        self.client.force_login(admin)
        url = reverse("auction_quick_checkout_htmx", kwargs={"slug": auction.slug, "filter": "777"})
        content = self.client.get(url).content.decode()
        self.assertNotIn("Scan this code to pay with PayPal", content)

    # -- PayPal bulk-invoice CSV export disabled when PayPal payments are live --

    def test_paypal_payments_enabled_for_non_oauth_club_auction(self):
        auction = Auction.objects.create(
            created_by=self.money_user,
            club=self.club,
            title="CSV Gate Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=2),
            date_end=timezone.now() - datetime.timedelta(days=1),
            enable_online_payments=False,  # club config supersedes this for club auctions
        )
        # Off by default => the per-auction flag (False) decides, so the CSV export stays available.
        self.assertFalse(auction.paypal_payments_enabled)
        # Turning on non-OAuth PayPal means buyers can pay directly => hide the manual CSV export.
        self._enable_credentials()
        auction.refresh_from_db()
        self.assertTrue(auction.paypal_payments_enabled)


class ClubMoneyInvoiceHistoryTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Ledger Club", enable_membership=True, membership_annual_fee=Decimal("15.00")
        )
        self.online_auction.club = self.club
        self.online_auction.save(update_fields=["club"])
        ClubMoney.objects.all().delete()

    def test_marking_seller_invoice_paid_books_payout_not_receivables(self):
        # self.invoice's user sold lots, so paying it books a seller payout. The old
        # receivable/profit categories are gone (commission is computed, not stored).
        self.invoice.status = "PAID"
        self.invoice.save(update_fields=["status"])
        categories = set(ClubMoney.objects.filter(invoice=self.invoice).values_list("category", flat=True))
        self.assertIn(ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT, categories)
        self.assertNotIn("unpaid_invoices", categories)
        self.assertNotIn("auction_profit", categories)

    def test_paid_unpaid_paid_nets_to_zero(self):
        self.invoice.status = "PAID"
        self.invoice.save(update_fields=["status"])
        self.invoice.status = "DRAFT"
        self.invoice.save(update_fields=["status"])
        total = sum(ClubMoney.objects.filter(invoice=self.invoice).values_list("amount", flat=True), Decimal("0.00"))
        self.assertEqual(total, Decimal("0.00"))

    def test_paid_buyer_invoice_books_sale_tax_and_membership(self):
        self.invoiceB.renewal_needed = True
        self.invoiceB.save(update_fields=["renewal_needed"])
        self.invoiceB.status = "PAID"
        self.invoiceB.save(update_fields=["status"])
        categories = set(ClubMoney.objects.filter(invoice=self.invoiceB).values_list("category", flat=True))
        # tosB bought lots in a 25%-tax auction and needs to renew.
        self.assertIn(ClubMoney.CATEGORY_AUCTION_SALE, categories)
        self.assertIn(ClubMoney.CATEGORY_TAX, categories)
        self.assertIn(ClubMoney.CATEGORY_MEMBERSHIP, categories)

    def test_setting_auction_club_backfills_paid_invoice_history(self):
        invoice = Invoice.objects.get_or_create(auctiontos_user=self.admin_in_person_tos)[0]
        self.in_person_lot.winning_price = Decimal("20.00")
        self.in_person_lot.auctiontos_winner = self.in_person_buyer
        self.in_person_lot.active = False
        self.in_person_lot.save(update_fields=["winning_price", "auctiontos_winner", "active"])
        invoice.status = "PAID"
        invoice.save(update_fields=["status"])
        ClubMoney.objects.all().delete()
        self.in_person_auction.club = self.club
        self.in_person_auction.save(update_fields=["club"])
        self.assertTrue(ClubMoney.objects.filter(invoice=invoice).exists())


class ClubProfitTests(TestCase):
    """Auction.club_profit -- what the club nets from auction activity.

    Regression coverage for the four defects fixed in Item 14:
      * a genuine loss stays negative (no abs()),
      * invoices whose calculated_total was never stamped (NULL) are not dropped,
      * cents survive end to end (no int() truncation),
      * sales tax and membership dues are excluded (they are not auction-activity profit).
    """

    def setUp(self):
        self.creator = User.objects.create_user("profit_creator", "pc@example.com", "pw")
        self.club = Club.objects.create(
            name="Profit Club", enable_membership=True, membership_annual_fee=Decimal("25.00")
        )
        self._n = 0

    def _auction(self, *, tax=0, rounding=False, club_pct=20, first_bid_payout=0):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Profit Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=club_pct,
            tax=tax,
            lot_entry_fee=0,
            unsold_lot_fee=0,
            first_bid_payout=first_bid_payout,
            invoice_rounding=rounding,
        )
        PickupLocation.objects.create(name="Profit Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _sold_lot(self, auction, seller, buyer, price):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            quantity=1,
        )

    def _invoice(self, tos, *, paid=False, renewal_needed=False):
        invoice = Invoice.objects.get_or_create(auctiontos_user=tos)[0]
        if renewal_needed:
            invoice.renewal_needed = True
        if paid:
            invoice.status = "PAID"
        if paid or renewal_needed:
            invoice.save()
        return invoice

    def test_normal_profit_is_positive_commission(self):
        # club_pct=20 on a $100 lot: buyer pays 100, seller gets 80, club keeps 20.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.club_profit, Decimal("20.00"))
        self.assertIsInstance(auction.club_profit, Decimal)

    def test_loss_shows_as_negative(self):
        # club takes no cut but promises every buyer a $5 first-bid payout: it collects 5 from the
        # buyer yet owes the seller 10, a real $5 loss. The old abs() reported this as +5 profit.
        auction = self._auction(club_pct=0, first_bid_payout=5)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.club_profit, Decimal("-5.00"))
        self.assertLess(auction.club_profit, 0)

    def test_null_calculated_total_is_not_dropped(self):
        # Fresh draft invoices never had calculated_total stamped; they must still contribute their
        # live rounded_net instead of silently vanishing from the total.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        seller_invoice = self._invoice(seller)
        buyer_invoice = self._invoice(buyer)
        # Precondition: both invoices genuinely have an unstamped (NULL) calculated_total.
        self.assertIsNone(seller_invoice.calculated_total)
        self.assertIsNone(buyer_invoice.calculated_total)
        # Profit is still the full 20 (100 collected - 80 paid), not 0 as it was when NULLs dropped.
        self.assertEqual(auction.club_profit, Decimal("20.00"))

    def test_cents_are_preserved(self):
        # club_pct=33 on a $10 lot: seller cut 6.70, club cut 3.30. int() truncation would drop the
        # 30 cents (yielding 3). No invoice_rounding, so the exact cents must flow through.
        auction = self._auction(club_pct=33)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.club_profit, Decimal("3.30"))
        self.assertNotEqual(auction.club_profit, Decimal(3))

    def test_tax_is_excluded(self):
        # 10% tax on a $100 lot: buyer owes 110, but the extra 10 is remitted to the taxing
        # authority. club_profit is the 20 commission, NOT 30 (which would count the tax as profit).
        auction = self._auction(club_pct=20, tax=10)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction._auction_tax_collected, Decimal("10.00"))
        self.assertEqual(auction.club_profit, Decimal("20.00"))

    def test_membership_dues_are_excluded(self):
        # A renewing buyer pays their $100 in bids plus the $25 annual fee. Dues are separate club
        # revenue, so club_profit stays the 20 commission, not 45.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True, renewal_needed=True)
        self.assertEqual(auction._auction_membership_dues, Decimal("25.00"))
        self.assertEqual(auction.club_profit, Decimal("20.00"))

    def test_tax_and_dues_together_leave_only_commission(self):
        auction = self._auction(club_pct=20, tax=10)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True, renewal_needed=True)
        # buyer invoice net = -(100 bids + 10 tax + 25 dues) = -135; seller net = +80.
        # -sum(calculated_total) = 55, minus 10 tax minus 25 dues = 20 commission.
        self.assertEqual(auction.club_profit, Decimal("20.00"))

    def test_untaxed_non_membership_auction_backs_out_nothing(self):
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction._auction_tax_collected, Decimal("0.00"))
        self.assertEqual(auction._auction_membership_dues, Decimal("0.00"))

    def test_derived_properties_track_the_fix(self):
        # percent_to_club derives from club_profit, so the sign fix flows through: on a loss the
        # club's percentage of gross is correctly negative -- which the old abs() masked.
        # total_to_sellers is computed directly (Item 15), so the buyer's $5 first-bid payout is NOT
        # miscounted as money paid to the seller: the seller is credited exactly their $10 cut.
        auction = self._auction(club_pct=0, first_bid_payout=5)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.gross, Decimal(10))
        self.assertEqual(auction.club_profit, Decimal("-5.00"))
        self.assertEqual(auction.total_to_sellers, Decimal("10.00"))
        self.assertLess(auction.percent_to_club, 0)


class TotalToSellersPercentToClubTests(TestCase):
    """Auction.total_to_sellers and Auction.percent_to_club (Item 15).

    total_to_sellers is now computed directly from the per-lot seller cut (``your_cut``) rather
    than as ``gross - club_profit``. That subtraction distorted the figure once club_profit
    stopped mirroring gross: club_profit excludes tax and dues (never part of gross) and reflects
    buyer-side promotions, so subtraction would fold tax, dues and buyer payouts into a number
    that is supposed to be only what sellers are owed. percent_to_club is club_profit as a
    Decimal fraction of gross, negative on a loss and 0 (not a ZeroDivisionError) when gross is 0.
    """

    def setUp(self):
        self.creator = User.objects.create_user("tts_creator", "tts@example.com", "pw")
        self.club = Club.objects.create(
            name="Sellers Club", enable_membership=True, membership_annual_fee=Decimal("25.00")
        )
        self._n = 0

    def _auction(self, *, tax=0, club_pct=25, first_bid_payout=0, unsold_lot_fee=0, rounding=False):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Sellers Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=club_pct,
            tax=tax,
            lot_entry_fee=0,
            unsold_lot_fee=unsold_lot_fee,
            first_bid_payout=first_bid_payout,
            invoice_rounding=rounding,
        )
        PickupLocation.objects.create(name="Sellers Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _sold_lot(self, auction, seller, buyer, price, *, banned=False, donation=False):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            banned=banned,
            donation=donation,
            quantity=1,
        )

    def _unsold_lot(self, auction, seller):
        return Lot.objects.create(
            lot_name="Unsold lot",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=None,
            active=False,
            quantity=1,
        )

    def _invoice(self, tos, *, paid=False, renewal_needed=False):
        invoice = Invoice.objects.get_or_create(auctiontos_user=tos)[0]
        if renewal_needed:
            invoice.renewal_needed = True
        if paid:
            invoice.status = "PAID"
        if paid or renewal_needed:
            invoice.save()
        return invoice

    # ---- total_to_sellers ----------------------------------------------------------------

    def test_total_to_sellers_matches_seller_credits(self):
        # club_pct=25: on $100 the seller keeps 75, on $40 they keep 30. Total credited = 105.
        auction = self._auction(club_pct=25)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 40)
        self.assertEqual(auction.total_to_sellers, Decimal("105.00"))
        self.assertIsInstance(auction.total_to_sellers, Decimal)

    def test_total_to_sellers_not_distorted_by_tax(self):
        # 10% tax makes the buyer owe 110, but tax never touches the seller cut: still 80.
        # gross - club_profit would also give 80 here only by coincidence; the direct value is 80.
        auction = self._auction(club_pct=20, tax=10)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.total_to_sellers, Decimal("80.00"))

    def test_total_to_sellers_not_distorted_by_membership_dues(self):
        # A renewing buyer pays $25 dues on top of their bids; the seller is still owed only 80.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True, renewal_needed=True)
        self.assertEqual(auction._auction_membership_dues, Decimal("25.00"))
        self.assertEqual(auction.total_to_sellers, Decimal("80.00"))

    def test_total_to_sellers_excludes_buyer_first_bid_payout(self):
        # club_pct=0 + $5 first-bid payout to the buyer: the seller is credited their full $10.
        # gross - club_profit = 10 - (-5) = 15 would wrongly count the buyer payout as a payout.
        auction = self._auction(club_pct=0, first_bid_payout=5)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.total_to_sellers, Decimal("10.00"))

    def test_total_to_sellers_ignores_banned_and_donated_lots(self):
        # Banned lots are never charged (seller gets 0) and donations go entirely to the club
        # (seller cut 0). Only the normal $100 lot's $75 cut counts.
        auction = self._auction(club_pct=25)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 200, banned=True)
        self._sold_lot(auction, seller, buyer, 60, donation=True)
        self.assertEqual(auction.total_to_sellers, Decimal("75.00"))

    def test_total_to_sellers_ignores_unsold_lot_fees(self):
        # An unsold lot carries a seller-charged unsold_lot_fee. total_to_sellers reports payouts
        # for lots that sold; the fee must not silently reduce it below the sold lot's cut.
        auction = self._auction(club_pct=25, unsold_lot_fee=2)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._unsold_lot(auction, seller)
        self.assertEqual(auction.total_to_sellers, Decimal("75.00"))

    def test_total_to_sellers_zero_when_nothing_sold(self):
        auction = self._auction(club_pct=25)
        seller = self._tos(auction)
        self._unsold_lot(auction, seller)
        self.assertEqual(auction.total_to_sellers, Decimal("0.00"))
        self.assertIsInstance(auction.total_to_sellers, Decimal)

    # ---- percent_to_club -----------------------------------------------------------------

    def test_percent_to_club_known_split(self):
        # club_pct=20 on a $100 lot: club nets 20 of 100 gross = 20%.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.percent_to_club, Decimal(20))
        self.assertIsInstance(auction.percent_to_club, Decimal)

    def test_percent_to_club_zero_gross_returns_zero(self):
        # No sold lots -> gross is 0. Must return a sane 0, not raise ZeroDivisionError.
        auction = self._auction(club_pct=20)
        self.assertEqual(auction.gross, 0)
        self.assertEqual(auction.percent_to_club, Decimal(0))
        self.assertIsInstance(auction.percent_to_club, Decimal)

    def test_percent_to_club_negative_on_loss(self):
        # club_pct=0 + $5 buyer payout: the club loses $5 on $10 gross = -50%.
        auction = self._auction(club_pct=0, first_bid_payout=5)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        self._invoice(seller, paid=True)
        self._invoice(buyer, paid=True)
        self.assertEqual(auction.club_profit, Decimal("-5.00"))
        self.assertEqual(auction.percent_to_club, Decimal(-50))
        self.assertLess(auction.percent_to_club, 0)


class AuctionGrossTests(TestCase):
    """Auction.gross -- refund-adjusted gross sales (Item 17).

    gross was ``Sum("winning_price")`` over every lot in the auction. That had three defects, all
    fixed here so gross reconciles with the money stats shown beside it on the stats page:

      * it counted BANNED (removed) lots, which are never charged and are excluded from
        ``total_to_sellers``, ``median_lot_price`` and ``total_sold_lots``;
      * it ignored PARTIAL REFUNDS, reporting the full hammer price even though a refund
        proportionally reduces both the buyer's bill and the seller's payout;
      * its filter did not match ``total_sold_lots``, so "N lots sold, $X gross" could count
        different lots.

    The chosen basis is refund-adjusted final price (``winning_price * (100 - refund%) / 100``) over
    sold, non-banned lots, which makes ``gross == total_to_sellers + club_profit_raw`` exactly.
    """

    def setUp(self):
        self.creator = User.objects.create_user("gross_creator", "gross@example.com", "pw")
        self.club = Club.objects.create(name="Gross Club")
        self._n = 0

    def _auction(self, *, club_pct=25, lot_entry_fee=0, unsold_lot_fee=0):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Gross Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=club_pct,
            lot_entry_fee=lot_entry_fee,
            unsold_lot_fee=unsold_lot_fee,
        )
        PickupLocation.objects.create(name="Gross Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _sold_lot(self, auction, seller, buyer, price, *, banned=False, donation=False, refund=0):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            partial_refund_percent=refund,
            active=False,
            banned=banned,
            donation=donation,
            quantity=1,
        )

    def _unsold_lot(self, auction, seller):
        return Lot.objects.create(
            lot_name="Unsold lot",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=None,
            active=False,
            quantity=1,
        )

    def test_gross_simple_known_prices(self):
        # Two sold lots, $100 and $40, no refunds: gross is the plain hammer total, 140.
        auction = self._auction()
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 40)
        self.assertEqual(auction.gross, Decimal("140.00"))
        self.assertIsInstance(auction.gross, Decimal)

    def test_gross_includes_donations(self):
        # A donation is still billed to the buyer at the hammer price (it all goes to the club),
        # so it is genuine gross even though the seller's cut is 0.
        auction = self._auction()
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 50, donation=True)
        self.assertEqual(auction.gross, Decimal("150.00"))

    def test_gross_excludes_banned_lots(self):
        # The $200 banned lot is pulled from the sale and never charged, so it must not inflate
        # gross above the $100 that actually sold.
        auction = self._auction()
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 200, banned=True)
        self.assertEqual(auction.gross, Decimal("100.00"))

    def test_gross_excludes_unsold_lots(self):
        # An unsold lot has no winning_price and contributes nothing to gross.
        auction = self._auction(unsold_lot_fee=2)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._unsold_lot(auction, seller)
        self.assertEqual(auction.gross, Decimal("100.00"))

    def test_gross_nets_out_partial_refund(self):
        # A 25% partial refund on a $100 lot reduces both the buyer's bill and the seller's payout,
        # so refund-adjusted gross is 75, not the full 100 hammer price. A second un-refunded $40
        # lot adds its full price: 75 + 40 = 115.
        auction = self._auction()
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100, refund=25)
        self._sold_lot(auction, seller, buyer, 40)
        self.assertEqual(auction.gross, Decimal("115.00"))

    def test_gross_ties_out_to_seller_and_club_cuts(self):
        # The whole point of the refund-adjusted basis: gross must equal what sellers are credited
        # plus the club's raw cut of the same lots, refunds and all.
        auction = self._auction(club_pct=25, lot_entry_fee=0)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100, refund=20)  # final 80: seller 60, club 20
        self._sold_lot(auction, seller, buyer, 40)  # final 40: seller 30, club 10
        expected = auction.total_to_sellers + Decimal(auction.club_profit_raw)
        self.assertEqual(auction.gross, expected)
        self.assertEqual(auction.gross, Decimal("120.00"))

    def test_gross_and_total_sold_lots_count_the_same_lots(self):
        # "N lots sold, $X gross" must be coherent: the lots counted by total_sold_lots are exactly
        # the ones summed by gross. The banned and unsold lots are excluded from both.
        auction = self._auction()
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._sold_lot(auction, seller, buyer, 40)
        self._sold_lot(auction, seller, buyer, 999, banned=True)
        self._unsold_lot(auction, seller)
        self.assertEqual(auction.total_sold_lots, 2)
        # Recompute gross independently over the same filter the count uses.
        sold = auction.lots_qs.filter(winning_price__isnull=False).exclude(banned=True)
        manual = sum(
            (lot.winning_price * (100 - lot.partial_refund_percent) / 100 for lot in sold),
            Decimal("0.00"),
        )
        self.assertEqual(auction.gross, manual)
        self.assertEqual(auction.gross, Decimal("140.00"))

    def test_gross_empty_auction_is_zero(self):
        # No lots at all: gross is a sane Decimal 0, never a crash or None.
        auction = self._auction()
        self.assertEqual(auction.gross, Decimal("0.00"))
        self.assertIsInstance(auction.gross, Decimal)

    def test_gross_only_unsold_is_zero(self):
        auction = self._auction(unsold_lot_fee=5)
        seller = self._tos(auction)
        self._unsold_lot(auction, seller)
        self.assertEqual(auction.gross, Decimal("0.00"))
