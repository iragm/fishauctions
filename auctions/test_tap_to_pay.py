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

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.middleware import MobileAppMiddleware
from auctions.mobile.services.payments import PaymentService
from auctions.models import Auction, AuctionTOS, Club, ClubMember, Invoice, InvoiceAdjustment, SquareSeller
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


class TapToPayButtonCopyTests(StandardTestCase):
    """TTP-2 — requirement 5.4 (approved wording) and 5.5 (no unapproved iconography)."""

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

    def test_button_comes_before_the_qr_block(self):
        """5.2 — with several payment options, Tap to Pay sits at the top of the list."""
        html = self._html(IOS_UA)
        self.assertLess(html.index(f"fishauctions://pay/{self.invoice.pk}"), html.index("View or adjust invoice"))


class SquareOnboardingInAppTests(StandardTestCase):
    """TTP-1 — the connect links must render inside the app, with no "use a browser" banner."""

    def setUp(self):
        super().setUp()
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


class SquareCallbackReturnToAppTests(StandardTestCase):
    """TTP-1 nice-to-have — end the OAuth round trip with a way back into the app."""

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

    def test_app_flow_offers_a_deep_link_back(self):
        self._connect(return_to_app="1")
        response = self._callback_ok()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fishauctions://square-connected")

    def test_a_session_the_app_opened_is_enough(self):
        """The in-app browser view sends Safari's User-Agent, so the session is the only signal."""
        from auctions.mobile.services.web_session import mark_session_opened_by_app

        session = self.client.session
        mark_session_opened_by_app(session)
        session.save()
        self._connect()
        self.assertContains(self._callback_ok(), "fishauctions://square-connected")


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
