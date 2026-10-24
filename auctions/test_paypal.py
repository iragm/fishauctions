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
        """The webhook is rejected when PAYPAL_WEBHOOK_ID is absent from settings."""
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
        """An unhandled event type returns 200 once verification succeeds."""
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

        # The verify call is the second one.
        verify_call = mock_post.call_args_list[1]
        self.assertIn("timeout", verify_call.kwargs)
        sent_payload = verify_call.kwargs.get("json") or verify_call[1].get("json")
        self.assertEqual(sent_payload["webhook_id"], "WH-TESTID-123")


class PayPalWebhookEventHandlerTests(StandardTestCase):
    """PayPalWebhookView event processing after signature verification."""

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
        """MERCHANT.ONBOARDING.COMPLETED creates or updates a PayPalSeller via tracking_id."""
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
        """MERCHANT.PARTNER-CONSENT.REVOKED for an unknown merchant returns 200."""
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
        """With rounding on, paying the rounded balance settles the invoice to PAID despite the residual."""
        from decimal import Decimal

        from auctions.models import InvoicePayment

        self.assertTrue(self.online_auction.invoice_rounding)  # default
        tos = AuctionTOS.objects.create(
            user=User.objects.create_user("roundpaypal", "rp@example.com", "pw"),
            auction=self.online_auction,
            pickup_location=self.location,
        )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=tos)
        # $20 owed less $0.40 leaves $19.60, rounding to $19.00.
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
        """CHECKOUT.ORDER.COMPLETED with a non-COMPLETED status creates no payment."""
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
    """Refund webhooks are redelivered by both PayPal and Square, so the refundable balance must move once
    per refund; it used to be decremented on every delivery.
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
        """Post a PayPal webhook with the token fetch and signature verification mocked."""
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
        # The env sets a real signature key; clear it so verification is skipped.
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
            return self.client.post(reverse("square_webhook"), data=json.dumps(event), content_type="application/json")

    def test_paypal_duplicate_refund_webhook_decrements_once(self):
        capture_id = "CAPTURE-REFUND-DUP"
        refund_id = "REFUND-DUP-1"
        payment = self._make_payment(capture_id, "40.00", "PayPal")
        event = self._paypal_refund_event(refund_id, capture_id, "15.00")

        # The first delivery decrements the refundable balance.
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

        # A later delivery raises the refund to $18; the balance moves by the $8 delta.
        self.assertEqual(
            self._post_verified_webhook(self._paypal_refund_event(refund_id, capture_id, "18.00")).status_code, 200
        )
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("22.00"))
        self.assertEqual(InvoicePayment.objects.filter(external_id=refund_id).count(), 1)
        self.assertEqual(InvoicePayment.objects.get(external_id=refund_id).amount, Decimal("-18.00"))

    def test_paypal_refund_on_paid_club_invoice_keeps_ledger_frozen(self):
        # A refund on a settled invoice records the refund but doesn't re-derive its total or
        # re-book the ledger.
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
        # The refund flows through: the balance drops once and the row exists.
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("25.00"))
        self.assertTrue(InvoicePayment.objects.filter(external_id=refund_id, amount=Decimal("-15.00")).exists())
        # The freeze: the settled total and booked rows are unchanged.
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

        # Square re-fires the same refund; no double decrement.
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

        # A later delivery raises it to $12; the balance moves by the $2 delta.
        self.assertEqual(self._post_square(self._square_refund_event(refund_id, payment_id, 1200)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("38.00"))
        self.assertEqual(InvoicePayment.objects.get(external_id=refund_id).amount, Decimal("-12.00"))


class SquarePaymentUpdatedRefundResurrectionTests(StandardTestCase):
    """A later payment.updated must not restore refundability that refunds have consumed.

    The handler reset amount_available_to_refund whenever it was 0, so a fully refunded payment became
    refundable again and could be refunded twice.
    """

    MERCHANT_ID = "MLF3WZS2N9WVG"

    def setUp(self):
        super().setUp()

        # The COMPLETED branch looks up a SquareSeller by merchant_id and the invoice by reference_id.
        # The auction's own creator: a payment into anyone else's account doesn't count for its invoices.
        self.square_seller = SquareSeller.objects.create(
            user=self.online_auction.created_by,
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
        """Build and post a payment.updated webhook, mocking the order lookup and skipping verification."""

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

        # A first payment.updated records the payment, with $30 refundable.
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        payment = InvoicePayment.objects.get(external_id=payment_id)
        self.assertEqual(payment.amount, Decimal("30.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("30.00"))

        # A full refund consumes the entire refundable balance.
        self.assertEqual(self._post_refund(self._square_refund_event(refund_id, payment_id, 3000)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("0.00"))

        # A duplicate must not resurrect refundability.
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("0.00"))
        # The amount is unchanged and no duplicate row was created.
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

        # Record $30, then refund $10, leaving $20.
        self.assertEqual(self._post_payment_updated(payment_id, order_id, 3000).status_code, 200)
        self.assertEqual(self._post_refund(self._square_refund_event(refund_id, payment_id, 1000)).status_code, 200)
        payment = InvoicePayment.objects.get(external_id=payment_id)
        self.assertEqual(payment.amount_available_to_refund, Decimal("20.00"))

        # A later payment.updated raising the capture to $35 moves the balance by the $5 delta.
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
        """A three-word name: first and last words are the names, the middle is dropped."""
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


@override_settings(
    PAYPAL_API_BASE="https://api-m.sandbox.paypal.com",
    PAYPAL_CLIENT_ID="site-client-id",
    PAYPAL_SECRET="site-secret",
    PAYPAL_BN_CODE="",
)
class PayPalLotRefundTests(StandardTestCase):
    """A lot refund goes back to the buyer's PayPal once, for no more than the lot and the capture.
    The fixture auction charges 25% tax, so a $10 lot cost the buyer $12.50."""

    def setUp(self):
        super().setUp()
        self.capture = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            external_id="CAPTURE-1",
            amount=Decimal("30.00"),
            amount_available_to_refund=Decimal("30.00"),
            currency="USD",
            payment_method="PayPal",
        )

    def _refund_response(self, refund_id="REFUND-1", value="12.50", status="COMPLETED"):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Paypal-Debug-Id": "debug"}
        response.json.return_value = {
            "id": refund_id,
            "status": status,
            "amount": {"value": value, "currency_code": "USD"},
        }
        return response

    def _token(self):
        token = MagicMock()
        token.raise_for_status.return_value = None
        token.json.return_value = {"access_token": "token"}
        return token

    def _refund(self, lot, percent, response=None, **request_kwargs):
        with (
            patch("auctions.views.payments.requests.post", return_value=self._token()) as token_post,
            patch(
                "auctions.views.payments.requests.request",
                return_value=response or self._refund_response(),
                **request_kwargs,
            ) as api,
        ):
            result = lot.refund(percent, self.admin_user)
        return result, api, token_post

    def test_refund_goes_back_through_paypal_and_is_recorded(self):
        result, api, _ = self._refund(self.lot, 100)
        self.assertEqual(result, ("PayPal", None))
        method, url = api.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("v2/payments/captures/CAPTURE-1/refund"))
        self.assertEqual(api.call_args.kwargs["json"]["amount"], {"value": "12.50", "currency_code": "USD"})
        headers = api.call_args.kwargs["headers"]
        self.assertEqual(headers["PayPal-Request-Id"], f"auction-fish-lot-{self.lot.pk}-refund")
        # The site account took the payment, so it refunds as itself.
        self.assertNotIn("PayPal-Auth-Assertion", headers)
        refund = InvoicePayment.objects.get(external_id="REFUND-1")
        self.assertEqual(refund.amount, Decimal("-12.50"))
        self.assertEqual(refund.invoice, self.invoiceB)
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.amount_available_to_refund, Decimal("17.50"))
        self.lot.refresh_from_db()
        self.assertTrue(self.lot.no_more_refunds_possible)
        self.assertEqual(self.lot.partial_refund_percent, 100)
        self.assertIn("PayPal refund processed", self.lot.lothistory_set.latest("pk").message)

    def test_a_second_refund_on_the_lot_never_reaches_paypal(self):
        self._refund(self.lot, 50, response=self._refund_response(value="6.25"))
        lot = Lot.objects.get(pk=self.lot.pk)
        result, api, _ = self._refund(lot, 100)
        self.assertEqual(result, (None, None))
        api.assert_not_called()
        self.assertEqual(InvoicePayment.objects.filter(payment_method="PayPal Refund").count(), 1)

    def test_a_double_submit_refunds_once_and_reports_no_failure(self):
        stale = Lot.objects.get(pk=self.lot.pk)
        self._refund(self.lot, 100)
        result, api, _ = self._refund(stale, 100)
        self.assertEqual(result, (None, None))
        api.assert_not_called()
        stale.refresh_from_db()
        # The stale copy's save doesn't clear the mark the first refund left.
        self.assertTrue(stale.no_more_refunds_possible)
        self.assertEqual(stale.lothistory_set.count(), 1)
        self.assertEqual(self.lot.paypal_refund(100), "This lot has already been refunded through PayPal")

    def test_only_the_part_above_an_earlier_refund_goes_back(self):
        Lot.objects.filter(pk=self.lot.pk).update(partial_refund_percent=50)
        lot = Lot.objects.get(pk=self.lot.pk)
        _, api, _ = self._refund(lot, 100, response=self._refund_response(value="6.25"))
        self.assertEqual(api.call_args.kwargs["json"]["amount"]["value"], "6.25")

    def test_lowering_a_refund_sends_nothing(self):
        Lot.objects.filter(pk=self.lot.pk).update(partial_refund_percent=100)
        lot = Lot.objects.get(pk=self.lot.pk)
        result, api, _ = self._refund(lot, 50)
        self.assertEqual(result, (None, None))
        api.assert_not_called()

    def test_a_capture_smaller_than_the_lot_is_left_alone(self):
        InvoicePayment.objects.filter(pk=self.capture.pk).update(amount_available_to_refund=Decimal("12.49"))
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertFalse(lot.paypal_refund_possible)
        result, api, _ = self._refund(lot, 100)
        self.assertEqual(result, (None, None))
        api.assert_not_called()

    def test_refund_invoice_refuses_more_than_the_capture_holds(self):
        from auctions.views import PayPalAPIMixin

        with patch("auctions.views.payments.requests.request") as api:
            error = PayPalAPIMixin().refund_invoice(self.invoiceB, Decimal("30.01"), request_id="x")
        self.assertIn("Not enough", error)
        api.assert_not_called()

    def test_without_site_credentials_nothing_is_attempted(self):
        with override_settings(PAYPAL_CLIENT_ID="", PAYPAL_SECRET=""):
            self.assertFalse(Lot.objects.get(pk=self.lot.pk).paypal_refund_possible)

    def test_a_refused_refund_records_nothing_and_says_so(self):
        provider, error = self._refund(self.lot, 100, response=self._failed(422))[0]
        self.assertEqual(provider, "PayPal")
        self.assertIn("PayPal", error)
        self.assertFalse(InvoicePayment.objects.filter(payment_method="PayPal Refund").exists())
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.amount_available_to_refund, Decimal("30.00"))
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.no_more_refunds_possible)
        # The lot's refund still stands; the history says the money has to go back by hand.
        self.assertEqual(self.lot.partial_refund_percent, 100)
        self.assertIn("PayPal refund failed", self.lot.lothistory_set.latest("pk").message)

    def _failed(self, status_code):
        import requests

        failed = MagicMock()
        failed.status_code = status_code
        failed.text = "{}"
        failed.headers = {"Paypal-Debug-Id": "debug"}
        failed.raise_for_status.side_effect = requests.HTTPError(str(status_code))
        return failed

    def test_a_lost_answer_is_asked_again_under_the_same_request_id(self):
        _, api, _ = self._refund(self.lot, 100, side_effect=[self._failed(503), self._refund_response()])
        self.assertEqual(api.call_count, 2)
        ids = {call.kwargs["headers"]["PayPal-Request-Id"] for call in api.call_args_list}
        self.assertEqual(len(ids), 1)
        self.assertEqual(InvoicePayment.objects.filter(payment_method="PayPal Refund").count(), 1)

    def test_no_answer_marks_the_lot_and_says_to_check_paypal(self):
        import requests

        result, _, _ = self._refund(self.lot, 100, side_effect=requests.ConnectionError("timeout"))
        self.assertIn("Check PayPal", result[1])
        self.lot.refresh_from_db()
        # It may have gone through, so it's never sent again.
        self.assertTrue(self.lot.no_more_refunds_possible)
        self.assertFalse(InvoicePayment.objects.filter(payment_method="PayPal Refund").exists())

    def test_a_pending_refund_marks_the_lot_and_waits_for_the_webhook(self):
        result, _, _ = self._refund(self.lot, 100, response=self._refund_response(status="PENDING"))
        self.assertIn("still processing", result[1])
        self.lot.refresh_from_db()
        self.assertTrue(self.lot.no_more_refunds_possible)
        self.assertFalse(InvoicePayment.objects.filter(payment_method="PayPal Refund").exists())

    def test_failing_to_sign_in_sends_nothing(self):
        import requests

        with (
            patch("auctions.views.payments.requests.post", side_effect=requests.ConnectionError("down")),
            patch("auctions.views.payments.requests.request") as api,
        ):
            provider, error = self.lot.refund(100, self.admin_user)
        api.assert_not_called()
        self.assertIn("nothing was refunded", error)
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.no_more_refunds_possible)

    def test_the_webhook_that_follows_adds_nothing(self):
        from auctions.views import PayPalAPIMixin

        self._refund(self.lot, 100)
        PayPalAPIMixin().handle_refund(
            {
                "id": "REFUND-1",
                "amount": {"value": "12.50", "currency_code": "USD"},
                "links": [{"rel": "up", "href": "https://api.paypal.com/v2/payments/captures/CAPTURE-1"}],
            }
        )
        self.assertEqual(InvoicePayment.objects.filter(external_id="REFUND-1").count(), 1)
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.amount_available_to_refund, Decimal("17.50"))

    def test_a_connected_seller_is_refunded_on_its_own_account(self):
        PayPalSeller.objects.create(user=self.online_auction.created_by, paypal_merchant_id="SELLER-MERCHANT")
        _, api, _ = self._refund(self.lot, 100)
        self.assertIn("PayPal-Auth-Assertion", api.call_args.kwargs["headers"])

    def test_square_still_comes_first(self):
        InvoicePayment.objects.create(
            invoice=self.invoiceB,
            external_id="SQ-1",
            amount=Decimal("30.00"),
            amount_available_to_refund=Decimal("30.00"),
            payment_method="square",
        )
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).card_refund_provider, "Square")

    def test_refund_dialog_says_paypal_will_refund(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("lot_refund", kwargs={"pk": self.lot.pk}))
        self.assertContains(response, "PayPal refund will be automatically issued")

    def test_refund_dialog_refunds_through_paypal(self):
        self.client.login(username="my_lot", password="testpassword")
        with (
            patch("auctions.views.payments.requests.post", return_value=self._token()),
            patch("auctions.views.payments.requests.request", return_value=self._refund_response()) as api,
        ):
            self.client.post(reverse("lot_refund", kwargs={"pk": self.lot.pk}), {"partial_refund_percent": 100})
        api.assert_called_once()
        self.assertTrue(InvoicePayment.objects.filter(external_id="REFUND-1").exists())

    def test_a_bidder_cannot_refund_through_the_dialog(self):
        self.client.login(username="no_tos", password="testpassword")
        with patch("auctions.views.payments.requests.request") as api:
            response = self.client.post(
                reverse("lot_refund", kwargs={"pk": self.lot.pk}), {"partial_refund_percent": 100}
            )
        self.assertIn(response.status_code, (302, 403))
        api.assert_not_called()

    def test_the_palette_offers_no_undo_once_the_card_was_refunded(self):
        from django.test import RequestFactory

        from auctions.palette_actions import refund_lot

        request = RequestFactory().post("/")
        request.user = self.user
        with (
            patch("auctions.views.payments.requests.post", return_value=self._token()),
            patch("auctions.views.payments.requests.request", return_value=self._refund_response()),
        ):
            result = refund_lot(request, {"lot_id": self.lot.pk, "percent": 100})
        self.assertTrue(result["ok"], result)
        self.assertIn("refunded through PayPal", result["summary"])
        self.assertNotIn("undo", result)
        self.assertNotIn("buyer's invoice is", result["summary"])
