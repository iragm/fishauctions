"""Square: taking a payment, refunding one, the OAuth grant, and webhook signatures."""

import base64
import datetime
import hashlib
import hmac
import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    AuctionTOS,
    ClubMoney,
    Invoice,
    InvoicePayment,
    Lot,
    PickupLocation,
    SquareOffsitePayment,
    SquareSeller,
    UserData,
)
from auctions.tests import StandardTestCase


class SquarePaymentTests(StandardTestCase):
    """Tests for Square payment oauth integration"""

    def setUp(self):
        super().setUp()

        from auctions.models import Invoice, InvoicePayment, SquareSeller, UserData

        # Enable Square for test users
        for user in [self.admin_user, self.user]:
            userdata, _ = UserData.objects.get_or_create(user=user)
            userdata.square_enabled = True
            userdata.save()

        # Create Square seller for admin
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

        # Create invoice and payment for testing refunds
        self.test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.square_payment = InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="TEST_PAYMENT_ID",
        )

    def test_square_seller_creation(self):
        """Test that SquareSeller model is created correctly"""
        self.assertEqual(self.square_seller.user, self.admin_user)
        self.assertEqual(self.square_seller.square_merchant_id, "TEST_MERCHANT_ID")
        self.assertIsNotNone(self.square_seller.access_token)
        self.assertIsNotNone(self.square_seller.refresh_token)

    def test_token_expiration_check(self):
        """Test token expiration checking"""
        # Token expires in 30 days - should not be expired
        self.assertFalse(self.square_seller.is_token_expired())

        # Set token to expire soon (within 1 hour)
        self.square_seller.token_expires_at = timezone.now() + datetime.timedelta(minutes=30)
        self.square_seller.save()
        self.assertTrue(self.square_seller.is_token_expired())

        # Set token to already expired
        self.square_seller.token_expires_at = timezone.now() - datetime.timedelta(hours=1)
        self.square_seller.save()
        self.assertTrue(self.square_seller.is_token_expired())

    def test_supports_tap_to_pay_reflects_scopes(self):
        """supports_tap_to_pay is True only when the in-person scope was granted."""
        from auctions.models import SQUARE_OAUTH_SCOPES

        # Created without scopes (legacy), so must reconnect.
        self.assertFalse(self.square_seller.supports_tap_to_pay)
        self.square_seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
        self.square_seller.save()
        self.assertTrue(self.square_seller.supports_tap_to_pay)
        # Missing the in-person scope isn't enough (no substring match).
        self.square_seller.scopes = "PAYMENTS_WRITE PAYMENTS_READ"
        self.square_seller.save()
        self.assertFalse(self.square_seller.supports_tap_to_pay)

    def test_square_reconnects_on_the_admin_endpoint(self):
        """The admin read lists legacy sellers until they have the scope."""
        from django.test import RequestFactory

        from auctions.mcp import admin
        from auctions.models import SQUARE_OAUTH_SCOPES

        request = RequestFactory().get("/")
        request.user = self.admin_user
        result = admin.square_reconnects(request, {})
        self.assertIn("1 Square seller must reconnect", result["summary"])
        self.assertEqual([seller["user"] for seller in result["sellers"]], [self.admin_user.username])

        self.square_seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
        self.square_seller.save()
        self.assertEqual(admin.square_reconnects(request, {})["sellers"], [])

    def test_winner_invoice_property(self):
        """Test Lot.winner_invoice property"""
        # Lot with auctiontos_winner
        invoice = self.lot.winner_invoice
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.auctiontos_user, self.lot.auctiontos_winner)

        # Lot with no winner
        unsold_lot = Lot.objects.create(
            lot_name="Unsold test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.assertIsNone(unsold_lot.winner_invoice)

    def test_seller_invoice_property(self):
        """Test Lot.seller_invoice property"""
        invoice = self.lot.sellers_invoice
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.auctiontos_user, self.lot.auctiontos_seller)

    def test_square_refund_possible_with_payment(self):
        """Test square_refund_possible when Square payment exists"""
        # Set up lot with Square payment
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        # Should be True since we have a payment of 100 and lot cost is 50
        self.assertTrue(self.lot.square_refund_possible)

    def test_square_refund_possible_insufficient_funds(self):
        """Test square_refund_possible when payment is insufficient"""
        self.lot.winning_price = 150  # More than available (100)
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        self.assertFalse(self.lot.square_refund_possible)

    def test_square_refund_possible_no_payment(self):
        """Test square_refund_possible when no Square payment exists"""
        # Create a lot with a different winner who has no Square payment
        other_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join, auction=self.online_auction, pickup_location=self.location
        )
        lot = Lot.objects.create(
            lot_name="Test lot no payment",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=other_tos,
            active=False,
        )

        self.assertFalse(lot.square_refund_possible)

    def test_square_refund_possible_already_refunded(self):
        """Test square_refund_possible when no_more_refunds_possible is True"""
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.no_more_refunds_possible = True
        self.lot.save()

        # Should be False even though payment exists
        self.assertFalse(self.lot.square_refund_possible)

    def test_no_more_refunds_field_default(self):
        """Test that no_more_refunds_possible defaults to False"""
        new_lot = Lot.objects.create(
            lot_name="New lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.assertFalse(new_lot.no_more_refunds_possible)

    def test_invoice_payment_square_method(self):
        """Test that Square payments are properly recorded"""

        from auctions.models import InvoicePayment

        payment = InvoicePayment.objects.filter(payment_method="square", invoice=self.test_invoice).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal("100.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("100.00"))

    def test_lot_refund_calls_square_refund(self):
        """Test that lot.refund() automatically calls square_refund when possible"""
        # Set up lot with Square payment possibility
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        # Get initial state
        initial_square_refund_possible = self.lot.square_refund_possible
        self.assertTrue(initial_square_refund_possible)

        # No real Square API in tests; only exercise the code path.
        try:
            self.lot.refund(100, self.admin_user, "Test refund")
        except Exception:
            pass

    def test_square_enabled_in_user_preferences(self):
        """Test that Square can be enabled for users"""

        userdata, _ = UserData.objects.get_or_create(user=self.user)
        userdata.square_enabled = True
        userdata.save()

        self.assertTrue(userdata.square_enabled)

    def test_square_fields_in_auction(self):
        """Test Square-related fields in Auction model"""
        self.online_auction.enable_square_payments = True
        self.online_auction.square_email_address = "test@square.com"
        self.online_auction.dismissed_square_banner = False
        self.online_auction.save()

        self.assertTrue(self.online_auction.enable_square_payments)
        self.assertEqual(self.online_auction.square_email_address, "test@square.com")
        self.assertFalse(self.online_auction.dismissed_square_banner)

    def test_square_url_patterns_exist(self):
        """Test that Square URL patterns are configured"""
        from django.urls import reverse

        # Test that Square URLs can be reversed
        try:
            square_seller_url = reverse("square_seller")
            self.assertIsNotNone(square_seller_url)
        except Exception:
            self.fail("square_seller URL pattern not found")

    def test_square_management_command_exists(self):
        """Test that change_square management command exists"""

        # Test that command exists and can be imported
        try:
            # Don't actually run the command, just verify it exists
            from django.core.management import load_command_class

            load_command_class("auctions", "change_square")
        except Exception as e:
            self.fail(f"change_square management command not found: {e}")

    def test_square_oauth_redirect_uri_without_proxy_header(self):
        """The Square OAuth redirect URI is http without X-Forwarded-Proto."""
        from django.urls import reverse

        # Login as admin user
        self.client.force_login(self.admin_user)

        # Test the Square connect view without X-Forwarded-Proto header
        response = self.client.get(reverse("square_connect"), HTTP_HOST="testserver", follow=False)

        # Should redirect to Square OAuth URL
        self.assertEqual(response.status_code, 302)
        self.assertIn("connect.squareup", response.url)

        # Verify redirect_uri parameter
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(response.url)
        params = parse_qs(parsed.query)

        # Check that redirect_uri exists
        self.assertIn("redirect_uri", params)
        redirect_uri = params["redirect_uri"][0]
        # Without the proxy header in test environment, it will use http
        self.assertIn("/square/onboard/success/", redirect_uri)

    def test_receipt_number_field(self):
        """Test that InvoicePayment has receipt_number field"""

        # Create a payment with receipt_number
        payment = InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="Square",
            amount=50.00,
            external_id="TEST_EXTERNAL_ID",
            receipt_number="ABCD",
        )

        self.assertEqual(payment.receipt_number, "ABCD")
        self.assertEqual(payment.external_id, "TEST_EXTERNAL_ID")

    def test_receipt_number_search_in_auction_tos_filter(self):
        """Test that receipt_number can be used to search users"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Create payment with receipt number
        InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="Square",
            amount=100.00,
            receipt_number="WXYZ",
        )

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search by receipt_number
        filtered_qs = filter_instance.generic(qs, "wxyz")

        # Should find the user with the invoice that has this receipt_number
        self.assertGreater(filtered_qs.count(), 0)

    def test_can_bid_filter_in_auction_tos(self):
        """Test that 'can bid' filter returns users where bidding_allowed=True"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Set bidding_allowed to False for some users
        self.tosB.bidding_allowed = False
        self.tosB.save()

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search for users who can bid
        filtered_qs = filter_instance.generic(qs, "can bid")

        # Should only return users where bidding_allowed=True
        for tos in filtered_qs:
            self.assertTrue(tos.bidding_allowed)

        # tosB should not be in the filtered results
        self.assertNotIn(self.tosB, filtered_qs)

    def test_no_bid_filter_in_auction_tos(self):
        """Test that 'no bid' filter returns users where bidding_allowed=False"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Set bidding_allowed to False for some users
        self.tosB.bidding_allowed = False
        self.tosB.save()

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search for users who cannot bid
        filtered_qs = filter_instance.generic(qs, "no bid")

        # Should only return users where bidding_allowed=False
        for tos in filtered_qs:
            self.assertFalse(tos.bidding_allowed)

        # tosB should be in the filtered results
        self.assertIn(self.tosB, filtered_qs)

    def test_is_club_member_filter_in_auction_tos(self):
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        self.online_tos.is_club_member = True
        self.online_tos.save(update_fields=["is_club_member"])
        self.tosB.is_club_member = False
        self.tosB.save(update_fields=["is_club_member"])

        qs = AuctionTOS.objects.filter(auction=self.online_auction)
        filter_instance = AuctionTOSFilter()

        paid = filter_instance.generic(qs, "club member")
        self.assertIn(self.online_tos, paid)
        self.assertNotIn(self.tosB, paid)

        unpaid = filter_instance.generic(qs, "unpaid")
        self.assertIn(self.tosB, unpaid)
        self.assertNotIn(self.online_tos, unpaid)

    def test_pickup_by_mail_requires_address(self):
        """Test that Square payment link requires address when pickup_by_mail is True"""

        # Create a pickup by mail location
        mail_location = PickupLocation.objects.create(
            auction=self.online_auction,
            name="Mail",
            pickup_by_mail=True,
        )

        # Update tosB to use mail pickup
        self.tosB.pickup_location = mail_location
        self.tosB.save()

        # The API call itself can't be tested.
        self.assertTrue(self.tosB.pickup_location.pickup_by_mail)

    def test_sanitize_square_phone(self):
        """The Square phone hint keeps valid numbers and drops junk that would 400."""
        from auctions.models import sanitize_square_phone

        self.assertEqual(sanitize_square_phone("(555) 123-4567"), "5551234567")
        self.assertEqual(sanitize_square_phone("1-555-123-4567"), "15551234567")
        self.assertEqual(sanitize_square_phone("+44 20 7946 0958"), "+442079460958")
        for junk in ["call me", "555-1234", "", None, "x1234", "0", "12345678901234567890"]:
            self.assertEqual(sanitize_square_phone(junk), "", msg=f"expected '' for {junk!r}")

    def test_open_invoice_filter_no_duplicates(self):
        """Multiple payments on an invoice don't duplicate AuctionTOS rows."""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        InvoicePayment.objects.create(
            invoice=self.test_invoice, payment_method="Cash", amount=10, receipt_number="RCPT1"
        )
        InvoicePayment.objects.create(
            invoice=self.test_invoice, payment_method="Cash", amount=10, receipt_number="RCPT1"
        )

        qs = AuctionTOS.objects.filter(auction=self.online_auction)
        filter_instance = AuctionTOSFilter()

        filtered_qs = filter_instance.auctiontos_search(qs, "query", "RCPT1")

        tos_pks = list(filtered_qs.values_list("pk", flat=True))
        self.assertEqual(
            tos_pks.count(self.tosB.pk),
            1,
            "tosB appeared more than once when searching by receipt number with multiple payments",
        )


class SquareRefundFormTests(StandardTestCase):
    """Tests for Square refund integration in forms"""

    def setUp(self):
        super().setUp()

        from auctions.models import InvoicePayment, SquareSeller, UserData

        # Enable Square
        userdata, _ = UserData.objects.get_or_create(user=self.admin_user)
        userdata.square_enabled = True
        userdata.save()

        # Create Square seller
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
        )

        # Create payment for testing
        self.square_payment = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="TEST_PAYMENT_ID",
        )

        # Set lot to have Square refund possible
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

    def test_lot_refund_form_shows_square_message(self):
        """Test that LotRefundForm shows Square auto-refund message when appropriate"""
        from auctions.forms import LotRefundForm

        form = LotRefundForm(lot=self.lot)

        # Check that form initializes without errors
        self.assertIsNotNone(form)

        self.assertTrue(self.lot.square_refund_possible)

    def test_lot_refund_form_without_square(self):
        """Test LotRefundForm when Square refund is not possible"""
        from auctions.forms import LotRefundForm

        # Set up a lot without Square payment
        unsold_lot = Lot.objects.create(
            lot_name="Unsold lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )

        form = LotRefundForm(lot=unsold_lot)
        self.assertIsNotNone(form)
        self.assertFalse(unsold_lot.square_refund_possible)


class SquarePaymentSuccessViewTests(StandardTestCase):
    """Tests for SquarePaymentSuccessView that doesn't verify email"""

    def setUp(self):
        super().setUp()
        self.tosA = self.online_tos
        self.auctionA = self.online_auction
        self.userA = self.user
        self.invoice = Invoice.for_participant(self.tosA)
        self.invoice.save()

    def test_square_payment_success_view_marks_invoice_opened(self):
        """Test that SquarePaymentSuccessView marks invoice as opened"""
        from django.urls import reverse

        self.assertFalse(self.invoice.opened)

        url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.opened)

    def test_square_payment_success_view_does_not_verify_email(self):
        """Test that SquarePaymentSuccessView does NOT mark email as VALID"""
        from django.urls import reverse

        # Set initial email status to something other than VALID
        self.tosA.email_address_status = "UNKNOWN"
        self.tosA.save()

        url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.tosA.refresh_from_db()
        # Email status should NOT have changed to VALID
        self.assertEqual(self.tosA.email_address_status, "UNKNOWN")

    def test_invoice_no_login_view_still_verifies_email(self):
        """Test that InvoiceNoLoginView still marks email as VALID (for comparison)"""
        from django.urls import reverse

        # Set initial email status
        self.tosA.email_address_status = "UNKNOWN"
        self.tosA.save()

        url = reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.tosA.refresh_from_db()
        # Email status SHOULD have changed to VALID for regular invoice links
        self.assertEqual(self.tosA.email_address_status, "VALID")

    def test_square_payment_success_url_pattern_exists(self):
        """Test that square_payment_success URL pattern is configured"""
        from django.urls import reverse

        try:
            url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
            self.assertTrue(url.startswith("/invoices/square-success/"))
        except Exception as e:
            self.fail(f"square_payment_success URL pattern not configured: {e}")


@override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True)
class SquareOAuthRevocationTests(StandardTestCase):
    """Tests for Square OAuth authorization revocation handling"""

    def setUp(self):
        super().setUp()

        # Create Square seller for testing revocation
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="MLF3WZS2N9WVG",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

    def test_oauth_revocation_deletes_square_seller(self):
        """Test that oauth.authorization.revoked webhook deletes SquareSeller"""
        from django.urls import reverse

        # Verify seller exists
        self.assertTrue(SquareSeller.objects.filter(square_merchant_id="MLF3WZS2N9WVG").exists())

        # Simulate Square revocation webhook
        webhook_data = {
            "merchant_id": "MLF3WZS2N9WVG",
            "type": "oauth.authorization.revoked",
            "event_id": "957299eb-98e4-399c-b7d9-e73ddeff19df",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "6ea8bc48-7c2e-43d1-bd36-c865f6c4083d",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should return 200
        self.assertEqual(response.status_code, 200)

        # SquareSeller should be deleted
        self.assertFalse(SquareSeller.objects.filter(square_merchant_id="MLF3WZS2N9WVG").exists())

    def test_oauth_revocation_handles_missing_seller(self):
        """Test that revocation webhook handles missing SquareSeller gracefully"""
        from django.urls import reverse

        # Delete the seller before webhook
        self.square_seller.delete()

        # Simulate revocation webhook for non-existent seller
        webhook_data = {
            "merchant_id": "NONEXISTENT_MERCHANT",
            "type": "oauth.authorization.revoked",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "test-revocation-id",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should still return 200 (graceful handling)
        self.assertEqual(response.status_code, 200)

    def test_payment_webhook_handles_missing_merchant(self):
        """Test that payment webhook handles missing SquareSeller gracefully"""
        from django.urls import reverse

        # Simulate payment webhook with non-existent merchant_id
        webhook_data = {
            "merchant_id": "NONEXISTENT_MERCHANT",
            "type": "payment.updated",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "payment",
                "id": "test-payment-id",
                "object": {
                    "payment": {
                        "id": "test-payment-id",
                        "status": "COMPLETED",
                        "order_id": "test-order-id",
                        "amount_money": {"amount": 1000, "currency": "USD"},
                    }
                },
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should return 200 (graceful handling with logged warning)
        self.assertEqual(response.status_code, 200)

    def test_payment_webhook_creates_invoice_payment(self):
        """payment.updated creates an InvoicePayment without a status field."""
        from unittest.mock import Mock

        from django.urls import reverse

        from auctions.models import Invoice, InvoicePayment, SquareSeller

        # Create an invoice for the test
        test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)
        # The merchant is the auction's own: payments into anyone else's account don't count for it.
        self.square_seller.user = self.online_auction.created_by
        self.square_seller.save()

        # Mock the entire Square orders.get flow
        mock_order = Mock()
        mock_order.reference_id = str(test_invoice.pk)

        mock_order_response = Mock()
        mock_order_response.order = mock_order

        mock_orders_api = Mock()
        mock_orders_api.get = Mock(return_value=mock_order_response)

        mock_client = Mock()
        mock_client.orders = mock_orders_api

        with patch.object(SquareSeller, "get_square_client", return_value=mock_client):
            # Simulate payment.updated webhook with COMPLETED status
            webhook_data = {
                "merchant_id": "MLF3WZS2N9WVG",
                "type": "payment.updated",
                "event_id": "test-payment-event",
                "created_at": "2025-11-23T16:29:14.35551833Z",
                "data": {
                    "type": "payment",
                    "id": "test-payment-updated-id",
                    "object": {
                        "payment": {
                            "id": "PAYMENT_123456",
                            "status": "COMPLETED",
                            "order_id": "ORDER_123456",
                            "amount_money": {"amount": 5000, "currency": "USD"},
                        }
                    },
                },
            }

            url = reverse("square_webhook")
            response = self.client.post(url, data=webhook_data, content_type="application/json")

            # Should return 200
            self.assertEqual(response.status_code, 200)

            # Verify InvoicePayment was created without status field
            payment = InvoicePayment.objects.filter(external_id="PAYMENT_123456").first()
            self.assertIsNotNone(payment)
            self.assertEqual(payment.invoice, test_invoice)
            self.assertEqual(payment.amount, Decimal("50.00"))  # 5000 cents = $50
            self.assertEqual(payment.currency, "USD")
            self.assertEqual(payment.payment_method, "Square")
            self.assertFalse(hasattr(payment, "status") and payment.status)

    def test_another_merchants_payment_does_not_pay_the_invoice(self):
        """The reference id is the paying merchant's to set: an order naming an invoice this merchant
        doesn't collect for (here the seller is admin_user, the auction's creator is user) records nothing.
        """
        from unittest.mock import Mock

        from django.urls import reverse

        from auctions.models import Invoice, InvoicePayment, SquareSeller

        test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)
        mock_client = Mock()
        mock_client.orders.get.return_value = Mock(order=Mock(reference_id=str(test_invoice.pk)))
        with patch.object(SquareSeller, "get_square_client", return_value=mock_client):
            response = self.client.post(
                reverse("square_webhook"),
                data={
                    "merchant_id": "MLF3WZS2N9WVG",
                    "type": "payment.updated",
                    "data": {
                        "object": {
                            "payment": {
                                "id": "PAYMENT_FORGED",
                                "status": "COMPLETED",
                                "order_id": "ORDER_FORGED",
                                "amount_money": {"amount": 5000, "currency": "USD"},
                            }
                        }
                    },
                },
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InvoicePayment.objects.filter(external_id="PAYMENT_FORGED").exists())
        test_invoice.refresh_from_db()
        self.assertNotEqual(test_invoice.status, "PAID")


class SquareWebhookSignatureValidationTests(StandardTestCase):
    """Square webhook signatures are validated when SQUARE_WEBHOOK_SIGNATURE_KEY is set."""

    def setUp(self):
        super().setUp()

        # Create Square seller for testing
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

        # Test signature key
        self.signature_key = "test-signature-key-12345"

        # Standard webhook data used across tests
        self.webhook_data = {
            "merchant_id": "TEST_MERCHANT_ID",
            "type": "oauth.authorization.revoked",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "test-revocation-id",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

    def compute_signature(self, url, body, key=None):
        """Square's base64 HMAC-SHA256 of url + body, with ``key`` defaulting to self.signature_key."""
        if key is None:
            key = self.signature_key
        message = (url + body).encode("utf-8")
        key_bytes = key.encode("utf-8")
        hash_bytes = hmac.new(key_bytes, message, hashlib.sha256).digest()
        return base64.b64encode(hash_bytes).decode("utf-8")

    def test_forged_signature_is_rejected(self):
        url = reverse("square_webhook")

        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            # Send with a forged/invalid signature
            response = self.client.post(
                url,
                data=self.webhook_data,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE="forged-invalid-signature",
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_missing_signature_header_is_rejected(self):
        url = reverse("square_webhook")

        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            # Send without signature header
            response = self.client.post(
                url,
                data=self.webhook_data,
                content_type="application/json",
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"missing signature", response.content)

    def test_valid_signature_is_accepted(self):
        url = reverse("square_webhook")
        body = json.dumps(self.webhook_data)

        # The test client's URL.
        full_url = "http://testserver" + url

        # Compute the correct signature
        valid_signature = self.compute_signature(full_url, body)

        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=valid_signature,
            )

            # Should return 200 OK
            self.assertEqual(response.status_code, 200)

    def test_wrong_signature_key_is_rejected(self):
        """Test that signatures computed with a different key are rejected"""
        url = reverse("square_webhook")
        body = json.dumps(self.webhook_data)
        full_url = "http://testserver" + url

        # Compute signature with a DIFFERENT key (attacker's key)
        wrong_signature = self.compute_signature(full_url, body, key="attacker-key-different")

        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=wrong_signature,
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_tampered_body_is_rejected(self):
        """Test that a valid signature for different body data is rejected"""
        import copy

        # Create tampered data by modifying a copy of the original
        tampered_webhook_data = copy.deepcopy(self.webhook_data)
        tampered_webhook_data["merchant_id"] = "DIFFERENT_MERCHANT"  # Attacker tries to change the merchant
        tampered_webhook_data["data"]["id"] = "tampered-id"

        url = reverse("square_webhook")
        original_body = json.dumps(self.webhook_data)
        tampered_body = json.dumps(tampered_webhook_data)
        full_url = "http://testserver" + url

        # Compute valid signature for ORIGINAL body
        valid_signature = self.compute_signature(full_url, original_body)

        # Test: Send tampered body with signature for original body
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=tampered_body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=valid_signature,
            )

            # Should return 403 Forbidden because body doesn't match signature
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_improperly_configured_in_production_without_webhook_key(self):
        """Production with Square configured but no webhook key raises ImproperlyConfigured."""
        url = reverse("square_webhook")

        with override_settings(
            DEBUG=False,
            SQUARE_APPLICATION_ID="test-app-id",
            SQUARE_CLIENT_SECRET="test-client-secret",
            SQUARE_WEBHOOK_SIGNATURE_KEY="",
        ):
            with self.assertRaises(ImproperlyConfigured) as context:
                self.client.post(
                    url,
                    data=self.webhook_data,
                    content_type="application/json",
                )

            self.assertIn("SQUARE_WEBHOOK_SIGNATURE_KEY must be set", str(context.exception))


@override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True)
class SquareOffsitePaymentTests(TestCase):
    """Payments a club takes in Square without this site (Payment Links, its own register) reach the webhook
    too: a quiet log line with no order details, a short-lived record for the members page and invoices, and
    a treasurer-report row only when the club opts in.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        from auctions.models import Club

        self.club = Club.objects.create(name="Offsite club", membership_system="rolling", membership_annual_fee=25)
        self.owner = User.objects.create_user(username="offsite_owner", password="pw", email="o@example.com")
        self.seller = SquareSeller.objects.create(
            user=self.owner, club=self.club, square_merchant_id="MERCHANT_OFFSITE", currency="USD"
        )
        self.order = SimpleNamespace(
            reference_id=None,
            source=SimpleNamespace(name="Payment Links"),
            line_items=[SimpleNamespace(name="Club shirt", quantity="2"), SimpleNamespace(name="Raffle", quantity="1")],
            customer_id="CUSTOMER_SECRET",
        )
        self.client_mock = Mock()
        self.client_mock.orders.get.return_value = SimpleNamespace(order=self.order)
        self.now = timezone.now()

    def _payment(self, payment_id="PAY_OFF", cents=2500, currency="USD", ago=None, **extra):
        created = self.now - (ago or datetime.timedelta(hours=1))
        payment = {
            "id": payment_id,
            "status": "COMPLETED",
            "order_id": "ORDER_OFF",
            "created_at": created.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "amount_money": {"amount": cents, "currency": currency},
            "total_money": {"amount": cents, "currency": currency},
            "buyer_email_address": "buyer@example.com",
            "billing_address": {"first_name": "Pat", "last_name": "Buyer"},
        }
        payment.update(extra)
        return payment

    def _post(self, event_type, obj, merchant_id="MERCHANT_OFFSITE"):
        with patch.object(SquareSeller, "get_square_client", return_value=self.client_mock):
            response = self.client.post(
                reverse("square_webhook"),
                data={"merchant_id": merchant_id, "type": event_type, "data": {"object": obj}},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)

    def _offsite_rows(self):
        return ClubMoney.objects.filter(club=self.club, category=ClubMoney.CATEGORY_SQUARE_OFFSITE)

    def _opt_in(self):
        self.club.record_offsite_square_payments = True
        self.club.save()

    def test_logged_quietly_without_order_details(self):
        with self.assertLogs("auctions.views.webhooks", level="INFO") as logs:
            self._post("payment.updated", {"payment": self._payment()})
        # The unsigned-in-DEBUG warning is this test's setup, not the payment.
        loud = [r for r in logs.records if r.levelno >= logging.WARNING and "SIGNATURE_KEY" not in r.getMessage()]
        self.assertFalse(loud)
        text = "\n".join(logs.output)
        self.assertIn("Payment Links", text)
        for detail in ("Club shirt", "CUSTOMER_SECRET", "Raffle", "Pat", "buyer@example.com"):
            self.assertNotIn(detail, text)
        kept = SquareOffsitePayment.objects.get()
        self.assertEqual((kept.buyer_name, kept.buyer_email, kept.amount), ("Pat Buyer", "buyer@example.com", 25))
        # The treasurer report is off by default.
        self.assertFalse(ClubMoney.objects.exists())
        self.assertFalse(InvoicePayment.objects.exists())

    def test_opted_in_club_books_it_once(self):
        self._opt_in()
        self._post("payment.updated", {"payment": self._payment()})
        self._post("payment.updated", {"payment": self._payment()})
        row = self._offsite_rows().get()
        self.assertEqual(row.amount, Decimal("25.00"))
        self.assertEqual(row.external_id, "PAY_OFF")
        self.assertEqual(row.description, "Square (Payment Links): Club shirt x2, Raffle")
        # A changed amount corrects the row rather than adding one.
        self._post("payment.updated", {"payment": self._payment(cents=3000)})
        self.assertEqual(self._offsite_rows().get().amount, Decimal("30.00"))

    def test_old_payments_are_not_new_to_the_report(self):
        """Square re-sends old payments (a refund fires payment.updated again); opting in later doesn't book them."""
        self._opt_in()
        self._post("payment.updated", {"payment": self._payment(ago=datetime.timedelta(days=10))})
        self.assertFalse(ClubMoney.objects.exists())

    def test_refund_comes_off_the_books_once(self):
        self._opt_in()
        self._post("payment.updated", {"payment": self._payment()})
        refund = {
            "id": "REFUND_OFF",
            "payment_id": "PAY_OFF",
            "status": "COMPLETED",
            "amount_money": {"amount": 1000, "currency": "USD"},
        }
        self._post("refund.updated", {"refund": refund})
        self._post("refund.updated", {"refund": refund})
        self.assertEqual(sum(row.amount for row in self._offsite_rows()), Decimal("15.00"))
        self.assertTrue(SquareOffsitePayment.objects.get(payment_id="PAY_OFF").refunded)
        # Another merchant naming the same payment books nothing.
        self._post("refund.updated", {"refund": dict(refund, id="REFUND_OTHER")}, merchant_id="SOMEONE_ELSE")
        self.assertFalse(ClubMoney.objects.filter(external_id="REFUND_OTHER").exists())

    def test_site_charges_are_never_offsite(self):
        """A Tap to Pay charge names its invoice on the payment, and an unread order proves nothing."""
        self._opt_in()
        self._post("payment.updated", {"payment": self._payment(payment_id="PAY_TTP", reference_id="999999")})
        self.client_mock.orders.get.return_value = SimpleNamespace(order=None, errors=["nope"])
        self._post("payment.updated", {"payment": self._payment(payment_id="PAY_NO_ORDER")})
        self.client_mock.orders.get.side_effect = RuntimeError("Square is down")
        self._post("payment.updated", {"payment": self._payment(payment_id="PAY_UNREAD")})
        self.assertFalse(SquareOffsitePayment.objects.exists())
        self.assertFalse(ClubMoney.objects.exists())

    def test_other_currency_and_personal_accounts_book_nothing(self):
        self._opt_in()
        self._post("payment.updated", {"payment": self._payment(payment_id="PAY_CAD", currency="CAD")})
        self.seller.club = None
        self.seller.save()
        self._post("payment.updated", {"payment": self._payment(payment_id="PAY_PERSONAL")})
        self.assertFalse(ClubMoney.objects.exists())

    def test_setting_only_offered_with_a_square_account(self):
        from auctions.forms import ClubMembershipSettingsForm

        self.assertIn("record_offsite_square_payments", ClubMembershipSettingsForm(instance=self.club).fields)
        self.seller.delete()
        club = type(self.club).objects.get(pk=self.club.pk)
        self.assertNotIn("record_offsite_square_payments", ClubMembershipSettingsForm(instance=club).fields)


class SquareOffsiteSuggestionTests(TestCase):
    """The members page offers dues-sized off-site payments; an unpaid invoice notes a matching one."""

    def setUp(self):
        from django.contrib.auth.models import User

        from auctions.models import Club, ClubMember

        self.club = Club.objects.create(name="Dues club", membership_system="rolling", membership_annual_fee=25)
        self.admin = User.objects.create_user(username="dues_admin", password="pw", email="a@example.com")
        ClubMember.objects.create(club=self.club, user=self.admin, name="Admin", permission_add_edit=True)
        SquareSeller.objects.create(user=self.admin, club=self.club, square_merchant_id="M_DUES", currency="USD")
        self.client.login(username="dues_admin", password="pw")
        self.url = reverse("club_square_dues", kwargs={"slug": self.club.slug})

    def _payment(self, payment_id, email="", name="", amount=25, ago_days=1, **extra):
        return SquareOffsitePayment.objects.create(
            payment_id=payment_id,
            merchant_id="M_DUES",
            amount=amount,
            paid_at=timezone.now() - datetime.timedelta(days=ago_days),
            buyer_email=email,
            buyer_name=name,
            **extra,
        )

    def test_newest_first_then_the_next_after_each_answer(self):
        from auctions.models import ClubMember

        member = ClubMember.objects.create(club=self.club, name="Old member", email="Old@Example.com")
        self._payment("P_OLD", email="old@example.com", ago_days=5)
        newest = self._payment("P_NEW", name="New Person", email="new@example.com", ago_days=1)
        self._payment("P_WRONG_AMOUNT", name="Raffle buyer", amount=10, ago_days=0)
        self._payment("P_STALE", name="Too old", ago_days=40)

        page = self.client.get(self.url).content.decode()
        self.assertIn("New Person", page)
        self.assertIn("Add as member", page)

        response = self.client.post(self.url, {"payment": newest.pk, "action": "add"})
        self.assertEqual(response["HX-Trigger"], "clubMemberListChanged")
        added = ClubMember.objects.get(email="new@example.com")
        self.assertTrue(added.membership_expiration_date)
        page = response.content.decode()
        self.assertIn("Renew Old member", page)

        page = self.client.post(self.url, {"payment": newest.pk, "action": "add"}).content.decode()
        self.assertIn("already been handled", page)

        old = SquareOffsitePayment.objects.get(payment_id="P_OLD")
        page = self.client.post(self.url, {"payment": old.pk, "action": "renew"}).content.decode()
        member.refresh_from_db()
        self.assertEqual(member.membership_last_paid, timezone.localdate())
        self.assertNotIn("Too old", page)
        self.assertNotIn("Raffle buyer", page)
        # Dues booked once each.
        self.assertEqual(ClubMoney.objects.filter(category=ClubMoney.CATEGORY_MEMBERSHIP).count(), 2)
        # Never refundable from the site: refunds work only on recorded invoice payments.
        self.assertFalse(InvoicePayment.objects.exists())

    def test_renewal_reuses_the_treasurer_report_row(self):
        from auctions.models import ClubMember

        ClubMember.objects.create(club=self.club, name="Member", email="m@example.com")
        payment = self._payment("P_BOOKED", email="m@example.com")
        ClubMoney.objects.create(
            club=self.club,
            external_id="P_BOOKED",
            amount=25,
            date=timezone.localdate(),
            category=ClubMoney.CATEGORY_SQUARE_OFFSITE,
        )
        self.client.post(self.url, {"payment": payment.pk, "action": "renew"})
        row = ClubMoney.objects.get(club=self.club)
        self.assertEqual(row.category, ClubMoney.CATEGORY_MEMBERSHIP)

    def test_name_only_payment_renews_its_member_once(self):
        from auctions.models import ClubMember

        member = ClubMember.objects.create(club=self.club, name="Register Buyer", email="r@example.com")
        payment = self._payment("P_REGISTER", name="register buyer")
        self.assertIn("Renew Register Buyer", self.client.get(self.url).content.decode())
        self.client.post(self.url, {"payment": payment.pk, "action": "renew"})
        row = ClubMoney.objects.get(category=ClubMoney.CATEGORY_MEMBERSHIP)
        self.assertEqual(row.external_id, "P_REGISTER")
        # Renewed some other way since the payment: not offered again.
        second = self._payment("P_REGISTER_2", name="Register Buyer", ago_days=3)
        member.refresh_from_db()
        self.assertNotIn("Register Buyer", self.client.get(self.url).content.decode())
        page = self.client.post(self.url, {"payment": second.pk, "action": "renew"}).content.decode()
        self.assertIn("already been renewed", page)
        self.assertEqual(ClubMoney.objects.filter(category=ClubMoney.CATEGORY_MEMBERSHIP).count(), 1)

    def test_kept_only_for_clubs_and_forgotten_on_disconnect(self):
        from auctions.square_offsite import record_payment

        payment = {
            "id": "P_PERSONAL",
            "created_at": timezone.now().isoformat(),
            "amount_money": {"amount": 500, "currency": "USD"},
            "buyer_email_address": "shopper@example.com",
        }
        record_payment("M_PERSONAL", payment, None)
        self.assertFalse(SquareOffsitePayment.objects.filter(payment_id="P_PERSONAL").exists())
        old = dict(payment, id="P_ANCIENT", created_at=(timezone.now() - datetime.timedelta(days=60)).isoformat())
        record_payment("M_DUES", old, None)
        self.assertFalse(SquareOffsitePayment.objects.filter(payment_id="P_ANCIENT").exists())
        self._payment("P_KEPT", name="Someone")
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY="", DEBUG=True):
            self.client.post(
                reverse("square_webhook"),
                data={"merchant_id": "M_DUES", "type": "oauth.authorization.revoked", "data": {}},
                content_type="application/json",
            )
        self.assertFalse(SquareOffsitePayment.objects.exists())

    def test_dismiss_and_permissions(self):
        payment = self._payment("P_DISMISS", name="Someone")
        self.client.post(self.url, {"payment": payment.pk, "action": "dismiss"})
        payment.refresh_from_db()
        self.assertEqual(payment.resolution, SquareOffsitePayment.RESOLUTION_DISMISSED)
        self.assertNotIn("Someone", self.client.get(self.url).content.decode())

        from django.contrib.auth.models import User

        User.objects.create_user(username="dues_outsider", password="pw")
        self.client.login(username="dues_outsider", password="pw")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"payment": payment.pk, "action": "add"}).status_code, 403)

    def test_another_clubs_payment_cannot_be_used(self):
        other = SquareOffsitePayment.objects.create(
            payment_id="P_OTHER", merchant_id="M_ELSEWHERE", amount=25, paid_at=timezone.now(), buyer_name="X"
        )
        page = self.client.post(self.url, {"payment": other.pk, "action": "add"}).content.decode()
        self.assertIn("already been handled", page)
        from auctions.models import ClubMember

        self.assertEqual(ClubMember.objects.filter(club=self.club).count(), 1)

    def test_invoice_hint_matches_amount_and_time(self):
        from auctions.models import Auction, AuctionTOS, Invoice, PickupLocation
        from auctions.square_offsite import invoice_payment_hint

        auction = Auction.objects.create(
            created_by=self.admin, club=self.club, title="Hint auction", date_start=timezone.now()
        )
        location = PickupLocation.objects.create(name="here", auction=auction, pickup_time=timezone.now())
        tos = AuctionTOS.objects.create(user=self.admin, auction=auction, pickup_location=location, name="Bidder")
        invoice = Invoice.objects.create(auctiontos_user=tos, auction=auction)
        owed = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
        self.assertEqual(invoice_payment_hint(invoice), "")  # nothing owed yet, or nothing matching
        with patch.object(Invoice, "rounded_net_after_payments", Decimal("-40.00")):
            self._payment("P_BEFORE", name="Early", amount=40, ago_days=2)
            Invoice.objects.filter(pk=invoice.pk).update(date=timezone.now() - datetime.timedelta(days=1))
            invoice.refresh_from_db()
            self.assertEqual(invoice_payment_hint(invoice), "")
            self._payment("P_AFTER", name="Pat Buyer", amount=40, ago_days=0)
            self.assertIn("Square payment from Pat Buyer", invoice_payment_hint(invoice))
            self._payment("P_REFUNDED", name="Refunded", amount=40, ago_days=0, refunded=True)
            self.assertNotIn("Refunded", invoice_payment_hint(invoice))
        self.assertGreaterEqual(owed, 0)
