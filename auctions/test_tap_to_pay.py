"""Tests for the Tap to Pay on iPhone review-guide work (TTP-1..4).

These aren't cosmetic. Apple grants the *publishing* entitlement — the one TestFlight and the App
Store need — only after reviewing the app against the Tap to Pay on iPhone App & Marketing
Requirements, and three of these are review blockers rather than polish:

* **TTP-1 (2.2, General Requirements)** — merchant onboarding must work inside the app. Sending a
  merchant to Safari to connect Square fails both rules, and it's the first thing the reviewer's
  onboarding video would show.
* **TTP-2 (5.4, 5.5)** — the button's wording comes from Apple's localization table, and an icon
  may only be SF Symbols' ``wave.3.right.circle``. "Tap to Pay on iPhone" is also iPhone-only
  wording, so the same template must not say it on an Android phone.
* **TTP-3 (1.5, 5.6, 3.8)** — the reader has to start preparing when the app foregrounds, which
  needs seller credentials before any invoice exists, and only the backend knows who is authorized
  to accept Apple's terms.
* **TTP-4 (5.10)** — a receipt must be sendable for every outcome, approved or declined.
"""

from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.middleware import MobileAppMiddleware
from auctions.mobile.services.payments import PaymentService
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    SquareSeller,
)
from auctions.tests import StandardTestCase

IOS_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"
ANDROID_UA = "FishAuctionsApp/1.0 (Flutter; Android)"


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class MobileAppPlatformFlagTests(TestCase):
    """TTP-2 — the platform booleans the checkout button branches on."""

    def _flags(self, user_agent):
        request = RequestFactory().get("/", HTTP_USER_AGENT=user_agent)
        MobileAppMiddleware(lambda r: None)(request)
        return request

    def test_ios_app(self):
        request = self._flags(IOS_UA)
        self.assertTrue(request.is_mobile_app)
        self.assertTrue(request.is_ios_app)
        self.assertFalse(request.is_android_app)

    def test_android_app(self):
        request = self._flags(ANDROID_UA)
        self.assertTrue(request.is_mobile_app)
        self.assertFalse(request.is_ios_app)
        self.assertTrue(request.is_android_app)

    def test_web_visitor_is_neither(self):
        request = self._flags("Mozilla/5.0")
        self.assertFalse(request.is_mobile_app)
        self.assertFalse(request.is_ios_app)
        self.assertFalse(request.is_android_app)

    def test_app_with_no_platform_token_is_neither(self):
        # The label falls back to the short form in this case; both strings are approved, so an
        # unknown platform must never be the reason "on iPhone" appears on an Android phone.
        request = self._flags("FishAuctionsApp/1.0")
        self.assertTrue(request.is_mobile_app)
        self.assertFalse(request.is_ios_app)
        self.assertFalse(request.is_android_app)

    def test_real_ios_webview_user_agent(self):
        # What actually arrives: the app appends its token to WKWebView's default User-Agent.
        request = self._flags(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Mobile/15E148 FishAuctionsApp/1.0 (Flutter; iOS)"
        )
        self.assertTrue(request.is_mobile_app)
        self.assertTrue(request.is_ios_app)

    def test_real_android_webview_user_agent(self):
        request = self._flags(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/120.0.6099.43 Mobile Safari/537.36 "
            "FishAuctionsApp/1.0 (Flutter; Android)"
        )
        self.assertTrue(request.is_mobile_app)
        self.assertTrue(request.is_android_app)
        self.assertFalse(request.is_ios_app)

    def test_android_device_model_containing_ios_is_still_android(self):
        """The device model is in the same header, and Android model names can contain "ios".

        A "Kiosk-…" handheld is the easy example, and kiosk hardware is exactly what ends up on a
        check-in desk. Reading the platform out of the whole User-Agent matches that model before it
        ever reaches the ``; Android)`` the app wrote — and puts "Tap to Pay on iPhone", a trademark
        Apple only permits on iOS, on an Android screen.
        """
        request = self._flags(
            "Mozilla/5.0 (Linux; Android 13; Kiosk-T10 Build/TQ3A) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/119.0 Mobile Safari/537.36 "
            "FishAuctionsApp/1.0 (Flutter; Android)"
        )
        self.assertTrue(request.is_android_app)
        self.assertFalse(request.is_ios_app)
        self.assertEqual(request.mobile_app_platform, "android")

    def test_a_web_browser_on_a_kiosk_device_is_not_the_app(self):
        request = self._flags(
            "Mozilla/5.0 (Linux; Android 13; Kiosk-T10) AppleWebKit/537.36 Chrome/119.0 Safari/537.36"
        )
        self.assertFalse(request.is_mobile_app)
        self.assertFalse(request.is_ios_app)
        self.assertEqual(request.mobile_app_platform, "")


class TapToPayButtonCopyMixin:
    """TTP-2 — requirement 5.4 (approved wording) and 5.5 (no unapproved iconography).

    Run against every page that renders the ``fishauctions://pay/`` handoff. There are two now
    (quick checkout and the invoice page), and the whole risk with approved wording is that one of
    them drifts: "Tap to Pay on iPhone" is a trademark Apple permits on iOS only, and an icon that
    isn't SF Symbols' wave.3.right.circle is a review finding wherever it appears. Subclasses
    supply ``_html(user_agent)`` and the invoice the button points at.
    """

    def _html(self, user_agent):
        raise NotImplementedError

    def test_ios_uses_the_long_form_label(self):
        html = self._html(IOS_UA)
        self.assertIn("Tap to Pay on iPhone", html)

    def test_android_never_says_on_iphone(self):
        # "Tap to Pay on iPhone" is iPhone-only wording; Android gets the approved short form.
        html = self._html(ANDROID_UA)
        self.assertIn("Tap to Pay", html)
        self.assertNotIn("Tap to Pay on iPhone", html)

    def test_no_iconography_on_the_button(self):
        """5.5 permits only wave.3.right.circle(.fill); the marketing rules forbid inventing one.

        Dropping the icon entirely is the way out: the requirement is conditional on using an icon
        at all. So the deep-link button must carry no <i> glyph.
        """
        html = self._html(IOS_UA)
        button = html.split(f'fishauctions://pay/{self.invoice.pk}"')[1].split("</a>")[0]
        self.assertNotIn("<i ", button)
        self.assertNotIn("bi-credit-card", button)

    def test_the_old_unapproved_label_is_gone(self):
        self.assertNotIn("Tap to Pay with card", self._html(IOS_UA))

    def test_the_button_is_not_offered_outside_the_app(self):
        # The scheme has no handler in a browser, and this is not a page people use on desktop
        # expecting it -- see the app-only exception in style_reference.md.
        self.assertNotIn("fishauctions://pay/", self._html("Mozilla/5.0"))


class TapToPayWarmUpMixin:
    """TTP-10 — requirement 1.5 (warm the reader early) and 5.6 (the prompt on screen in a second).

    The app warms the reader at mount and again when it foregrounds, but a resume can be hours
    before anybody actually charges a card. The page that draws the pay button is the last honest
    moment to say "a charge is imminent", so every page that renders the ``fishauctions://pay/``
    handoff also asks the app to warm up — and the two must not drift apart, which is why this runs
    against both of them exactly as the copy mixin does.

    The server drives it on purpose. The app deliberately does not infer checkout pages from the
    URL: the awareness modal used to guess from a URL prefix and announced a merchant feature to
    organizers who had none, so it now waits to be told. This is the same rule, and the tests below
    are what stop it being "simplified" into a URL check in the app.
    """

    HANDLER = "callHandler('tapToPayWarm')"

    def _html(self, user_agent):
        raise NotImplementedError

    def test_the_page_that_draws_the_button_warms_the_reader(self):
        self.assertIn(self.HANDLER, self._html(IOS_UA))

    def test_android_warms_too(self):
        """Tap to Pay is iPhone-only wording, not an iPhone-only feature; Android readers warm too."""
        self.assertIn(self.HANDLER, self._html(ANDROID_UA))

    def test_nothing_is_warmed_outside_the_app(self):
        self.assertNotIn(self.HANDLER, self._html("Mozilla/5.0"))

    def test_warming_is_rendered_only_where_the_button_is(self):
        """Warming asks the backend for eligibility, so a page-wide call is a wasted request a view.

        Tying the count to the number of buttons is what keeps it that way: one button, one warm.
        """
        html = self._html(IOS_UA)
        self.assertEqual(html.count(self.HANDLER), html.count("fishauctions://pay/"))
        self.assertEqual(html.count(self.HANDLER), 1)

    def test_the_call_is_fire_and_forget(self):
        """An older app build has no such handler and the promise rejects.

        Nothing on the page may depend on the answer — it resolves ``{warmed: true|false}`` and
        ``false`` only means the app's throttle swallowed it — and an unhandled rejection from a
        build that shipped before this handler existed must not break the page. So the call is
        never awaited and always caught.
        """
        html = self._html(IOS_UA)
        self.assertIn(".catch(", html.split(self.HANDLER)[1].split("</script>")[0])

    def test_the_bridge_is_checked_before_it_is_called(self):
        """In a browser there is no ``window.flutter_inappwebview`` at all, so calling it throws."""
        script = self._html(IOS_UA).split(self.HANDLER)[0].rsplit("<script>", 1)[1]
        self.assertIn("window.flutter_inappwebview &&", script)


class QuickCheckoutTapToPayCopyTests(TapToPayButtonCopyMixin, TapToPayWarmUpMixin, StandardTestCase):
    """The checkout desk: the button's original home."""

    def setUp(self):
        super().setUp()
        self.in_person_tos.bidder_number = "TTP1"
        self.in_person_tos.save()
        self.invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        self.client.force_login(self.admin_user)
        self.url = reverse(
            "auction_quick_checkout_htmx",
            kwargs={"slug": self.in_person_auction.slug, "filter": "TTP1"},
        )

    def _html(self, user_agent):
        from auctions.views import QuickCheckoutHTMX

        with (
            patch.object(Invoice, "show_square_button", new_callable=PropertyMock, return_value=True),
            patch.object(Invoice, "reason_for_payment_not_available", new_callable=PropertyMock, return_value=""),
            patch.object(
                QuickCheckoutHTMX, "create_payment_link", return_value=("https://squareup.com/pay/fake", None)
            ),
        ):
            return self.client.get(self.url, HTTP_USER_AGENT=user_agent).content.decode()

    def test_button_comes_before_the_qr_block(self):
        """5.2 — with several payment options, Tap to Pay sits at the top of the list."""
        html = self._html(IOS_UA)
        self.assertLess(html.index(f"fishauctions://pay/{self.invoice.pk}"), html.index("View or adjust invoice"))


class InvoicePageTapToPayTests(TapToPayButtonCopyMixin, TapToPayWarmUpMixin, StandardTestCase):
    """TTP-8 — the invoice page was a dead end in the app, and then offered a charge with nothing
    left to collect.

    It hides the web PayPal and Square buttons for app requests (both redirect to a hosted checkout
    the WebView can't run) and offered nothing in their place, so an admin who reached an invoice
    from the users table, a search or a notification saw the payment options vanish with no hint
    that a working path existed one screen away. A reviewer with a demo account goes to Invoices
    first, and concludes Tap to Pay doesn't work.

    The button that fixed that gated on ``status != "PAID"`` alone, which is not the same question
    as "does this person still owe the club money". Every settled-but-not-PAID invoice — a zero
    balance, one already covered by recorded payments, a seller the club owes — put a card reader in
    front of a cashier with nothing to charge for. ``quick_checkout_htmx.html`` never had the bug:
    ``show_square_button`` tests the balance. The tests below are the invoice page borrowing that
    half without borrowing ``enable_square_payments`` with it.
    """

    def setUp(self):
        super().setUp()
        # Every test in this class is about a cashier collecting money, so the shared fixture's
        # invoice needs something to collect: in_person_tos neither buys nor sells anything in the
        # fixture, which makes its invoice exactly the settled $0 one this feature must refuse.
        # 25% to the club and a $2 entry fee are the auction's, so the balance is the buyer's $40
        # plus 25% tax -- the number does not matter, only its sign.
        self.won_lot = Lot.objects.create(
            lot_name="a lot this bidder won",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            auctiontos_winner=self.in_person_tos,
            winning_price=40,
            quantity=1,
            active=False,
        )
        self.invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_tos)
        self.client.force_login(self.admin_user)
        self.url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})

    def _balance(self):
        return Invoice.objects.get(pk=self.invoice.pk).rounded_net_after_payments

    def _html(self, user_agent, **patches):
        offers = patches.pop("offers_tap_to_pay", True)
        with patch.object(Auction, "offers_tap_to_pay", new_callable=PropertyMock, return_value=offers):
            return self.client.get(self.url, HTTP_USER_AGENT=user_agent).content.decode()

    def test_the_admin_gets_the_button_in_the_app(self):
        self.assertIn(f"fishauctions://pay/{self.invoice.pk}", self._html(IOS_UA))

    def test_nothing_is_offered_when_this_auctions_square_account_cannot_take_a_card(self):
        # A seller connected before the in-person scope existed can't charge in the room, so the
        # button would be a dead end -- the same question Auction.offers_tap_to_pay answers for the
        # awareness modal.
        self.assertNotIn("fishauctions://pay/", self._html(IOS_UA, offers_tap_to_pay=False))

    def test_a_paid_invoice_is_not_offered_a_charge(self):
        """And the status half of the gate is load-bearing on its own.

        An invoice marked paid at the desk in cash has no ``InvoicePayment`` row, so its balance
        still reads as owing. Dropping ``status != "PAID"`` in favour of the balance test alone
        would put the reader back in front of the cashier for money already in the till.
        """
        self.invoice.status = "PAID"
        self.invoice.save()
        self.assertLess(self._balance(), 0)
        self.assertNotIn("fishauctions://pay/", self._html(IOS_UA))

    def test_the_fixture_invoice_really_does_owe_the_club(self):
        """Guards setUp: on a $0 invoice every other test in this class passes for the wrong reason.

        ``rounded_net_after_payments`` is negative when the buyer owes the club and positive when
        the club owes them (``Invoice.net_after_payments``), which is why the template asks for
        ``< 0`` rather than a truthiness test.
        """
        self.assertLess(self._balance(), 0)

    def test_a_zero_balance_is_not_offered_a_charge(self):
        """The bug: a settled invoice nobody marked PAID still offered the cashier a card charge.

        This is the ordinary shape of it -- an invoice with nothing on it at all -- and it is what
        the whole fixture looked like before setUp gave this one a lot.
        """
        self.won_lot.delete()
        self.assertEqual(self._balance(), 0)
        html = self._html(IOS_UA)
        self.assertNotIn("fishauctions://pay/", html)
        # ...and with no button there is nothing to warm the reader for, either (TTP-10).
        self.assertNotIn("tapToPayWarm", html)

    def test_a_balance_already_covered_by_payments_is_not_offered_a_charge(self):
        """The money is in: a Square QR, a PayPal capture, a cash payment somebody recorded.

        ``status`` is still DRAFT here -- nothing marks an invoice PAID just because the payments
        add up -- so this is precisely the case ``status != "PAID"`` cannot see.
        """
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        InvoicePayment.objects.create(invoice=invoice, amount=-invoice.net_after_payments)
        self.assertNotEqual(invoice.status, "PAID")
        self.assertEqual(self._balance(), 0)
        self.assertNotIn("fishauctions://pay/", self._html(IOS_UA))

    def test_a_seller_the_club_owes_is_not_offered_a_charge(self):
        """The sign matters, not just the zero: a payout invoice owes money the other way.

        Charging their card is not a smaller version of paying them out, it is the opposite thing,
        and ``status != "PAID"`` alone offered it on every unpaid vendor invoice in the auction.
        """
        self.won_lot.delete()
        Lot.objects.create(
            lot_name="a lot this seller sold",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            auctiontos_winner=self.in_person_buyer,
            winning_price=100,
            quantity=1,
            active=False,
        )
        self.assertGreater(self._balance(), 0)
        self.assertNotIn("fishauctions://pay/", self._html(IOS_UA))

    def test_the_online_payment_switch_is_still_not_the_question(self):
        """Only the balance half of ``show_square_button`` was borrowed, deliberately.

        This is the cashier collecting in the room, so the question is whether the auction's Square
        account can take a card at all (``offers_tap_to_pay``) -- not whether the buyer-facing
        online payment flow has been opened.
        """
        self.in_person_auction.enable_square_payments = False
        self.in_person_auction.enable_online_payments = False
        self.in_person_auction.save()
        self.assertIn("fishauctions://pay/", self._html(IOS_UA))

    def test_the_buyer_is_never_offered_it(self):
        """Tap to Pay authorizes with the *seller's* Square account: this is the cashier's button.

        A bidder looking at their own invoice in the app gets the ordinary payment buttons or
        nothing, never the reader. (self.user can't stand in for the bidder here -- they created
        the auction, so the invoice page treats them as an admin, which is the correct answer.)
        """
        bidder = User.objects.create_user("ttpbidder", "ttpbid@example.com", "pw")
        tos = AuctionTOS.objects.create(
            user=bidder, auction=self.in_person_auction, pickup_location=self.in_person_location
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        self.client.force_login(bidder)
        self.url = reverse("invoice_by_pk", kwargs={"pk": invoice.pk})
        self.assertNotIn("fishauctions://pay/", self._html(IOS_UA))


class SquareOnboardingInAppTests(StandardTestCase):
    """TTP-1 — the connect links must render inside the app, with no "use a browser" banner."""

    def setUp(self):
        super().setUp()
        # Card payments enabled: this class is about the merchant path. The account waiting to be
        # reviewed is SquareAccessGateDisclosureTests below.
        self.user.userdata.square_enabled = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.url = reverse("square_seller")

    def test_connect_link_renders_in_the_app(self):
        html = self.client.get(self.url, HTTP_USER_AGENT=IOS_UA).content.decode()
        self.assertIn(reverse("square_connect"), html)

    def test_no_open_this_on_the_website_banner(self):
        html = self.client.get(self.url, HTTP_USER_AGENT=IOS_UA).content.decode()
        self.assertNotIn("isn't available in the app", html)

    def test_paypal_page_matches(self):
        html = self.client.get(reverse("paypal_seller"), HTTP_USER_AGENT=IOS_UA).content.decode()
        self.assertIn(reverse("paypal_connect"), html)
        self.assertNotIn("isn't available in the app", html)

    def test_reconnect_prompt_is_actionable_in_the_app(self):
        # A legacy Square account can't take an in-person charge until it's reconnected, so the
        # button that fixes it has to be reachable from the device that hit the problem.
        SquareSeller.objects.create(user=self.user, square_merchant_id="MID", access_token="t", scopes="")
        html = self.client.get(self.url, HTTP_USER_AGENT=IOS_UA).content.decode()
        self.assertIn("Reconnect required for Tap to Pay", html)
        self.assertIn(reverse("square_connect"), html)


class SquareAccessGateDisclosureTests(StandardTestCase):
    """TTP-9 — ``square_enabled`` is off by default, and it used to be enforced by rendering nothing.

    An organizer who wanted to take card payments found no button, no explanation and no way to
    ask. From inside the app that is indistinguishable from "this site can't do card payments",
    which is the reading Apple's onboarding requirements (2.1, 2.2) exist to prevent. The gate is
    deliberately still here: what these tests hold is that it is *visible and requestable*.
    """

    def setUp(self):
        super().setUp()
        # Set rather than assumed: SQUARE_ENABLED_FOR_USERS decides the column default, and the dev
        # .env turns it on while CI leaves it off. The state under test is the default one a live
        # site runs -- an organizer whose account has not been reviewed yet.
        self.user.userdata.square_enabled = False
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.url = reverse("square_seller")

    def _html(self, user_agent=IOS_UA):
        return self.client.get(self.url, HTTP_USER_AGENT=user_agent).content.decode()

    def test_the_page_says_accounts_are_reviewed(self):
        self.assertIn("reviewed before they're switched on", self._html())

    def test_the_page_carries_the_request_access_button(self):
        html = self._html()
        self.assertIn("Contact us and request access", html)
        self.assertIn("mailto:", html)

    def test_no_connect_button_that_would_only_bounce_them_home(self):
        # SquareConnectView refuses an account this isn't enabled on, so a connect button here is a
        # dead end. Asking is the action that is actually available.
        self.assertNotIn(reverse("square_connect"), self._html())

    def test_reaching_connect_directly_lands_on_the_explanation(self):
        """The gate's own error used to bounce them to the home page with "Square isn't enabled".

        That is the same dead end as rendering nothing, one URL later -- and it is reachable from
        an old bookmark or an app build that still deep-links here. square_seller is where the
        review is explained and where the request-access button lives.
        """
        response = self.client.get(reverse("square_connect"))
        self.assertRedirects(response, reverse("square_seller"))
        self.assertContains(self.client.get(reverse("square_seller")), "request access", status_code=200)

    def test_an_enabled_account_still_gets_the_connect_button(self):
        self.user.userdata.square_enabled = True
        self.user.userdata.save()
        html = self._html()
        self.assertIn(reverse("square_connect"), html)
        self.assertNotIn("reviewed before they're switched on", html)

    def test_the_menu_entry_stays_visible_for_someone_who_runs_an_auction(self):
        """preferences_ribbon.html used to hide the whole Square item on square_enabled."""
        html = self.client.get(reverse("preferences"), HTTP_USER_AGENT=IOS_UA).content.decode()
        self.assertIn(reverse("square_seller"), html)

    def test_the_menu_entry_is_not_offered_to_someone_with_nothing_to_charge_for(self):
        # A bidder with no auction and no club has nothing to connect Square for; the entry would
        # be noise. This is the half of the old gate that was doing useful work.
        bidder = User.objects.create_user("squarebidder", "sqb@example.com", "pw")
        bidder.userdata.square_enabled = False  # see setUp: the flag's default is site config
        bidder.userdata.save()
        self.client.force_login(bidder)
        html = self.client.get(reverse("preferences")).content.decode()
        self.assertNotIn(reverse("square_seller"), html)

    def test_the_auction_banner_is_shown_rather_than_hidden(self):
        """Auction.show_square_banner used to return False on the gate, so the organizer's one
        route to card payments disappeared from the page they actually look at."""
        self.assertTrue(self.in_person_auction.show_square_banner)

    def test_the_banner_still_stops_for_the_reasons_that_are_not_the_gate(self):
        self.user.userdata.never_show_square_connect = True
        self.user.userdata.save()
        self.in_person_auction.refresh_from_db()
        self.assertFalse(self.in_person_auction.show_square_banner)

    def test_the_request_access_mailto_names_the_admin_and_the_user(self):
        query = self.user.userdata.square_access_request_mailto_query
        self.assertTrue(query.startswith(settings.ADMINS[0][1]))
        self.assertIn(self.user.username, query)

    def _club_settings_html(self):
        """The club's membership settings page, as an admin who can set its payment accounts."""
        club = Club.objects.create(name="Gate Disclosure Club")
        ClubMember.objects.create(club=club, user=self.user, name="Organizer", permission_edit_club=True)
        return self.client.get(
            reverse("club_membership_settings", kwargs={"slug": club.slug}), HTTP_USER_AGENT=IOS_UA
        ).content.decode()

    # Every test that renders this page names the site-level Square credentials, because this page
    # asks a second question square_seller.html never asks -- whether the *site* has a Square app
    # at all -- and that check sits ahead of the per-account gate in the template, deliberately:
    # "nobody here can connect Square" is a different answer from "your account is in the queue",
    # and sending a club admin to a request-access mailto on a site with no Square app would be
    # asking them to queue for something nobody can be given. The dev .env carries sandbox
    # credentials and .env.example -- which is the whole of CI's .env -- carries none, so a test
    # that leaves them unpinned is really a test of whichever .env it ran under.
    @override_settings(SQUARE_APPLICATION_ID="sq0idp-x", SQUARE_CLIENT_SECRET="sq0csp-x")
    def test_the_clubs_payment_settings_do_not_show_a_bare_square_heading(self):
        """The fourth surface, and the worst-looking one.

        square_seller.html sends a club organizer here ("the club's connected Square account is
        used ... set on the club's membership settings page"), and what they used to find was the
        word "Square" with nothing whatsoever underneath it -- the connect button was inside an
        ``{% elif %}`` with no ``{% else %}``. Everyone who can open this page is a club admin with
        permission_edit_club or permission_money, so there is no question of whether they have a
        use for it.
        """
        html = self._club_settings_html()
        self.assertIn("reviewed before they're switched on", html)
        self.assertIn("Contact us and request access", html)

    @override_settings(SQUARE_APPLICATION_ID="sq0idp-x", SQUARE_CLIENT_SECRET="sq0csp-x")
    def test_the_clubs_payment_settings_still_connect_once_enabled(self):
        self.user.userdata.square_enabled = True
        self.user.userdata.save()
        html = self._club_settings_html()
        self.assertIn("Connect a Square account for this club", html)
        self.assertNotIn("Contact us and request access", html)

    @override_settings(SQUARE_APPLICATION_ID="", SQUARE_CLIENT_SECRET="")
    def test_a_site_with_no_square_app_says_that_instead_of_offering_the_queue(self):
        """The other half of the pin above, and the reason the gate disclosure sits under it."""
        html = self._club_settings_html()
        self.assertIn("Square isn't configured on this site", html)
        self.assertNotIn("Contact us and request access", html)

    def test_the_mailto_covers_a_club_as_well_as_an_auction(self):
        # The same property is now linked from a club page, so its body may not say "my auction".
        self.assertIn("auction+or+club", self.user.userdata.square_access_request_mailto_query)


class SquareCallbackReturnToAppTests(StandardTestCase):
    """TTP-1/TTP-7 — end the OAuth round trip with a confirmation page, not a dead deep link."""

    def setUp(self):
        super().setUp()
        self.user.userdata.square_enabled = True
        self.user.userdata.save()
        self.client.force_login(self.user)

    def _connect(self, **params):
        return self.client.get(reverse("square_connect"), params)

    def _callback_ok(self):
        """Drive the callback with Square's SDK stubbed out at the OAuth exchange."""
        result = SimpleNamespace(access_token="tok", refresh_token="rtok", expires_at=None, merchant_id="MID")
        merchant = MagicMock()
        merchant.merchants.get.return_value = SimpleNamespace(owner_email="m@example.com", currency="USD")
        client = MagicMock()
        client.o_auth.obtain_token.return_value = result
        with patch("square.Square", side_effect=[client, merchant]):
            return self.client.get(
                reverse("square_callback"),
                {"code": "c", "state": self.user.userdata.unsubscribe_link},
            )

    def test_web_flow_still_redirects(self):
        self._connect()
        response = self._callback_ok()
        self.assertEqual(response.status_code, 302)

    def test_app_flow_gets_the_confirmation_page(self):
        self._connect(return_to_app="1")
        response = self._callback_ok()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auctions/square_connected_app.html")

    def test_a_session_the_app_opened_is_enough(self):
        """The in-app browser view sends Safari's User-Agent, so the session is the only signal."""
        from auctions.mobile.services.web_session import mark_session_opened_by_app

        session = self.client.session
        mark_session_opened_by_app(session)
        session.save()
        self._connect()
        response = self._callback_ok()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auctions/square_connected_app.html")

    def test_no_dead_deep_link_back(self):
        """Nothing receives fishauctions:// outside the shell's WebView, and this page renders in
        an in-app browser view -- a different process the shell never sees.  See Part TTP-7."""
        self._connect(return_to_app="1")
        self.assertNotContains(self._callback_ok(), "fishauctions://")

    def test_the_app_flow_redirects_to_the_auth_session_scheme(self):
        """TTP-7 -- the browser view has to close itself, not wait to be dismissed.

        Seller onboarding runs in ASWebAuthenticationSession (Chrome Auth Tab on Android), which
        ends the moment it sees its callback scheme. Offering a link instead of redirecting is what
        made this read, on camera, as the app handing the merchant to a website and abandoning them.
        """
        self._connect(return_to_app="1")
        self.assertContains(self._callback_ok(), "fishauctions-oauth://square-connected")

    def test_the_web_flow_never_sees_the_callback_scheme(self):
        # The redirect is gated on the same session_opened_by_app branch as the whole page: a
        # merchant connecting Square in a desktop browser must still land on their auction.
        self._connect()
        self.assertNotContains(self._callback_ok(), "fishauctions-oauth://", status_code=302)

    def test_the_done_instruction_survives_the_redirect(self):
        """The fallback for a session that doesn't complete -- an older build, or a plain browser
        view. It names the control the system actually draws."""
        self._connect(return_to_app="1")
        self.assertContains(self._callback_ok(), "Done")


class PaymentAuthorizationEndpointTests(StandardTestCase):
    """TTP-3 — GET /api/mobile/payments/authorization/."""

    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-payment-authorization")
        self.buyer = User.objects.create_user("ttpbuyer", "ttpb@example.com", "pw")

    def _seller_for(self, user, **kwargs):
        defaults = {
            "square_merchant_id": "MID",
            "access_token": "seller-tok",
            "scopes": "PAYMENTS_WRITE_IN_PERSON",
        }
        return SquareSeller.objects.create(user=user, **{**defaults, **kwargs})

    def test_buyer_is_not_eligible_and_gets_no_credentials(self):
        """The gate that stands between a signed-in buyer and a merchant-wide OAuth token."""
        resp = self.client.get(self.url, **_bearer(self.buyer))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["eligible"])
        self.assertFalse(body["can_accept_terms"])
        self.assertNotIn("access_token", body)
        self.assertIn("Square", body["message"])

    def test_admin_with_a_connected_seller_gets_credentials(self):
        self._seller_for(self.user)
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        with patch.object(SquareSeller, "get_location_id", return_value="LOC1"):
            resp = self.client.get(self.url, **_bearer(self.user))
        body = resp.json()
        self.assertTrue(body["eligible"])
        self.assertTrue(body["can_accept_terms"])
        self.assertEqual(body["access_token"], "seller-tok")
        self.assertEqual(body["location_id"], "LOC1")

    def test_admin_without_a_seller_is_eligible_but_gets_nothing_to_warm_up_with(self):
        # A valid, handled state: the app shows the setup UI and skips the warm-up.
        resp = self.client.get(self.url, **_bearer(self.user))
        body = resp.json()
        self.assertTrue(body["eligible"])
        self.assertNotIn("access_token", body)
        self.assertNotIn("location_id", body)

    def test_legacy_seller_without_the_in_person_scope_gets_no_credentials(self):
        """A pre-Tap-to-Pay token would fail authorize() with an opaque Square error."""
        self._seller_for(self.user, scopes="")
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        resp = self.client.get(self.url, **_bearer(self.user))
        body = resp.json()
        self.assertTrue(body["eligible"])
        self.assertNotIn("access_token", body)

    def test_club_auction_hands_out_the_club_token_not_the_creators(self):
        """Same routing as create: a club's money never lands in an individual's Square account."""
        club = Club.objects.create(name="TTP Club")
        club_owner = User.objects.create_user("ttpclubowner", "ttpco@example.com", "pw")
        self._seller_for(self.user)  # the creator's personal account, to compete with the club's
        club_seller = self._seller_for(club_owner, club=club, square_merchant_id="CLUB", access_token="club-tok")
        self.in_person_auction.club = club
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()

        with patch.object(SquareSeller, "get_location_id", return_value="LOC1"):
            body = self.client.get(self.url, **_bearer(self.user)).json()

        self.assertEqual(body["access_token"], "club-tok")
        self.assertEqual(body["seller_name"], "TTP Club")
        del club_seller

    def test_club_money_manager_is_eligible(self):
        """Requirement 3.8: whoever may accept Apple's terms is whoever may take payments."""
        club = Club.objects.create(name="Money Club")
        manager = User.objects.create_user("moneyman", "mm@example.com", "pw")
        ClubMember.objects.create(club=club, user=manager, permission_money=True)
        resp = self.client.get(self.url, **_bearer(manager))
        self.assertTrue(resp.json()["eligible"])

    def test_falls_back_to_a_recent_auction_when_none_has_been_used(self):
        self._seller_for(self.user)
        self.assertIsNone(self.user.userdata.last_auction_used)
        with patch.object(SquareSeller, "get_location_id", return_value="LOC1"):
            body = self.client.get(self.url, **_bearer(self.user)).json()
        self.assertEqual(body["access_token"], "seller-tok")

    def test_deleted_last_auction_is_ignored(self):
        self._seller_for(self.user)
        self.online_auction.is_deleted = True
        self.online_auction.save()
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        with patch.object(SquareSeller, "get_location_id", return_value="LOC1"):
            resp = self.client.get(self.url, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["eligible"])

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_a_web_session_is_not_enough(self):
        # Mobile endpoints exclude session auth on purpose: this one hands out a seller token.
        self.client.force_login(self.user)
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_no_token_is_issued_when_the_seller_token_cannot_be_refreshed(self):
        self._seller_for(self.user)
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        with patch.object(SquareSeller, "get_valid_access_token", return_value=None):
            body = self.client.get(self.url, **_bearer(self.user)).json()
        self.assertTrue(body["eligible"])
        self.assertNotIn("access_token", body)


class PaymentAuthorizationIsolationTests(StandardTestCase):
    """A user with no connection to any auction or club must never be eligible."""

    def test_stranger_is_not_eligible(self):
        stranger = User.objects.create_user("stranger", "st@example.com", "pw")
        body = self.client.get(reverse("mobile-payment-authorization"), **_bearer(stranger)).json()
        self.assertFalse(body["eligible"])

    def test_plain_attendee_is_not_eligible(self):
        """An AuctionTOS without is_admin is a buyer, not a cashier."""
        attendee = User.objects.create_user("attendee", "at@example.com", "pw")
        AuctionTOS.objects.create(user=attendee, auction=self.online_auction, pickup_location=self.location)
        body = self.client.get(reverse("mobile-payment-authorization"), **_bearer(attendee)).json()
        self.assertFalse(body["eligible"])

    def test_plain_club_member_is_not_eligible(self):
        club = Club.objects.create(name="Plain Club")
        member = User.objects.create_user("plainmember", "pm@example.com", "pw")
        ClubMember.objects.create(club=club, user=member)
        body = self.client.get(reverse("mobile-payment-authorization"), **_bearer(member)).json()
        self.assertFalse(body["eligible"])

    def test_auction_admin_tos_is_eligible(self):
        admin = User.objects.create_user("tosadmin", "ta@example.com", "pw")
        AuctionTOS.objects.create(user=admin, auction=self.online_auction, pickup_location=self.location, is_admin=True)
        body = self.client.get(reverse("mobile-payment-authorization"), **_bearer(admin)).json()
        self.assertTrue(body["eligible"])


class ReceiptUrlTests(StandardTestCase):
    """TTP-4 — requirement 5.10, a receipt the customer can actually be sent."""

    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user("receiptbuyer", "rb2@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.pay_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.pay_invoice)
        self.pay_invoice.refresh_from_db()

    def _confirm(self, receipt_url):
        payment = SimpleNamespace(
            id="PAY9",
            status="COMPLETED",
            receipt_number="RC9",
            receipt_url=receipt_url,
            amount_money=SimpleNamespace(amount=2000, currency=self.pay_invoice.currency),
            location_id="LOC1",
            reference_id=str(self.pay_invoice.pk),
        )
        seller = MagicMock()
        seller.get_valid_access_token.return_value = "tok"
        seller.get_location_id.return_value = "LOC1"
        seller.get_square_client.return_value.payments.get.return_value = SimpleNamespace(errors=None, payment=payment)
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views._ensure_invoice_renewal_state"),
            patch("auctions.views._process_invoice_membership_renewal"),
        ):
            return PaymentService.confirm_mobile_payment(
                invoice_pk=self.pay_invoice.pk,
                payment_id="PAY9",
                idempotency_key="idem-9",
                user=self.admin_user,
            )

    def test_receipt_url_is_returned(self):
        url = "https://squareup.com/receipt/preview/PAY9"
        self.assertEqual(self._confirm(url)["receipt_url"], url)

    def test_missing_receipt_url_is_null_rather_than_empty(self):
        # The app treats a missing link as "no receipt to share"; "" would render as a broken one.
        self.assertIsNone(self._confirm("")["receipt_url"])


class TapToPayAttemptTests(StandardTestCase):
    """TTP-10 — the attempt record, and what it replaced.

    ``create`` used to return a *stable* per-invoice ``idempotency_key`` whose comment described
    Square's server-side dedup key. The app passes that value to the Mobile Payments SDK as
    ``paymentAttemptId``, which is a different concept with the opposite behaviour: it names one
    attempt, and a repeat is an error. Found on hardware — a card was declined, the cashier
    retried, and Square's own UI said "something went wrong, please contact the developer of this
    app — error code payment_attempt_id_reused". Declines are routine, so that made Tap to Pay fail
    precisely when it is needed, and nothing was ever being deduplicated in the first place.

    Making the key per-create fixes the decline, and gives up an accidental protection: a charge
    captured on-device whose ``confirm`` never arrives (app killed, network dropped) leaves the
    invoice unpaid with nothing to stop a second tap charging the card again. These tests are that
    protection, done on purpose — and the endpoint that keeps it from becoming the same failure one
    step later.
    """

    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user("attemptbuyer", "ab@example.com", "pw")
        tos = AuctionTOS.objects.create(user=self.buyer, auction=self.online_auction, pickup_location=self.location)
        self.invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=self.invoice)
        self.invoice.refresh_from_db()
        self.create_url = reverse("mobile-payment-create")
        self.close_url = reverse("mobile-payment-attempt-close")

    def _seller(self):
        seller = MagicMock()
        seller.get_valid_access_token.return_value = "tok"
        seller.get_location_id.return_value = "LOC1"
        seller.supports_tap_to_pay = True
        return seller

    def _create(self, user=None):
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._seller()):
            return self.client.post(
                self.create_url, {"invoice_pk": self.invoice.pk}, **_bearer(user or self.admin_user)
            )

    def _confirm_ok(self):
        """Drive confirm with a Square payment that verifies against this invoice."""
        payment = SimpleNamespace(
            id="PAYA",
            status="COMPLETED",
            receipt_number="RC1",
            receipt_url="",
            amount_money=SimpleNamespace(amount=2000, currency=self.invoice.currency),
            location_id="LOC1",
            reference_id=str(self.invoice.pk),
        )
        seller = self._seller()
        seller.get_square_client.return_value.payments.get.return_value = SimpleNamespace(errors=None, payment=payment)
        with (
            patch.object(PaymentService, "_get_seller_for_invoice", return_value=seller),
            patch("auctions.views._ensure_invoice_renewal_state"),
            patch("auctions.views._process_invoice_membership_renewal"),
        ):
            return PaymentService.confirm_mobile_payment(
                invoice_pk=self.invoice.pk, payment_id="PAYA", idempotency_key="ignored", user=self.admin_user
            )

    def test_create_records_an_open_attempt(self):
        from auctions.models import TapToPayAttempt

        body = self._create().json()
        attempt = TapToPayAttempt.objects.get(attempt_id=body["attempt_id"])
        self.assertEqual(attempt.invoice, self.invoice)
        self.assertEqual(attempt.created_by, self.admin_user)
        self.assertEqual(attempt.outcome, "")  # open
        self.assertIsNone(attempt.closed_at)

    def test_a_second_create_is_refused_while_one_is_open(self):
        self._create()
        response = self._create()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "attempt_in_progress")
        # Written for somebody standing at a checkout desk, and shown by the app verbatim.
        self.assertIn("may already have been charged", body["detail"])
        self.assertIn("Square", body["detail"])

    def test_a_declined_card_can_be_retried_once_the_app_closes_the_attempt(self):
        """The failure this whole part exists to remove, moved one step later.

        Declines are routine. If a declined attempt stayed open, create would refuse the retry and
        the cashier would be blocked from the one action that is definitely correct.
        """
        attempt_id = self._create().json()["attempt_id"]
        closed = self.client.post(
            self.close_url, {"attempt_id": attempt_id, "outcome": "failed"}, **_bearer(self.admin_user)
        )
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(self._create().status_code, 200)

    def test_a_canceled_attempt_is_closed_the_same_way(self):
        attempt_id = self._create().json()["attempt_id"]
        response = self.client.post(
            self.close_url, {"attempt_id": attempt_id, "outcome": "canceled"}, **_bearer(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "canceled")

    def test_closing_an_already_closed_attempt_is_not_an_error(self):
        # The app calls this best-effort and never shows a cashier a bookkeeping error; confirm may
        # also have won the race and closed it as captured.
        attempt_id = self._create().json()["attempt_id"]
        body = {"attempt_id": attempt_id, "outcome": "canceled"}
        self.client.post(self.close_url, body, **_bearer(self.admin_user))
        self.assertEqual(self.client.post(self.close_url, body, **_bearer(self.admin_user)).status_code, 200)

    def test_an_unknown_attempt_is_a_404_the_app_can_ignore(self):
        # What an older deployment (or an attempt that already aged out) looks like.
        response = self.client.post(
            self.close_url, {"attempt_id": "taptopay-inv-1-deadbeef", "outcome": "failed"}, **_bearer(self.admin_user)
        )
        self.assertEqual(response.status_code, 404)

    def test_a_capture_may_not_be_reported_here(self):
        # Only confirm may say a card was charged: it is the half that verifies against Square.
        attempt_id = self._create().json()["attempt_id"]
        response = self.client.post(
            self.close_url, {"attempt_id": attempt_id, "outcome": "captured"}, **_bearer(self.admin_user)
        )
        self.assertEqual(response.status_code, 400)

    def test_the_buyer_cannot_close_an_attempt(self):
        attempt_id = self._create().json()["attempt_id"]
        response = self.client.post(
            self.close_url, {"attempt_id": attempt_id, "outcome": "failed"}, **_bearer(self.buyer)
        )
        self.assertEqual(response.status_code, 403)

    def test_closing_requires_jwt(self):
        attempt_id = self._create().json()["attempt_id"]
        self.assertIn(
            self.client.post(self.close_url, {"attempt_id": attempt_id, "outcome": "failed"}).status_code, (401, 403)
        )

    def test_confirm_closes_the_attempt_as_captured(self):
        from auctions.models import TapToPayAttempt

        attempt_id = self._create().json()["attempt_id"]
        self._confirm_ok()
        attempt = TapToPayAttempt.objects.get(attempt_id=attempt_id)
        self.assertEqual(attempt.outcome, "captured")
        self.assertEqual(attempt.payment_id, "PAYA")
        self.assertIsNotNone(attempt.closed_at)

    def test_an_attempt_ages_out_so_a_wedged_row_cannot_strand_an_invoice(self):
        from auctions.models import TapToPayAttempt

        attempt_id = self._create().json()["attempt_id"]
        stale = timezone.now() - PaymentService.OPEN_ATTEMPT_TIMEOUT - timedelta(seconds=1)
        # auto_now_add ignores an assigned value, so age the row with an update().
        TapToPayAttempt.objects.filter(attempt_id=attempt_id).update(createdon=stale)
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(TapToPayAttempt.objects.get(attempt_id=attempt_id).outcome, "expired")

    def test_an_attempt_on_one_invoice_does_not_block_another(self):
        other_tos = AuctionTOS.objects.create(
            user=User.objects.create_user("attemptbuyer2", "ab2@example.com", "pw"),
            auction=self.online_auction,
            pickup_location=self.location,
        )
        other_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=other_tos)
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=other_invoice)
        other_invoice.refresh_from_db()
        self._create()
        with patch.object(PaymentService, "_get_seller_for_invoice", return_value=self._seller()):
            response = self.client.post(self.create_url, {"invoice_pk": other_invoice.pk}, **_bearer(self.admin_user))
        self.assertEqual(response.status_code, 200)

    def test_no_attempt_is_opened_when_create_refuses(self):
        from auctions.models import TapToPayAttempt

        # A buyer reaching create is a 403; a row here would let a rejected caller block the desk.
        self._create(user=self.buyer)
        self.assertEqual(TapToPayAttempt.objects.filter(invoice=self.invoice).count(), 0)


class LaunchAnnouncementTests(StandardTestCase):
    """TTP-5 — marketing requirements 6.1 (launch email) and 6.3 (push).

    The copy for both must come from Apple's toolkit; the guide forbids writing your own. So the
    interesting behaviour here is the refusal to send without it — a plausible-looking default would
    ship and only fail at review.
    """

    def setUp(self):
        super().setUp()
        from auctions.models import MobileDevice

        SquareSeller.objects.create(
            user=self.user,
            square_merchant_id="MID",
            access_token="tok",
            scopes="PAYMENTS_WRITE_IN_PERSON",
        )
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        MobileDevice.objects.create(user=self.user, device_uuid="11111111-1111-4111-8111-111111111111", platform="ios")
        # An Android merchant: eligible in every other way, but Tap to Pay on iPhone isn't for them.
        self.android_merchant = User.objects.create_user("droid", "droid@example.com", "pw")
        AuctionTOS.objects.create(
            user=self.android_merchant,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            is_admin=True,
        )
        MobileDevice.objects.create(
            user=self.android_merchant, device_uuid="22222222-2222-4222-8222-222222222222", platform="android"
        )

    def _run(self, **kwargs):
        from django.core.management import call_command

        out = StringIO()
        call_command("tap_to_pay_launch_announcement", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _make_template(self):
        from post_office.models import EmailTemplate

        EmailTemplate.objects.create(
            name="tap_to_pay_launch_email", subject="Tap to Pay on iPhone", content="toolkit copy"
        )

    def test_refuses_to_send_without_the_toolkit_email_template(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as caught:
            self._run(push_title="t", push_body="b")
        self.assertIn("Marketing Guide and Toolkit", str(caught.exception))

    def test_refuses_to_send_without_the_toolkit_push_copy(self):
        from django.core.management.base import CommandError

        self._make_template()
        with self.assertRaises(CommandError) as caught:
            self._run()
        self.assertIn("Value Proposition", str(caught.exception))

    def test_dry_run_reports_instead_of_refusing(self):
        output = self._run(dry_run=True)
        self.assertIn("Marketing Guide and Toolkit", output)
        self.assertIn("[DRY RUN]", output)

    def test_only_iphone_merchants_are_eligible(self):
        from auctions.management.commands.tap_to_pay_launch_announcement import Command

        eligible = list(Command.eligible_users())
        self.assertIn(self.user, eligible)
        self.assertNotIn(self.android_merchant, eligible)

    def test_buyers_are_not_eligible(self):
        from auctions.management.commands.tap_to_pay_launch_announcement import Command
        from auctions.models import MobileDevice

        buyer = User.objects.create_user("launchbuyer", "lb@example.com", "pw")
        AuctionTOS.objects.create(user=buyer, auction=self.in_person_auction, pickup_location=self.in_person_location)
        MobileDevice.objects.create(user=buyer, device_uuid="33333333-3333-4333-8333-333333333333", platform="ios")
        self.assertNotIn(buyer, list(Command.eligible_users()))

    def test_sends_once_and_only_once(self):
        from auctions.models import PushNotificationSent

        self._make_template()
        with patch("auctions.tasks.send_push_to_user.delay") as push:
            self._run(push_title="t", push_body="b")
            self.assertEqual(push.call_count, 1)
            self.assertEqual(PushNotificationSent.objects.filter(category="tap_to_pay_launch").count(), 1)
            # Re-running is a no-op — nobody is announced to twice.
            self._run(push_title="t", push_body="b")
            self.assertEqual(push.call_count, 1)


class SetupChecklistTests(TestCase):
    """The setup instructions for the new providers live on the admin checklist, not in a doc file.

    A doc nobody opens is worse than no doc; the checklist is the page an admin already visits, and
    it can say which of these *this* install has actually done.
    """

    def setUp(self):
        self.superuser = User.objects.create_superuser("checkadmin", "check@example.com", "pw")
        self.client.force_login(self.superuser)
        ip_patcher = patch("auctions.views.get_server_public_ip", return_value="203.0.113.7")
        ip_patcher.start()
        self.addCleanup(ip_patcher.stop)

    def _items(self):
        response = self.client.get(reverse("admin_setup_checklist"))
        self.assertEqual(response.status_code, 200)
        return response, {item["name"]: item for item in response.context["setup_items"]}

    def test_sections_are_present(self):
        response, _ = self._items()
        self.assertContains(response, "Sign in with Apple")
        self.assertContains(response, "Facebook Login")

    @override_settings(APPLE_SIGN_IN_BUNDLE_ID="", APPLE_SIGN_IN_SERVICES_ID="", APPLE_SIGN_IN_PRIVATE_KEY="")
    def test_unconfigured_apple_needs_setup(self):
        _, items = self._items()
        self.assertFalse(items["Sign in with Apple in the mobile app"]["configured"])
        self.assertFalse(items["Sign in with Apple on the website"]["configured"])

    @override_settings(APPLE_SIGN_IN_BUNDLE_ID="com.fishauctions.app")
    def test_bundle_id_alone_completes_the_app_item(self):
        # Deliberate: verifying an Apple token needs only Apple's public keys, so the app half is
        # genuinely done at that point even with no Services ID and no key.
        _, items = self._items()
        self.assertTrue(items["Sign in with Apple in the mobile app"]["configured"])
        self.assertFalse(items["Sign in with Apple on the website"]["configured"])

    @override_settings(
        APPLE_SIGN_IN_TEAM_ID="TEAM",
        APPLE_SIGN_IN_KEY_ID="KEY",
        APPLE_SIGN_IN_PRIVATE_KEY="pem",
        APPLE_SIGN_IN_BUNDLE_ID="com.fishauctions.app",
    )
    def test_revocation_item_tracks_the_team_key(self):
        _, items = self._items()
        self.assertTrue(items["Account deletion & Hide My Email"]["configured"])

    def test_hide_my_email_warning_is_stated_plainly(self):
        """The failure is silent, so the page has to say so — that's the whole reason it's an item."""
        _, items = self._items()
        text = items["Account deletion & Hide My Email"]["what_it_does"]
        self.assertIn("privaterelay.appleid.com", text)
        self.assertIn("without a bounce", text)

    def test_apple_callback_url_has_no_accounts_prefix(self):
        """allauth is mounted at the site root here, so the usual /accounts/ path is wrong."""
        _, items = self._items()
        steps = " ".join(items["Sign in with Apple on the website"]["setup_steps"])
        self.assertIn(reverse("apple_callback"), steps)
        self.assertNotIn("/accounts/apple/", steps)

    @override_settings(FACEBOOK_APP_ID="123", FACEBOOK_APP_SECRET="")
    def test_facebook_needs_both_halves(self):
        _, items = self._items()
        self.assertFalse(items["Facebook Login"]["configured"])

    @override_settings(FACEBOOK_APP_ID="123", FACEBOOK_APP_SECRET="s")
    def test_facebook_configured(self):
        _, items = self._items()
        self.assertTrue(items["Facebook Login"]["configured"])

    @override_settings(SQUARE_APPLICATION_ID="sq0idp-x")
    def test_tap_to_pay_items_appear_once_square_exists(self):
        _, items = self._items()
        self.assertIn("Apple's publishing entitlement", items)
        self.assertFalse(items["Launch email & push notification"]["configured"])

    @override_settings(SQUARE_APPLICATION_ID="")
    def test_tap_to_pay_hidden_without_square(self):
        # Tap to Pay charges through Square; the section is noise on an install that has none.
        _, items = self._items()
        self.assertNotIn("Apple's publishing entitlement", items)

    @override_settings(SQUARE_APPLICATION_ID="sq0idp-x")
    def test_launch_item_is_done_once_the_toolkit_template_exists(self):
        from post_office.models import EmailTemplate

        EmailTemplate.objects.create(name="tap_to_pay_launch_email", subject="s", content="toolkit copy")
        _, items = self._items()
        self.assertTrue(items["Launch email & push notification"]["configured"])

    @override_settings(SQUARE_APPLICATION_ID="sq0idp-x")
    def test_entitlement_item_does_not_claim_to_know_apples_answer(self):
        """It renders a green badge because there's no setting — say so, or it reads as a claim."""
        _, items = self._items()
        self.assertIn(
            "can't tell whether Apple has granted it", items["Apple's publishing entitlement"]["what_it_does"]
        )

    def test_ampersands_in_names_are_literal(self):
        # `name` is auto-escaped by the template, so an HTML entity here renders as visible text.
        _, items = self._items()
        for name in items:
            self.assertNotIn("&amp;", name)


class LatestAdminAuctionTests(StandardTestCase):
    """Which auction the warm-up resolves the seller from."""

    def test_prefers_the_auction_the_app_is_working_in(self):
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self.assertEqual(PaymentService._latest_admin_auction(self.user), self.in_person_auction)

    def test_ignores_a_last_used_auction_the_user_does_not_administer(self):
        outsider = User.objects.create_user("outsider", "out@example.com", "pw")
        outsider.userdata.last_auction_used = self.in_person_auction
        outsider.userdata.save()
        self.assertIsNone(PaymentService._latest_admin_auction(outsider))

    def test_returns_none_for_a_user_with_no_auctions(self):
        stranger = User.objects.create_user("noauctions", "na@example.com", "pw")
        self.assertIsNone(PaymentService._latest_admin_auction(stranger))

    def test_falls_back_to_the_newest_auction_they_administer(self):
        found = PaymentService._latest_admin_auction(self.user)
        self.assertIn(found, Auction.objects.filter(created_by=self.user))


class TapToPayAwarenessOfferTests(StandardTestCase):
    """TTP-6 — the auction ribbon, not a URL prefix, decides when to ask for Apple's awareness modal.

    The app used to infer it from ``/auctions/`` plus "the backend once issued this user live Square
    credentials", which is an approximation of the only question that matters — *is the website
    showing its own Square card to this user on this page?* — and getting it wrong put the modal in
    front of an organizer on an unrelated page. Only the server can answer that, so the page asks.
    """

    HANDLER = "callHandler('tapToPayOffer')"

    def setUp(self):
        super().setUp()
        self.url = reverse("auction_main", kwargs={"slug": self.in_person_auction.slug})
        self.seller = SquareSeller.objects.create(
            user=self.user,
            square_merchant_id="MERCHANT1",
            scopes="PAYMENTS_WRITE PAYMENTS_WRITE_IN_PERSON",
        )
        self.client.force_login(self.user)

    def _html(self, user_agent=IOS_UA):
        return self.client.get(self.url, HTTP_USER_AGENT=user_agent).content.decode()

    def test_admin_in_the_app_with_in_person_square_is_offered_the_modal(self):
        self.assertIn(self.HANDLER, self._html())

    def test_never_on_the_web(self):
        """There is no handler in a browser, and no modal to show."""
        self.assertNotIn(self.HANDLER, self._html("Mozilla/5.0"))

    def test_not_offered_without_a_connected_square_account(self):
        self.seller.delete()
        self.assertNotIn(self.HANDLER, self._html())

    def test_not_offered_to_a_legacy_connection_that_cannot_take_a_card_in_the_room(self):
        # Connected before the in-person scope existed: a merchant id but no Tap to Pay, so the modal
        # would open onto a dead end. Refreshing the token keeps the original scopes, so this is not
        # a state that fixes itself.
        self.seller.scopes = "PAYMENTS_WRITE"
        self.seller.save()
        self.assertNotIn(self.HANDLER, self._html())

    def test_not_offered_to_someone_who_does_not_run_this_auction(self):
        self.client.force_login(self.user_who_does_not_join)
        self.assertNotIn(self.HANDLER, self._html())


class OffersTapToPayPropertyTests(StandardTestCase):
    """The property behind TTP-6, exercised directly: it is what the ribbon and nothing else reads."""

    def test_no_seller_at_all(self):
        self.assertFalse(self.in_person_auction.offers_tap_to_pay)

    def test_seller_with_the_in_person_scope(self):
        SquareSeller.objects.create(user=self.user, square_merchant_id="M1", scopes="PAYMENTS_WRITE_IN_PERSON")
        self.assertTrue(Auction.objects.get(pk=self.in_person_auction.pk).offers_tap_to_pay)

    def test_seller_connected_but_never_authorised_in_person(self):
        SquareSeller.objects.create(user=self.user, square_merchant_id="M1", scopes="PAYMENTS_WRITE")
        self.assertFalse(Auction.objects.get(pk=self.in_person_auction.pk).offers_tap_to_pay)

    def test_oauth_started_but_never_finished(self):
        """No merchant id means the OAuth handshake never completed; there is no account to charge to."""
        SquareSeller.objects.create(user=self.user, square_merchant_id="", scopes="PAYMENTS_WRITE_IN_PERSON")
        self.assertFalse(Auction.objects.get(pk=self.in_person_auction.pk).offers_tap_to_pay)
