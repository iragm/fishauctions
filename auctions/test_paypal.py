"""PayPal: the webhooks, their event handlers, refund idempotency and the CSV export."""

import csv
import datetime
import io
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    AuctionTOS,
    Club,
    ClubMoney,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    PayPalSeller,
    SquareSeller,
)
from auctions.tests import StandardTestCase, patch_views


class PayPalWebhookViewTests(TestCase):
    """Tests for PayPalWebhookView webhook signature verification"""

    def setUp(self):
        self.url = reverse("paypal-webhook")
        self.webhook_event = {
            "id": "WH-ABC123",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAPTURE123", "status": "COMPLETED"},
        }
        self.valid_headers = {
            "HTTP_PAYPAL_TRANSMISSION_ID": "trans-id-123",
            "HTTP_PAYPAL_TRANSMISSION_TIME": "2024-01-01T00:00:00Z",
            "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/v1/notifications/certs/cert123",
            "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
            "HTTP_PAYPAL_TRANSMISSION_SIG": "sig-abc123",
        }

    def _post_webhook(self, data=None, extra_headers=None):
        headers = dict(self.valid_headers)
        if extra_headers is not None:
            headers.update(extra_headers)
        body = json.dumps(data if data is not None else self.webhook_event)
        return self.client.post(self.url, data=body, content_type="application/json", **headers)

    def test_missing_webhook_id_config_returns_400(self):
        """Webhook is rejected when PAYPAL_WEBHOOK_ID is not configured"""
        with override_settings(PAYPAL_WEBHOOK_ID=""):
            response = self._post_webhook()
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"webhook not configured", response.content)

    def test_missing_webhook_id_config_no_attr_returns_400(self):
        """Webhook is rejected when PAYPAL_WEBHOOK_ID attribute is absent from settings"""
        with self.settings():
            # Remove attribute if present
            from django.conf import settings as djsettings

            had_attr = hasattr(djsettings, "PAYPAL_WEBHOOK_ID")
            if had_attr:
                original = djsettings.PAYPAL_WEBHOOK_ID
                del djsettings.PAYPAL_WEBHOOK_ID
            try:
                response = self._post_webhook()
            finally:
                if had_attr:
                    djsettings.PAYPAL_WEBHOOK_ID = original
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"webhook not configured", response.content)

    def test_missing_transmission_headers_returns_400(self):
        """Webhook is rejected when PayPal transmission headers are absent"""
        with override_settings(PAYPAL_WEBHOOK_ID="WH-TESTID"):
            response = self.client.post(
                self.url,
                data=json.dumps(self.webhook_event),
                content_type="application/json",
                # No transmission headers
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"missing verification headers", response.content)

    def test_partial_transmission_headers_returns_400(self):
        """Webhook is rejected when only some PayPal transmission headers are present"""
        with override_settings(PAYPAL_WEBHOOK_ID="WH-TESTID"):
            response = self.client.post(
                self.url,
                data=json.dumps(self.webhook_event),
                content_type="application/json",
                HTTP_PAYPAL_TRANSMISSION_ID="trans-id-123",
                # Missing other required headers
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"missing verification headers", response.content)

    def test_invalid_json_body_returns_400(self):
        """Webhook is rejected when body is not valid JSON"""
        with override_settings(PAYPAL_WEBHOOK_ID="WH-TESTID"):
            response = self.client.post(
                self.url,
                data="not-valid-json{{{",
                content_type="application/json",
                **self.valid_headers,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"invalid json", response.content)

    @patch_views("requests.post")
    def test_access_token_failure_returns_500(self, mock_post):
        """Webhook returns 500 when access token request fails"""
        import requests as req

        mock_post.side_effect = req.HTTPError("token error")
        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            response = self._post_webhook()
        self.assertEqual(response.status_code, 500)

    @patch_views("requests.post")
    def test_verification_failure_returns_400(self, mock_post):
        """Webhook returns 400 when PayPal verification returns non-SUCCESS status"""
        from unittest.mock import MagicMock

        # First call: access token
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        # Second call: verify-webhook-signature returning FAILURE
        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.return_value = {"verification_status": "FAILURE"}

        mock_post.side_effect = [token_mock, verify_mock]

        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            response = self._post_webhook()
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"webhook verification failed", response.content)

    @patch_views("requests.post")
    def test_verify_endpoint_non_2xx_returns_500(self, mock_post):
        """Webhook returns 500 when PayPal verify-webhook-signature returns non-2xx"""
        from unittest.mock import MagicMock

        import requests as req

        # First call: access token succeeds
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        # Second call: verify endpoint returns 503
        verify_mock = MagicMock()
        verify_mock.status_code = 503
        verify_mock.headers = {"Paypal-Debug-Id": "debug-abc"}
        verify_mock.text = "Service Unavailable"
        verify_mock.raise_for_status.side_effect = req.HTTPError("503 error")

        mock_post.side_effect = [token_mock, verify_mock]

        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            response = self._post_webhook()
        self.assertEqual(response.status_code, 500)

    @patch_views("requests.post")
    def test_verify_endpoint_non_json_returns_500(self, mock_post):
        """Webhook returns 500 when PayPal verify-webhook-signature returns non-JSON"""
        from unittest.mock import MagicMock

        # First call: access token succeeds
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        # Second call: verify endpoint returns non-JSON
        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.headers = {"Paypal-Debug-Id": "debug-abc"}
        verify_mock.text = "not-json"
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.side_effect = ValueError("No JSON")

        mock_post.side_effect = [token_mock, verify_mock]

        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            response = self._post_webhook()
        self.assertEqual(response.status_code, 500)

    @patch_views("requests.post")
    def test_successful_verification_returns_200(self, mock_post):
        """Webhook returns 200 when PayPal verification succeeds for unhandled event type"""
        from unittest.mock import MagicMock

        # First call: access token
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        # Second call: verify-webhook-signature returning SUCCESS
        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.return_value = {"verification_status": "SUCCESS"}

        mock_post.side_effect = [token_mock, verify_mock]

        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            response = self._post_webhook(data={"id": "WH-XYZ", "event_type": "SOME.UNKNOWN.EVENT", "resource": {}})
        self.assertEqual(response.status_code, 200)

    @patch_views("requests.post")
    def test_verify_request_includes_webhook_id_and_timeout(self, mock_post):
        """Verify that webhook_id is sent in verification payload and timeout is set"""
        from unittest.mock import MagicMock

        # First call: access token
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        # Second call: verify-webhook-signature
        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.return_value = {"verification_status": "SUCCESS"}

        mock_post.side_effect = [token_mock, verify_mock]

        with override_settings(
            PAYPAL_WEBHOOK_ID="WH-TESTID-123",
            PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
            PAYPAL_CLIENT_ID="test-client-id",
            PAYPAL_SECRET="test-secret",
        ):
            self._post_webhook()

        # Check the second call (verify endpoint) was made with the right payload
        verify_call = mock_post.call_args_list[1]
        self.assertIn("timeout", verify_call.kwargs)
        sent_payload = verify_call.kwargs.get("json") or verify_call[1].get("json")
        self.assertEqual(sent_payload["webhook_id"], "WH-TESTID-123")


class PayPalWebhookEventHandlerTests(StandardTestCase):
    """Tests for PayPalWebhookView event processing after successful signature verification"""

    PAYPAL_SETTINGS = {
        "PAYPAL_WEBHOOK_ID": "WH-TESTID",
        "PAYPAL_API_BASE": "https://api-m.sandbox.paypal.com",
        "PAYPAL_CLIENT_ID": "test-client-id",
        "PAYPAL_SECRET": "test-secret",
    }

    def setUp(self):
        super().setUp()
        self.url = reverse("paypal-webhook")
        self.valid_headers = {
            "HTTP_PAYPAL_TRANSMISSION_ID": "trans-id-123",
            "HTTP_PAYPAL_TRANSMISSION_TIME": "2024-01-01T00:00:00Z",
            "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/v1/notifications/certs/cert123",
            "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
            "HTTP_PAYPAL_TRANSMISSION_SIG": "sig-abc123",
        }
        self.paypal_seller = PayPalSeller.objects.create(
            user=self.user,
            paypal_merchant_id="MERCHANT-ID-123",
            payer_email="seller@example.com",
        )
        self.invoiceB.status = "UNPAID"
        self.invoiceB.save()

    def _post_verified_webhook(self, event_data):
        """Post a webhook event with mocked signature verification always passing"""
        from unittest.mock import MagicMock

        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.return_value = {"verification_status": "SUCCESS"}

        with patch_views("requests.post", side_effect=[token_mock, verify_mock]):
            with override_settings(**self.PAYPAL_SETTINGS):
                return self.client.post(
                    self.url,
                    data=json.dumps(event_data),
                    content_type="application/json",
                    **self.valid_headers,
                )

    def test_onboarding_completed_creates_paypal_seller(self):
        """MERCHANT.ONBOARDING.COMPLETED webhook creates/updates PayPalSeller via tracking_id"""
        tracking_id = str(self.admin_user.userdata.unsubscribe_link)
        new_merchant_id = "NEW-MERCHANT-456"
        event = {
            "id": "WH-ONBOARDING-123",
            "event_type": "MERCHANT.ONBOARDING.COMPLETED",
            "resource": {
                "tracking_id": tracking_id,
                "merchant_id": new_merchant_id,
                "payerEmail": "newemail@example.com",
            },
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        seller = PayPalSeller.objects.filter(user=self.admin_user).first()
        self.assertIsNotNone(seller)
        self.assertEqual(seller.paypal_merchant_id, new_merchant_id)
        self.assertEqual(seller.payer_email, "newemail@example.com")

    def test_onboarding_completed_unknown_user_returns_200(self):
        """MERCHANT.ONBOARDING.COMPLETED with unknown tracking_id logs and returns 200"""
        event = {
            "id": "WH-ONBOARDING-UNKNOWN",
            "event_type": "MERCHANT.ONBOARDING.COMPLETED",
            "resource": {
                "tracking_id": "unknown-tracking-id",
                "merchant_id": "UNKNOWN-MERCHANT",
            },
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PayPalSeller.objects.filter(paypal_merchant_id="UNKNOWN-MERCHANT").exists())

    def test_consent_revoked_deletes_seller(self):
        """MERCHANT.PARTNER-CONSENT.REVOKED deletes the matching PayPalSeller"""
        event = {
            "id": "WH-REVOKED-123",
            "event_type": "MERCHANT.PARTNER-CONSENT.REVOKED",
            "resource": {"merchant_id": "MERCHANT-ID-123"},
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PayPalSeller.objects.filter(paypal_merchant_id="MERCHANT-ID-123").exists())

    def test_consent_revoked_unknown_merchant_returns_200(self):
        """MERCHANT.PARTNER-CONSENT.REVOKED with unknown merchant returns 200 without error"""
        event = {
            "id": "WH-REVOKED-UNKNOWN",
            "event_type": "MERCHANT.PARTNER-CONSENT.REVOKED",
            "resource": {"merchant_id": "NONEXISTENT-MERCHANT"},
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)

    def test_checkout_order_completed_records_payment(self):
        """CHECKOUT.ORDER.COMPLETED webhook creates an InvoicePayment record"""
        from decimal import Decimal

        from auctions.models import InvoicePayment

        invoice = self.invoiceB
        capture_id = "CAPTURE-WEBHOOK-789"
        order_id = "ORDER-WEBHOOK-456"
        event = {
            "id": "WH-ORDER-COMPLETED",
            "event_type": "CHECKOUT.ORDER.COMPLETED",
            "resource": {
                "id": order_id,
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "reference_id": str(invoice.pk),
                        "amount": {"currency_code": "USD", "value": "37.50"},
                        "payments": {
                            "captures": [
                                {
                                    "id": capture_id,
                                    "status": "COMPLETED",
                                    "amount": {"currency_code": "USD", "value": "37.50"},
                                }
                            ]
                        },
                    }
                ],
                "payer": {
                    "name": {"given_name": "Jane", "surname": "Buyer"},
                    "email_address": "jane@example.com",
                },
            },
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        payment = InvoicePayment.objects.filter(external_id=capture_id).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.invoice, invoice)
        self.assertEqual(payment.payment_method, "PayPal")
        self.assertEqual(payment.amount, Decimal("37.50"))

    def test_rounded_paypal_payment_marks_invoice_paid_and_zeroes_balance(self):
        """With invoice rounding on, paying the rounded balance must settle to PAID / $0.00.

        A fractional balance paid at the rounded amount leaves a sub-dollar residual on
        net_after_payments; the PAID check must use the rounded balance so the invoice still settles.
        This would fail under the old `net_after_payments >= 0` check (it would stay UNPAID).
        """
        from decimal import Decimal

        from auctions.models import InvoicePayment

        self.assertTrue(self.online_auction.invoice_rounding)  # default
        tos = AuctionTOS.objects.create(
            user=User.objects.create_user("roundpaypal", "rp@example.com", "pw"),
            auction=self.online_auction,
            pickup_location=self.location,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        # $20 owed, less a $0.40 partial payment, leaves a fractional $19.60 balance → rounds to $19.00.
        InvoiceAdjustment.objects.create(adjustment_type="ADD", amount=20, notes="t", invoice=invoice)
        InvoicePayment.objects.create(
            invoice=invoice, payment_method="Cash", amount=Decimal("0.40"), currency=invoice.currency
        )
        invoice.refresh_from_db()
        rounded = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        unrounded = Decimal("0.00") - Decimal(invoice.net_after_payments)
        self.assertNotEqual(rounded, unrounded)  # rounding actually applies here
        self.assertEqual(rounded, Decimal("19.00"))

        event = {
            "id": "WH-ORDER-ROUNDED",
            "event_type": "CHECKOUT.ORDER.COMPLETED",
            "resource": {
                "id": "ORDER-ROUNDED-1",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "reference_id": str(invoice.pk),
                        "amount": {"currency_code": "USD", "value": f"{rounded:.2f}"},
                        "payments": {
                            "captures": [
                                {
                                    "id": "CAPTURE-ROUNDED-1",
                                    "status": "COMPLETED",
                                    "amount": {"currency_code": "USD", "value": f"{rounded:.2f}"},
                                }
                            ]
                        },
                    }
                ],
            },
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "PAID")
        self.assertEqual(invoice.rounded_net_after_payments, Decimal("0.00"))  # balance due shows 0.00

    def test_checkout_order_completed_non_completed_status_is_ignored(self):
        """CHECKOUT.ORDER.COMPLETED with non-COMPLETED status does not create a payment"""
        from auctions.models import InvoicePayment

        invoice = self.invoiceB
        event = {
            "id": "WH-ORDER-PENDING",
            "event_type": "CHECKOUT.ORDER.COMPLETED",
            "resource": {
                "id": "ORDER-PENDING",
                "status": "PENDING",
                "purchase_units": [{"reference_id": str(invoice.pk)}],
            },
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InvoicePayment.objects.filter(invoice=invoice).exists())

    def test_unhandled_event_type_returns_200(self):
        """Unhandled event types return 200 and are logged without error"""
        event = {
            "id": "WH-UNKNOWN",
            "event_type": "SOME.UNKNOWN.EVENT.TYPE",
            "resource": {"id": "res-123"},
        }
        response = self._post_verified_webhook(event)
        self.assertEqual(response.status_code, 200)


class RefundWebhookIdempotencyTests(StandardTestCase):
    """Refund webhooks are redelivered/re-fired by both PayPal and Square, so the refundable
    balance must move only once per refund.

    Regression: amount_available_to_refund was decremented on every webhook delivery, so a
    duplicate delivery of the same refund permanently shrank the refundable amount.
    """

    PAYPAL_SETTINGS = {
        "PAYPAL_WEBHOOK_ID": "WH-TESTID",
        "PAYPAL_API_BASE": "https://api-m.sandbox.paypal.com",
        "PAYPAL_CLIENT_ID": "test-client-id",
        "PAYPAL_SECRET": "test-secret",
    }

    def setUp(self):
        super().setUp()
        self.paypal_headers = {
            "HTTP_PAYPAL_TRANSMISSION_ID": "trans-id-123",
            "HTTP_PAYPAL_TRANSMISSION_TIME": "2024-01-01T00:00:00Z",
            "HTTP_PAYPAL_CERT_URL": "https://api.paypal.com/v1/notifications/certs/cert123",
            "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
            "HTTP_PAYPAL_TRANSMISSION_SIG": "sig-abc123",
        }

    def _post_verified_webhook(self, event_data):
        """Post a PayPal webhook with mocked token fetch and signature verification (always passes)."""
        token_mock = MagicMock()
        token_mock.json.return_value = {"access_token": "test-token"}
        token_mock.raise_for_status.return_value = None

        verify_mock = MagicMock()
        verify_mock.status_code = 200
        verify_mock.raise_for_status.return_value = None
        verify_mock.json.return_value = {"verification_status": "SUCCESS"}

        with patch_views("requests.post", side_effect=[token_mock, verify_mock]):
            with override_settings(**self.PAYPAL_SETTINGS):
                return self.client.post(
                    reverse("paypal-webhook"),
                    data=json.dumps(event_data),
                    content_type="application/json",
                    **self.paypal_headers,
                )

    def _make_payment(self, external_id, amount, payment_method):
        return InvoicePayment.objects.create(
            invoice=self.invoiceB,
            external_id=external_id,
            amount=Decimal(amount),
            amount_available_to_refund=Decimal(amount),
            currency="USD",
            payment_method=payment_method,
        )

    def _paypal_refund_event(self, refund_id, capture_id, value):
        return {
            "id": "WH-REFUND-EVENT",
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {
                "id": refund_id,
                "amount": {"currency_code": "USD", "value": value},
                "links": [
                    {"rel": "up", "href": f"https://api.paypal.com/v2/payments/captures/{capture_id}"},
                ],
            },
        }

    def _square_refund_event(self, refund_id, payment_id, amount_cents):
        return {
            "merchant_id": "MLF3WZS2N9WVG",
            "type": "refund.updated",
            "event_id": "sq-refund-event",
            "created_at": "2026-01-01T00:00:00Z",
            "data": {
                "type": "refund",
                "id": "refund-data-id",
                "object": {
                    "refund": {
                        "id": refund_id,
                        "status": "COMPLETED",
                        "payment_id": payment_id,
                        "amount_money": {"amount": amount_cents, "currency": "USD"},
                        "reason": "test refund",
                    }
                },
            },
        }

    def _post_square(self, event):
        # The env sets a real SQUARE_WEBHOOK_SIGNATURE_KEY; clear it (as the other Square webhook
        # tests do) so signature verification is skipped for these posts.
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
            return self.client.post(reverse("square_webhook"), data=json.dumps(event), content_type="application/json")

    def test_paypal_duplicate_refund_webhook_decrements_once(self):
        capture_id = "CAPTURE-REFUND-DUP"
        refund_id = "REFUND-DUP-1"
        payment = self._make_payment(capture_id, "40.00", "PayPal")
        event = self._paypal_refund_event(refund_id, capture_id, "15.00")

        # First delivery decrements the refundable balance by the refund amount.
        self.assertEqual(self._post_verified_webhook(event).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("25.00"))

        # Redelivery of the identical refund must be a no-op.
        self.assertEqual(self._post_verified_webhook(event).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("25.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id=refund_id).count(), 1)

    def test_paypal_updated_refund_amount_adjusts_by_delta(self):
        capture_id = "CAPTURE-REFUND-UPD"
        refund_id = "REFUND-UPD-1"
        payment = self._make_payment(capture_id, "40.00", "PayPal")

        self.assertEqual(
            self._post_verified_webhook(self._paypal_refund_event(refund_id, capture_id, "10.00")).status_code, 200
        )
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("30.00"))

        # A later delivery raises the refund from $10 to $18; the balance moves only by the $8 delta.
        self.assertEqual(
            self._post_verified_webhook(self._paypal_refund_event(refund_id, capture_id, "18.00")).status_code, 200
        )
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("22.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id=refund_id).count(), 1)
        self.assertEqual(InvoicePayment.objects.get(external_id=refund_id).amount, Decimal("-18.00"))

    def test_paypal_refund_on_paid_club_invoice_keeps_ledger_frozen(self):
        # Item 9 freeze end-to-end: a refund webhook on a settled (PAID) club invoice still
        # records the refund (negative InvoicePayment + reduced refundable balance) but must not
        # re-derive the invoice's settled total or re-book the club ledger from current settings.
        club = Club.objects.create(name="Refund Freeze Club", enable_membership=True)
        self.online_auction.club = club
        self.online_auction.save(update_fields=["club"])
        ClubMoney.objects.all().delete()

        capture_id = "CAPTURE-FREEZE"
        refund_id = "REFUND-FREEZE"
        payment = self._make_payment(capture_id, "40.00", "PayPal")
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        self.invoiceB.refresh_from_db()
        frozen_total = self.invoiceB.calculated_total
        frozen_rows = sorted(ClubMoney.objects.filter(invoice=self.invoiceB).values_list("pk", "amount", "category"))
        self.assertTrue(frozen_rows)  # the sale/tax entries were booked when it was marked PAID

        self.assertEqual(
            self._post_verified_webhook(self._paypal_refund_event(refund_id, capture_id, "15.00")).status_code, 200
        )
        # The refund itself flows through: refundable balance drops once, refund row exists.
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("25.00"))
        self.assertTrue(InvoicePayment.objects.filter(external_id=refund_id, amount=Decimal("-15.00")).exists())
        # The freeze: the settled total and every booked ledger row are unchanged.
        self.invoiceB.refresh_from_db()
        self.assertEqual(self.invoiceB.calculated_total, frozen_total)
        current_rows = sorted(ClubMoney.objects.filter(invoice=self.invoiceB).values_list("pk", "amount", "category"))
        self.assertEqual(current_rows, frozen_rows)

    def test_square_duplicate_refund_webhook_decrements_once(self):
        payment_id = "SQ-PAYMENT-DUP"
        refund_id = "SQ-REFUND-DUP"
        payment = self._make_payment(payment_id, "50.00", "Square")
        event = self._square_refund_event(refund_id, payment_id, 2000)  # $20.00

        self.assertEqual(self._post_square(event).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("30.00"))

        # Square re-fires the same refund (refund.updated retries / follow-up events); no double decrement.
        self.assertEqual(self._post_square(event).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("30.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id=refund_id).count(), 1)

    def test_square_updated_refund_amount_adjusts_by_delta(self):
        payment_id = "SQ-PAYMENT-UPD"
        refund_id = "SQ-REFUND-UPD"
        payment = self._make_payment(payment_id, "50.00", "Square")

        self.assertEqual(self._post_square(self._square_refund_event(refund_id, payment_id, 1000)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("40.00"))

        # A later delivery raises the refund from $10 to $12; the balance moves only by the $2 delta.
        self.assertEqual(self._post_square(self._square_refund_event(refund_id, payment_id, 1200)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("38.00"))
        self.assertEqual(InvoicePayment.objects.get(external_id=refund_id).amount, Decimal("-12.00"))


class SquarePaymentUpdatedRefundResurrectionTests(StandardTestCase):
    """Square fires payment.updated for many lifecycle changes. A later payment.updated for an
    already-recorded payment must never restore refundability that refunds have already consumed.

    Regression: the handler reset amount_available_to_refund to the full payment amount whenever it
    was currently 0, so a fully-refunded payment became "refundable" again after any later
    payment.updated event, allowing a second full refund (double refund).
    """

    MERCHANT_ID = "MLF3WZS2N9WVG"

    def setUp(self):
        super().setUp()

        # The COMPLETED branch of payment.updated looks up a SquareSeller by merchant_id and
        # resolves the invoice via the order's reference_id.
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id=self.MERCHANT_ID,
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

    def _payment_updated_event(self, payment_id, order_id, amount_cents, status="COMPLETED"):
        return {
            "merchant_id": self.MERCHANT_ID,
            "type": "payment.updated",
            "event_id": "sq-payment-event",
            "created_at": "2026-01-01T00:00:00Z",
            "data": {
                "type": "payment",
                "id": "payment-data-id",
                "object": {
                    "payment": {
                        "id": payment_id,
                        "status": status,
                        "order_id": order_id,
                        "amount_money": {"amount": amount_cents, "currency": "USD"},
                    }
                },
            },
        }

    def _square_refund_event(self, refund_id, payment_id, amount_cents):
        return {
            "merchant_id": self.MERCHANT_ID,
            "type": "refund.updated",
            "event_id": "sq-refund-event",
            "created_at": "2026-01-01T00:00:00Z",
            "data": {
                "type": "refund",
                "id": "refund-data-id",
                "object": {
                    "refund": {
                        "id": refund_id,
                        "status": "COMPLETED",
                        "payment_id": payment_id,
                        "amount_money": {"amount": amount_cents, "currency": "USD"},
                        "reason": "test refund",
                    }
                },
            },
        }

    def _post_payment_updated(self, payment_id, order_id, amount_cents, reference_id=None, status="COMPLETED"):
        """Build and post a payment.updated webhook, mocking the Square order lookup to return
        reference_id (our invoice pk, defaulting to invoiceB) and skipping signature verification.
        """

        if reference_id is None:
            reference_id = self.invoiceB.pk
        event = self._payment_updated_event(payment_id, order_id, amount_cents, status=status)
        mock_order = MagicMock()
        mock_order.reference_id = str(reference_id)
        mock_order_response = MagicMock()
        mock_order_response.order = mock_order
        mock_client = MagicMock()
        mock_client.orders.get.return_value = mock_order_response

        with patch.object(SquareSeller, "get_square_client", return_value=mock_client):
            with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
                return self.client.post(
                    reverse("square_webhook"), data=json.dumps(event), content_type="application/json"
                )

    def _post_refund(self, event):
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
            return self.client.post(reverse("square_webhook"), data=json.dumps(event), content_type="application/json")

    def test_payment_updated_does_not_resurrect_refundability_after_full_refund(self):
        payment_id = "SQ-PAY-RESURRECT"
        order_id = "SQ-ORDER-RESURRECT"
        refund_id = "SQ-REFUND-RESURRECT"

        # A first payment.updated records the payment ($30 available to refund).
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        payment = InvoicePayment.objects.get(external_id=payment_id)
        self.assertEqual(payment.amount, Decimal("30.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("30.00"))

        # A full refund consumes the entire refundable balance.
        self.assertEqual(self._post_refund(self._square_refund_event(refund_id, payment_id, 3000)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("0.00"))

        # A later/duplicate payment.updated for the same payment must NOT resurrect refundability.
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("0.00"))
        # The payment amount itself is unchanged, and no duplicate payment row was created.
        self.assertEqual(payment.amount, Decimal("30.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id=payment_id).count(), 1)

    def test_payment_updated_initializes_new_payment(self):
        payment_id = "SQ-PAY-NEW"
        order_id = "SQ-ORDER-NEW"

        self.assertFalse(InvoicePayment.objects.filter(external_id=payment_id).exists())
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 4500).status_code, 200)
        payment = InvoicePayment.objects.get(external_id=payment_id)
        self.assertEqual(payment.invoice, self.invoiceB)
        self.assertEqual(payment.amount, Decimal("45.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("45.00"))
        self.assertEqual(payment.payment_method, "Square")

    def test_payment_updated_amount_change_moves_refundable_by_delta(self):
        payment_id = "SQ-PAY-DELTA"
        order_id = "SQ-ORDER-DELTA"
        refund_id = "SQ-REFUND-DELTA"

        # Record a $30 payment, then partially refund $10 (leaving $20 available).
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        self.assertEqual(self._post_refund(self._square_refund_event(refund_id, payment_id, 1000)).status_code, 200)
        payment = InvoicePayment.objects.get(external_id=payment_id)
        self.assertEqual(payment.amount_available_to_refund, Decimal("20.00"))

        # A later payment.updated raises the captured amount to $35; the refundable balance moves by
        # the $5 delta (to $25), rather than being reset to the full amount.
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3500).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("35.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("25.00"))


class PayPalCSVExportTests(StandardTestCase):
    """Test the PayPal CSV export name splitting and truncation behavior"""

    def _create_tos_with_lot(self, name, email):
        """Helper: create an AuctionTOS with a bought lot so the invoice is in debt"""
        tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name=name,
            email=email,
        )
        Lot.objects.create(
            lot_name=f"Lot for {email}",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=50,
            auctiontos_winner=tos,
            active=False,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        invoice.status = "UNPAID"
        invoice.save()
        return tos

    def _get_csv_rows(self):
        self.client.force_login(self.admin_user)
        url = reverse("paypal_csv", kwargs={"slug": self.online_auction.slug, "chunk": 1})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        return list(reader)

    def _row_for_email(self, rows, email):
        for row in rows[1:]:
            if row and row[0] == email:
                return row
        return None

    def test_two_word_name_split_into_first_and_last(self):
        """Two-word name: first word → first name, second word → last name"""
        self._create_tos_with_lot("John Doe", "twopart@example.com")
        rows = self._get_csv_rows()
        row = self._row_for_email(rows, "twopart@example.com")
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "John")
        self.assertEqual(row[2], "Doe")

    def test_middle_name_dropped(self):
        """Three-word name: first word → first name, last word → last name, middle dropped"""
        self._create_tos_with_lot("John Middle Doe", "middle@example.com")
        rows = self._get_csv_rows()
        row = self._row_for_email(rows, "middle@example.com")
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "John")
        self.assertEqual(row[2], "Doe")

    def test_single_word_name_goes_to_last_name(self):
        """Single-word name: first name empty, name goes to last name"""
        self._create_tos_with_lot("Cher", "singlename@example.com")
        rows = self._get_csv_rows()
        row = self._row_for_email(rows, "singlename@example.com")
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "")
        self.assertEqual(row[2], "Cher")

    def test_long_first_name_truncated_to_20_chars(self):
        """First name exceeding 20 chars must be truncated"""
        self._create_tos_with_lot("Bartholomewthefirstofhisname Doe", "longfirst@example.com")
        rows = self._get_csv_rows()
        row = self._row_for_email(rows, "longfirst@example.com")
        self.assertIsNotNone(row)
        self.assertLessEqual(len(row[1]), 20)
        self.assertEqual(row[1], "Bartholomewthefirsto")

    def test_long_last_name_truncated_to_20_chars(self):
        """Last name exceeding 20 chars must be truncated"""
        self._create_tos_with_lot("John Longfellowtheeloquentspeaker", "longlast@example.com")
        rows = self._get_csv_rows()
        row = self._row_for_email(rows, "longlast@example.com")
        self.assertIsNotNone(row)
        self.assertLessEqual(len(row[2]), 20)
        self.assertEqual(row[2], "Longfellowtheeloquen")
