"""The PayPal and Square connect/callback views, PayPal checkout (order creation and capture), the
PayPalAPIMixin's error handling, the club subscription webhook's real verification round trip, the
Square webhook's refusals, and LotConsumer's chat handling. Every PayPal and Square call is mocked
where it is looked up; the consumer is driven directly with a fake channel layer.
"""

import datetime
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.consumers import LotConsumer
from auctions.models import (
    Club,
    ClubHistory,
    ClubMember,
    Invoice,
    InvoicePayment,
    LotHistory,
    PayPalSeller,
    SquareSeller,
    UserBan,
)
from auctions.tests import StandardTestCase, patch_views
from auctions.views.base import PAYMENT_OAUTH_CLUB_SESSION_KEY
from auctions.views.payments import (
    CreatePayPalOrderView,
    PayPalAPIMixin,
    PayPalCallbackView,
    PayPalConnectView,
    PayPalRequestError,
    PayPalSuccessView,
)

PAYPAL_SETTINGS = {
    "PAYPAL_API_BASE": "https://api-m.sandbox.paypal.com",
    "PAYPAL_CLIENT_ID": "site-client-id",
    "PAYPAL_SECRET": "site-secret",
    "PARTNER_MERCHANT_ID": "PARTNER-1",
    "PAYPAL_BN_CODE": "BN-CODE",
    "PAYPAL_PLATFORM_FEE": Decimal(0),
}


def _json_response(payload, status=200, debug_id=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {"Paypal-Debug-Id": debug_id}
    resp.text = json.dumps(payload)
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _money_club(name, user, **kwargs):
    club = Club.objects.create(name=name, **kwargs)
    ClubMember.objects.create(club=club, user=user, name=name + " treasurer", permission_money=True)
    return club


@override_settings(**PAYPAL_SETTINGS)
class PayPalAPIMixinErrorTests(SimpleTestCase):
    def _mixin(self):
        return PayPalAPIMixin()

    def test_http_error_raises_with_debug_id_and_masks_token_in_log(self):
        token = _json_response({"access_token": "tok-VERY-SECRET"})
        failure = _json_response({"name": "UNPROCESSABLE"}, status=422, debug_id="DBG-1")
        mixin = self._mixin()
        with (
            patch("auctions.views.payments.requests.post", return_value=token),
            patch("auctions.views.payments.requests.request", return_value=failure),
            self.assertLogs("auctions.views.payments", level="ERROR") as logs,
        ):
            with self.assertRaises(PayPalRequestError) as ctx:
                mixin.get_from_paypal("v1/something")
        self.assertIn("DBG-1", str(ctx.exception))
        self.assertEqual(mixin.paypal_debug, "DBG-1")
        self.assertNotIn("tok-VERY-SECRET", "\n".join(logs.output))

    def test_no_response_raises_paypal_request_error(self):
        token = _json_response({"access_token": "tok"})
        with (
            patch("auctions.views.payments.requests.post", return_value=token),
            patch("auctions.views.payments.requests.request", side_effect=requests.ConnectionError("down")),
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            with self.assertRaises(PayPalRequestError):
                self._mixin().post_to_paypal("v2/checkout/orders", {})

    def test_token_failure_raises_before_any_api_call(self):
        with (
            patch("auctions.views.payments.requests.post", side_effect=requests.ConnectionError("down")),
            patch("auctions.views.payments.requests.request") as mock_request,
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            with self.assertRaises(PayPalRequestError):
                self._mixin().get_from_paypal("v1/something")
        mock_request.assert_not_called()

    def test_club_credentials_authenticate_and_withhold_our_bn_code(self):
        mixin = self._mixin()
        mixin.club_paypal_credentials = ("club-cid", "club-secret")
        with (
            patch(
                "auctions.views.payments.requests.post", return_value=_json_response({"access_token": "t"})
            ) as mock_post,
            patch("auctions.views.payments.requests.request", return_value=_json_response({"ok": 1})) as mock_request,
        ):
            self.assertEqual(mixin.get_from_paypal("v1/something"), {"ok": 1})
        self.assertEqual(mock_post.call_args.kwargs["auth"], ("club-cid", "club-secret"))
        self.assertNotIn("PayPal-Partner-Attribution-Id", mock_request.call_args.kwargs["headers"])


@override_settings(**PAYPAL_SETTINGS)
class PayPalConnectViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("paypal_connect")
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        self.client.force_login(self.user)

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_user_without_paypal_enabled_never_reaches_paypal(self):
        self.user.userdata.paypal_enabled = False
        self.user.userdata.save()
        with patch.object(PayPalConnectView, "post_to_paypal") as mock_post:
            response = self.client.get(self.url)
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        mock_post.assert_not_called()

    def test_redirects_to_paypal_action_url_with_the_users_tracking_id(self):
        data = {"links": [{"rel": "self", "href": "x"}, {"rel": "action_url", "href": "https://paypal.example/go"}]}
        with patch.object(PayPalConnectView, "post_to_paypal", return_value=data) as mock_post:
            response = self.client.get(self.url)
        self.assertRedirects(response, "https://paypal.example/go", fetch_redirect_response=False)
        payload = mock_post.call_args.args[1]
        self.assertEqual(str(payload["tracking_id"]), str(self.user.userdata.unsubscribe_link))
        self.assertTrue(payload["partner_config_override"]["return_url"].endswith(reverse("paypal_callback")))

    def test_missing_action_url_goes_home_with_error(self):
        with (
            patch("auctions.views.payments.requests.post", return_value=_json_response({"access_token": "t"})),
            patch("auctions.views.payments.requests.request", return_value=_json_response({"links": []})),
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            response = self.client.get(self.url)
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertTrue(any("Unable to start PayPal" in m for m in _messages(response)))

    def test_club_is_stashed_only_with_payment_permission(self):
        allowed = _money_club("Allowed Club", self.user)
        other = Club.objects.create(name="Not My Club")
        data = {"links": [{"rel": "action_url", "href": "https://paypal.example/go"}]}
        with patch.object(PayPalConnectView, "post_to_paypal", return_value=data):
            self.client.get(self.url, {"club": other.slug})
            self.assertNotIn(PAYMENT_OAUTH_CLUB_SESSION_KEY, self.client.session)
            self.client.get(self.url, {"club": allowed.slug})
            self.assertEqual(self.client.session[PAYMENT_OAUTH_CLUB_SESSION_KEY], allowed.slug)


@override_settings(**PAYPAL_SETTINGS)
class PayPalCallbackViewTests(StandardTestCase):
    GOOD_MERCHANT = {
        "payments_receivable": True,
        "primary_email_confirmed": True,
        "primary_email": "seller@paypal.example",
        "primary_currency": "CAD",
        "oauth_integrations": [{"integration_type": "OAUTH_THIRD_PARTY", "oauth_third_party": [{"scopes": []}]}],
    }

    def setUp(self):
        super().setUp()
        self.url = reverse("paypal_callback")
        self.client.force_login(self.user)

    def _callback(self, merchant_info=None, tracking_id=None, merchant_id="MERCH-NEW"):
        params = {"merchantId": tracking_id or self.user.userdata.unsubscribe_link, "merchantIdInPayPal": merchant_id}
        with patch.object(
            PayPalCallbackView, "get_from_paypal", return_value=merchant_info or self.GOOD_MERCHANT
        ) as mock_get:
            response = self.client.get(self.url, params)
        return response, mock_get

    def _stash(self, club):
        session = self.client.session
        session[PAYMENT_OAUTH_CLUB_SESSION_KEY] = club.slug
        session.save()

    def test_missing_ids_link_nothing(self):
        with patch.object(PayPalCallbackView, "get_from_paypal") as mock_get:
            response = self.client.get(self.url, {"merchantId": self.user.userdata.unsubscribe_link})
        self.assertEqual(response.status_code, 302)
        mock_get.assert_not_called()
        self.assertFalse(PayPalSeller.objects.exists())

    def test_tracking_id_of_another_user_is_refused(self):
        response, mock_get = self._callback(tracking_id=self.userB.userdata.unsubscribe_link)
        mock_get.assert_not_called()
        self.assertFalse(PayPalSeller.objects.exists())
        self.assertTrue(any("does not match" in m for m in _messages(response)))

    def test_merchant_that_cannot_receive_payments_is_not_linked(self):
        response, _ = self._callback({**self.GOOD_MERCHANT, "payments_receivable": False})
        self.assertFalse(PayPalSeller.objects.filter(user=self.user).exists())
        self.assertTrue(any("cannot receive payments" in m for m in _messages(response)))

    def test_merchant_without_third_party_oauth_is_not_linked(self):
        self._callback({**self.GOOD_MERCHANT, "oauth_integrations": []})
        self.assertFalse(PayPalSeller.objects.filter(user=self.user).exists())

    def test_good_merchant_is_linked(self):
        response, _ = self._callback()
        seller = PayPalSeller.objects.get(user=self.user)
        self.assertEqual(seller.paypal_merchant_id, "MERCH-NEW")
        self.assertEqual(seller.payer_email, "seller@paypal.example")
        self.assertEqual(seller.currency, "CAD")
        self.assertIsNone(seller.club)
        self.assertIn("enable_online_payments=True", response["Location"])

    def test_stashed_club_is_linked_and_replaces_its_previous_seller(self):
        club = _money_club("Paying Club", self.user)
        previous = PayPalSeller.objects.create(user=self.admin_user, club=club, paypal_merchant_id="OLD")
        self._stash(club)
        response, _ = self._callback()
        self.assertRedirects(
            response,
            reverse("club_membership_settings", kwargs={"slug": club.slug}),
            fetch_redirect_response=False,
        )
        self.assertEqual(PayPalSeller.objects.get(user=self.user).club, club)
        previous.refresh_from_db()
        self.assertIsNone(previous.club)
        actions = list(ClubHistory.objects.filter(club=club).values_list("action", flat=True))
        self.assertTrue(any(a.startswith("Replaced PayPal account") for a in actions))
        self.assertTrue(any(a.startswith("Connected PayPal account") for a in actions))

    def test_stashed_club_without_payment_permission_is_not_linked(self):
        club = Club.objects.create(name="Someone Else's Club")
        ClubMember.objects.create(club=club, user=self.user, name="Plain member")
        self._stash(club)
        response, _ = self._callback()
        self.assertIsNone(PayPalSeller.objects.get(user=self.user).club)
        self.assertNotIn(club.slug, response["Location"])
        self.assertFalse(ClubHistory.objects.filter(club=club).exists())


class SquareCallbackViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("square_callback")
        self.client.force_login(self.user)
        self.state = self.user.userdata.unsubscribe_link

    def _square_clients(self, merchant_id="MID"):
        result = SimpleNamespace(access_token="tok", refresh_token="rtok", expires_at=None, merchant_id=merchant_id)
        client = MagicMock()
        client.o_auth.obtain_token.return_value = result
        merchant = MagicMock()
        merchant.merchants.get.return_value = SimpleNamespace(owner_email="m@example.com", currency="USD")
        return client, merchant

    def test_square_error_param_links_nothing(self):
        with patch("square.Square") as mock_square:
            response = self.client.get(self.url, {"error": "access_denied", "error_description": "User said no"})
        self.assertRedirects(response, reverse("square_seller"), fetch_redirect_response=False)
        mock_square.assert_not_called()
        self.assertTrue(any("User said no" in m for m in _messages(response)))
        self.assertFalse(SquareSeller.objects.exists())

    def test_state_of_another_user_never_exchanges_the_code(self):
        with patch("square.Square") as mock_square:
            response = self.client.get(self.url, {"code": "c", "state": self.userB.userdata.unsubscribe_link})
        self.assertRedirects(response, reverse("square_seller"), fetch_redirect_response=False)
        mock_square.assert_not_called()
        self.assertFalse(SquareSeller.objects.exists())

    def test_missing_code_links_nothing(self):
        with patch("square.Square") as mock_square:
            self.client.get(self.url, {"state": self.state})
        mock_square.assert_not_called()
        self.assertFalse(SquareSeller.objects.exists())

    def test_exchange_failure_links_nothing(self):
        client = MagicMock()
        client.o_auth.obtain_token.side_effect = RuntimeError("square is down")
        with (
            patch("square.Square", return_value=client),
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            response = self.client.get(self.url, {"code": "c", "state": self.state})
        self.assertRedirects(response, reverse("square_seller"), fetch_redirect_response=False)
        self.assertFalse(SquareSeller.objects.exists())

    def test_response_without_merchant_id_links_nothing(self):
        client, merchant = self._square_clients(merchant_id=None)
        with (
            patch("square.Square", side_effect=[client, merchant]),
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            self.client.get(self.url, {"code": "c", "state": self.state})
        self.assertFalse(SquareSeller.objects.exists())

    def test_stashed_club_is_linked_and_replaces_its_previous_seller(self):
        club = _money_club("Square Club", self.user)
        previous = SquareSeller.objects.create(user=self.admin_user, club=club, square_merchant_id="OLD")
        session = self.client.session
        session[PAYMENT_OAUTH_CLUB_SESSION_KEY] = club.slug
        session.save()
        client, merchant = self._square_clients()
        with patch("square.Square", side_effect=[client, merchant]):
            response = self.client.get(self.url, {"code": "c", "state": self.state})
        self.assertRedirects(
            response,
            reverse("club_membership_settings", kwargs={"slug": club.slug}),
            fetch_redirect_response=False,
        )
        seller = SquareSeller.objects.get(user=self.user)
        self.assertEqual(seller.club, club)
        self.assertEqual(seller.square_merchant_id, "MID")
        self.assertEqual(seller.access_token, "tok")
        previous.refresh_from_db()
        self.assertIsNone(previous.club)
        self.assertTrue(ClubHistory.objects.filter(club=club, action__startswith="Connected Square account").exists())


@override_settings(**PAYPAL_SETTINGS)
class CreatePayPalOrderViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.user.userdata.is_trusted = True
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        self.online_auction.enable_online_payments = True
        self.online_auction.save()
        PayPalSeller.objects.create(user=self.user, paypal_merchant_id="MERCH-1")
        self.invoiceB.status = "UNPAID"
        self.invoiceB.save()
        self.url = reverse("create_paypal_order", kwargs={"uuid": self.invoiceB.no_login_link})
        self.invoice_page = reverse("invoice_no_login", kwargs={"uuid": self.invoiceB.no_login_link})

    def test_unknown_invoice_is_404(self):
        response = self.client.post(
            reverse("create_paypal_order", kwargs={"uuid": "00000000-0000-4000-8000-000000000000"})
        )
        self.assertEqual(response.status_code, 404)

    def test_paid_invoice_is_not_sent_to_paypal(self):
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        with patch.object(CreatePayPalOrderView, "post_to_paypal") as mock_post:
            response = self.client.post(self.url)
        self.assertRedirects(response, self.invoice_page, fetch_redirect_response=False)
        mock_post.assert_not_called()

    def test_order_is_created_for_the_balance_and_redirects_to_approval(self):
        owed = (
            Decimal("0.00") - Decimal(Invoice.objects.get(pk=self.invoiceB.pk).rounded_net_after_payments)
        ).quantize(Decimal("0.01"))
        self.assertGreater(owed, 0)
        order = {"id": "ORDER-1", "links": [{"rel": "approve", "href": "https://paypal.example/approve"}]}
        with patch.object(CreatePayPalOrderView, "post_to_paypal", return_value=order) as mock_post:
            response = self.client.post(self.url)
        self.assertRedirects(response, "https://paypal.example/approve", fetch_redirect_response=False)
        endpoint, payload = mock_post.call_args.args
        self.assertEqual(endpoint, "v2/checkout/orders")
        unit = payload["purchase_units"][0]
        self.assertEqual(unit["reference_id"], str(self.invoiceB.pk))
        self.assertEqual(unit["amount"]["value"], f"{owed:.2f}")
        self.assertEqual(unit["payee"], {"merchant_id": "MERCH-1"})
        self.assertIn(str(self.invoiceB.no_login_link), payload["application_context"]["return_url"])

    def test_paypal_failure_returns_to_the_invoice_with_an_error(self):
        with patch.object(CreatePayPalOrderView, "post_to_paypal", side_effect=PayPalRequestError("boom")):
            response = self.client.post(self.url)
        self.assertRedirects(response, self.invoice_page, fetch_redirect_response=False)
        self.assertTrue(any("rejected the order" in m for m in _messages(response)))

    def test_order_without_approval_link_returns_to_the_invoice(self):
        with (
            patch("auctions.views.payments.requests.post", return_value=_json_response({"access_token": "t"})),
            patch("auctions.views.payments.requests.request", return_value=_json_response({"id": "X", "links": []})),
            self.assertLogs("auctions.views.payments", level="ERROR"),
        ):
            response = self.client.post(self.url)
        self.assertRedirects(response, self.invoice_page, fetch_redirect_response=False)


@override_settings(**PAYPAL_SETTINGS)
class PayPalSuccessViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.invoiceB.status = "UNPAID"
        self.invoiceB.save()
        self.url = reverse("paypal_success")

    def _order(self, invoice_pk, value, status="COMPLETED"):
        return {
            "id": "ORDER-9",
            "status": status,
            "purchase_units": [
                {
                    "reference_id": str(invoice_pk),
                    "amount": {"currency_code": "USD", "value": value},
                    "payments": {"captures": [{"id": "CAPTURE-9", "amount": {"currency_code": "USD", "value": value}}]},
                }
            ],
            "payer": {"name": {"given_name": "Jane", "surname": "Buyer"}, "email_address": "jane@example.com"},
        }

    def test_completed_capture_records_payment_and_marks_invoice_paid(self):
        owed = Decimal("0.00") - Decimal(Invoice.objects.get(pk=self.invoiceB.pk).rounded_net_after_payments)
        layer = FakeChannelLayer()
        with (
            patch.object(
                PayPalSuccessView, "post_to_paypal", return_value=self._order(self.invoiceB.pk, f"{owed:.2f}")
            ),
            patch("channels.layers.get_channel_layer", return_value=layer),
        ):
            response = self.client.get(self.url, {"token": "ORDER-9"})
        self.assertRedirects(
            response,
            reverse("invoice_no_login", kwargs={"uuid": self.invoiceB.no_login_link}),
            fetch_redirect_response=False,
        )
        payment = InvoicePayment.objects.get(external_id="CAPTURE-9")
        self.assertEqual(payment.invoice, self.invoiceB)
        self.assertEqual(payment.amount, owed)
        self.assertEqual(payment.payer_name, "Jane Buyer")
        self.invoiceB.refresh_from_db()
        self.assertEqual(self.invoiceB.status, "PAID")
        self.assertIn(
            (f"auctions_{self.online_auction.pk}", {"type": "invoice_paid", "pk": self.invoiceB.pk}), layer.sent
        )

    def test_reloading_the_return_page_does_not_double_record(self):
        order = self._order(self.invoiceB.pk, "5.00")
        layer = FakeChannelLayer()
        with (
            patch.object(PayPalSuccessView, "post_to_paypal", return_value=order),
            patch("channels.layers.get_channel_layer", return_value=layer),
        ):
            self.client.get(self.url, {"token": "ORDER-9"})
            self.client.get(self.url, {"token": "ORDER-9"})
        self.assertEqual(InvoicePayment.objects.filter(external_id="CAPTURE-9").count(), 1)

    def test_uncompleted_order_records_nothing(self):
        order = self._order(self.invoiceB.pk, "5.00", status="PAYER_ACTION_REQUIRED")
        with patch.object(PayPalSuccessView, "post_to_paypal", return_value=order):
            response = self.client.get(self.url, {"token": "ORDER-9"})
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertFalse(InvoicePayment.objects.exists())
        self.assertTrue(any("not yet been completed" in m for m in _messages(response)))
        self.invoiceB.refresh_from_db()
        self.assertEqual(self.invoiceB.status, "UNPAID")

    def test_order_for_unknown_invoice_records_nothing(self):
        with patch.object(PayPalSuccessView, "post_to_paypal", return_value=self._order(999999, "5.00")):
            response = self.client.get(self.url, {"token": "ORDER-9"})
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertFalse(InvoicePayment.objects.exists())

    def test_capture_uses_the_credentials_of_the_club_that_created_the_order(self):
        """An order created with a club's own PayPal app can only be captured by that app."""
        club = Club.objects.create(
            name="Own App Club",
            membership_annual_fee=Decimal("20.00"),
            allow_non_oauth_paypal=True,
            paypal_client_id="club-cid",
            paypal_secret="club-secret",
        )
        invoice = Invoice.objects.create(club=club, buyer=self.userB, status="UNPAID", renewal_needed=True)
        with (
            patch(
                "auctions.views.payments.requests.post", return_value=_json_response({"access_token": "t"})
            ) as mock_post,
            patch(
                "auctions.views.payments.requests.request",
                return_value=_json_response({"status": "PAYER_ACTION_REQUIRED"}),
            ) as mock_request,
        ):
            self.client.get(self.url, {"token": "ORDER-9", "invoice": str(invoice.no_login_link)})
        self.assertEqual(mock_post.call_args.kwargs["auth"], ("club-cid", "club-secret"))
        self.assertTrue(mock_request.call_args.args[1].endswith("v2/checkout/orders/ORDER-9/capture"))


@override_settings(**PAYPAL_SETTINGS)
class PayPalSubscriptionWebhookVerificationTests(StandardTestCase):
    """The real verify round trip (requests mocked), which test_membership_flow patches out."""

    HEADERS = {
        "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
        "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/cert",
        "HTTP_PAYPAL_TRANSMISSION_ID": "tid",
        "HTTP_PAYPAL_TRANSMISSION_SIG": "sig",
        "HTTP_PAYPAL_TRANSMISSION_TIME": "2026-07-24T00:00:00Z",
    }

    def setUp(self):
        super().setUp()
        self.url = reverse("club_paypal_subscription_webhook")
        self.club = Club.objects.create(
            name="Subscription Club",
            membership_system="rolling",
            membership_annual_fee=Decimal("25.00"),
            allow_non_oauth_paypal=True,
            paypal_client_id="club-cid",
            paypal_secret="club-secret",
            paypal_webhook_id="WH-CLUB-1",
        )
        self.event = {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": "I-SUB1"}}

    def _subscription(self):
        next_time = (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": "I-SUB1",
            "status": "ACTIVE",
            "subscriber": {"email_address": "subscriber@example.com"},
            "billing_info": {
                "next_billing_time": next_time,
                "last_payment": {
                    "amount": {"currency_code": "USD", "value": "25.00"},
                    "time": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
        }

    def _post(self, verify_status="SUCCESS", subscription=None, fetch_error=None):
        token = _json_response({"access_token": "t"})
        verify = _json_response({"verification_status": verify_status})
        fetch = {"side_effect": fetch_error} if fetch_error else {"return_value": _json_response(subscription or {})}
        with (
            patch("auctions.views.payments.requests.post", side_effect=[token, verify, token, verify]) as mock_post,
            patch("auctions.views.payments.requests.request", **fetch) as mock_request,
            patch_views("maybe_send_membership_renewal_confirmation"),
        ):
            response = self.client.post(
                self.url, data=json.dumps(self.event), content_type="application/json", **self.HEADERS
            )
        return response, mock_post, mock_request

    def test_verified_renewal_is_applied(self):
        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        response, mock_post, _ = self._post(subscription=self._subscription())
        self.assertEqual(response.status_code, 200)
        member.refresh_from_db()
        self.assertEqual(member.paypal_subscription_id, "I-SUB1")
        self.assertEqual(member.membership_expiration_date, (timezone.now() + datetime.timedelta(days=365)).date())
        token_call, verify_call = mock_post.call_args_list
        self.assertEqual(token_call.kwargs["auth"], ("club-cid", "club-secret"))
        self.assertEqual(verify_call.kwargs["json"]["webhook_id"], "WH-CLUB-1")

    def test_unverifiable_event_is_400_and_applies_nothing(self):
        member = ClubMember.objects.create(club=self.club, name="Sub Member", email="subscriber@example.com")
        response, _, mock_request = self._post(verify_status="FAILURE")
        self.assertEqual(response.status_code, 400)
        mock_request.assert_not_called()
        member.refresh_from_db()
        self.assertEqual(member.paypal_subscription_id, "")

    def test_known_subscribers_club_failing_verification_does_not_fall_through(self):
        ClubMember.objects.create(
            club=self.club, name="Sub Member", email="subscriber@example.com", paypal_subscription_id="I-SUB1"
        )
        Club.objects.create(
            name="Other Club",
            allow_non_oauth_paypal=True,
            paypal_client_id="o",
            paypal_secret="o",
            paypal_webhook_id="WH-OTHER",
        )
        response, mock_post, _ = self._post(verify_status="FAILURE")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_post.call_count, 2)  # one token + one verify, for the subscriber's own club only

    def test_inactive_club_is_never_a_candidate(self):
        self.club.active = False
        self.club.save()
        response, mock_post, _ = self._post()
        self.assertEqual(response.status_code, 400)
        mock_post.assert_not_called()

    def test_subscription_fetch_failure_is_500_so_paypal_retries(self):
        with self.assertLogs("auctions.views", level="ERROR"):
            response, _, _ = self._post(fetch_error=requests.ConnectionError("down"))
        self.assertEqual(response.status_code, 500)
        self.assertFalse(ClubMember.objects.filter(club=self.club).exists())


class SquareWebhookRefusalTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("square_webhook")
        self.seller = SquareSeller.objects.create(
            user=self.admin_user, square_merchant_id="MERCHANT-1", access_token="tok", currency="USD"
        )
        self.revocation = {"merchant_id": "MERCHANT-1", "type": "oauth.authorization.revoked", "data": {}}

    @override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="real-key")
    def test_forged_revocation_leaves_the_seller_connected(self):
        response = self.client.post(
            self.url,
            data=json.dumps(self.revocation),
            content_type="application/json",
            HTTP_X_SQUARE_HMACSHA256_SIGNATURE="forged",
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SquareSeller.objects.filter(pk=self.seller.pk).exists())

    @override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="real-key")
    def test_forged_payment_records_nothing_and_never_calls_square(self):
        event = {
            "merchant_id": "MERCHANT-1",
            "type": "payment.updated",
            "data": {
                "object": {
                    "payment": {
                        "id": "PAY-1",
                        "status": "COMPLETED",
                        "order_id": "O-1",
                        "amount_money": {"amount": 1000, "currency": "USD"},
                    }
                }
            },
        }
        with patch.object(SquareSeller, "get_square_client") as mock_client:
            response = self.client.post(
                self.url,
                data=json.dumps(event),
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE="forged",
            )
        self.assertEqual(response.status_code, 403)
        mock_client.assert_not_called()
        self.assertFalse(InvoicePayment.objects.exists())

    @override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=False, SQUARE_APPLICATION_ID="", SQUARE_CLIENT_SECRET="")
    def test_no_signature_key_outside_debug_refuses_everything(self):
        with self.assertLogs("auctions.views", level="ERROR"):
            response = self.client.post(self.url, data=json.dumps(self.revocation), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SquareSeller.objects.filter(pk=self.seller.pk).exists())


class FakeChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_add(self, group, channel):
        return None

    async def group_discard(self, group, channel):
        return None

    async def group_send(self, group, message):
        self.sent.append((group, message))


class LotConsumerChatTests(StandardTestCase):
    """LotConsumer.receive and connect, driven directly with a fake channel layer."""

    def setUp(self):
        super().setUp()
        self.layer = FakeChannelLayer()
        patcher = patch("auctions.consumers.get_channel_layer", return_value=self.layer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _consumer(self, user):
        consumer = LotConsumer()
        consumer.scope = {"url_route": {"kwargs": {"lot_number": self.lot.pk}}, "user": user}
        consumer.channel_name = "test.channel"
        consumer.channel_layer = self.layer
        consumer.user = user
        consumer.lot = type(self.lot).objects.get(pk=self.lot.pk)
        consumer.room_group_name = f"lot_{self.lot.pk}"
        consumer.user_room_name = f"private_user_{user.pk}_lot_{self.lot.pk}"
        return consumer

    def _chats(self):
        return LotHistory.objects.filter(lot=self.lot, changed_price=False)

    def _errors(self):
        return [m["error"] for _, m in self.layer.sent if m["type"] == "error_message"]

    def test_chat_from_a_joined_user_is_saved_and_broadcast(self):
        self._consumer(self.userB).receive(json.dumps({"message": "Is this still available?"}))
        chat = self._chats().get()
        self.assertEqual(chat.user, self.userB)
        self.assertEqual(chat.message, "Is this still available?")
        self.assertIn(f"lot_{self.lot.pk}", [group for group, _ in self.layer.sent])

    def test_overlong_chat_is_truncated_not_lost(self):
        max_length = LotHistory._meta.get_field("message").max_length
        self._consumer(self.userB).receive(json.dumps({"message": "x" * (max_length + 500)}))
        self.assertEqual(len(self._chats().get().message), max_length)

    def test_chat_banned_user_is_refused(self):
        self.userB.userdata.banned_from_chat_until = timezone.now() + datetime.timedelta(days=3)
        self.userB.userdata.save()
        self._consumer(self.userB).receive(json.dumps({"message": "hello"}))
        self.assertFalse(self._chats().exists())
        self.assertTrue(any("can't chat" in e for e in self._errors()))

    def test_user_banned_by_the_seller_is_refused(self):
        # lot.user is None here: the ban is found through auctiontos_seller.user.
        self.assertIsNone(self.lot.user)
        UserBan.objects.create(user=self.user, banned_user=self.userB)
        self._consumer(self.userB).receive(json.dumps({"message": "hello"}))
        self.assertFalse(self._chats().exists())
        self.assertTrue(any("banned you" in e for e in self._errors()))

    def test_junk_frames_are_ignored_without_error(self):
        consumer = self._consumer(self.userB)
        for frame in (
            "not json",
            json.dumps([1, 2, 3]),
            json.dumps("a string"),
            json.dumps({"no_message": "x"}),
            json.dumps({"message": 42}),
            json.dumps({"message": {"nested": True}}),
            json.dumps({"message": "   "}),
            None,
        ):
            consumer.receive(frame)
        self.assertFalse(self._chats().exists())
        self.assertEqual(self.layer.sent, [])

    def test_anonymous_chat_is_ignored(self):
        self._consumer(AnonymousUser()).receive(json.dumps({"message": "hi"}))
        self.assertFalse(self._chats().exists())

    def _connect(self, user):
        consumer = LotConsumer()
        consumer.scope = {"url_route": {"kwargs": {"lot_number": self.lot.pk}}, "user": user}
        consumer.channel_name = "test.channel"
        consumer.channel_layer = self.layer
        with patch.object(LotConsumer, "accept"), patch.object(LotConsumer, "send"):
            consumer.connect()
        return consumer

    def test_seller_via_auctiontos_connecting_marks_chat_seen(self):
        LotHistory.objects.create(lot=self.lot, user=self.userB, message="unseen", changed_price=False)
        self._connect(self.user)
        self.assertFalse(LotHistory.objects.filter(lot=self.lot, seen=False).exists())

    def test_someone_else_connecting_leaves_chat_unseen(self):
        LotHistory.objects.create(lot=self.lot, user=self.userB, message="unseen", changed_price=False)
        self._connect(self.user_with_no_lots)
        self.assertTrue(LotHistory.objects.filter(lot=self.lot, seen=False).exists())

    def test_seller_banned_user_cannot_connect(self):
        UserBan.objects.create(user=self.user, banned_user=self.userB)
        with patch.object(LotConsumer, "close") as mock_close:
            consumer = self._connect(self.userB)
        mock_close.assert_called_once()
        self.assertIsNone(consumer.room_group_name)
