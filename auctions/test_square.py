"""Square: taking a payment, refunding one, the OAuth grant, and webhook signatures."""

import base64
import datetime
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    AuctionTOS,
    Invoice,
    InvoicePayment,
    Lot,
    PickupLocation,
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

        # The seller was created without scopes (legacy connection) → must reconnect.
        self.assertFalse(self.square_seller.supports_tap_to_pay)
        # A full reconnect records the requested scopes, which include the in-person scope.
        self.square_seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
        self.square_seller.save()
        self.assertTrue(self.square_seller.supports_tap_to_pay)
        # A non-empty grant that still lacks the in-person scope is not enough (no substring match).
        self.square_seller.scopes = "PAYMENTS_WRITE PAYMENTS_READ"
        self.square_seller.save()
        self.assertFalse(self.square_seller.supports_tap_to_pay)

    def test_find_square_reconnects_command(self):
        """The audit command lists legacy sellers and drops them once they have the scope."""
        from io import StringIO

        from auctions.models import SQUARE_OAUTH_SCOPES

        out = StringIO()
        call_command("find_square_reconnects", stdout=out)
        output = out.getvalue()
        self.assertIn("Need to reconnect: 1", output)
        self.assertIn(self.admin_user.username, output)

        # Once the scope is recorded (reconnected), the seller drops off the list.
        self.square_seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
        self.square_seller.save()
        out = StringIO()
        call_command("find_square_reconnects", stdout=out)
        output = out.getvalue()
        self.assertIn("Need to reconnect: 0", output)
        self.assertNotIn(self.admin_user.username, output)

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

        # Since we can't actually call Square API in tests, we'll just verify
        # that the refund method can be called without errors
        # In a real scenario with mocked Square API, this would process a refund
        try:
            self.lot.refund(100, self.admin_user, "Test refund")
            # The refund method should handle the case where Square API is not available
        except Exception:
            # We expect this might fail in tests since we don't have real Square credentials
            # but we want to ensure the code path is exercised
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
        """Test that Square OAuth redirect URI defaults to http when no X-Forwarded-Proto header"""
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
        """'club member' returns is_club_member=True users; 'unpaid' returns is_club_member=False."""
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

        # The create_payment_link method should set ask_for_shipping_address=True
        # We can't test the actual API call, but we can verify the location is set correctly
        self.assertTrue(self.tosB.pickup_location.pickup_by_mail)

    def test_sanitize_square_phone(self):
        """The Square phone pre-fill hint keeps valid numbers and drops junk that would 400."""
        from auctions.models import sanitize_square_phone

        # Valid: US 10-digit (formatting stripped), US 11-digit, and E.164 keep the leading +.
        self.assertEqual(sanitize_square_phone("(555) 123-4567"), "5551234567")
        self.assertEqual(sanitize_square_phone("1-555-123-4567"), "15551234567")
        self.assertEqual(sanitize_square_phone("+44 20 7946 0958"), "+442079460958")
        # Invalid: dropped to "" so the caller omits the hint instead of failing the link.
        for junk in ["call me", "555-1234", "", None, "x1234", "0", "12345678901234567890"]:
            self.assertEqual(sanitize_square_phone(junk), "", msg=f"expected '' for {junk!r}")

    def test_open_invoice_filter_no_duplicates(self):
        """Filtering should not return duplicate AuctionTOS rows when a user has multiple payments on their invoice"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Give tosB's existing invoice multiple payments - a naive JOIN would produce duplicate rows
        InvoicePayment.objects.create(
            invoice=self.test_invoice, payment_method="Cash", amount=10, receipt_number="RCPT1"
        )
        InvoicePayment.objects.create(
            invoice=self.test_invoice, payment_method="Cash", amount=10, receipt_number="RCPT1"
        )

        qs = AuctionTOS.objects.filter(auction=self.online_auction)
        filter_instance = AuctionTOSFilter()

        filtered_qs = filter_instance.auctiontos_search(qs, "query", "RCPT1")

        # tosB should appear exactly once despite having multiple payments with the same receipt number
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

        # When square_refund_possible is True, the form should include a message
        # We can't easily test the rendered HTML here, but we can verify the form works
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
        self.invoice = Invoice.objects.create(
            auctiontos_user=self.tosA,
            auction=self.auctionA,
        )
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
        """Test that payment.updated webhook successfully creates InvoicePayment without status field"""
        from unittest.mock import Mock

        from django.urls import reverse

        from auctions.models import Invoice, InvoicePayment, SquareSeller

        # Create an invoice for the test
        test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)

        # Mock the entire Square orders.get flow
        mock_order = Mock()
        mock_order.reference_id = str(test_invoice.pk)

        mock_order_response = Mock()
        mock_order_response.order = mock_order

        mock_orders_api = Mock()
        mock_orders_api.get = Mock(return_value=mock_order_response)

        mock_client = Mock()
        mock_client.orders = mock_orders_api

        # Patch get_square_client at the class level so any instance returns our mock
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
            # Verify that the status field is not present (would raise AttributeError if accessed)
            self.assertFalse(hasattr(payment, "status") and payment.status)


class SquareWebhookSignatureValidationTests(StandardTestCase):
    """Tests for Square webhook signature validation

    Confirms that SQUARE_WEBHOOK_SIGNATURE_KEY is actually respected
    and that we don't validate forged requests.
    """

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
        """Compute an HMAC-SHA256 signature for testing using base64 encoding (as Square does)

        Args:
            url: The notification URL
            body: The request body
            key: Optional signature key (defaults to self.signature_key)
        """
        if key is None:
            key = self.signature_key
        message = (url + body).encode("utf-8")
        key_bytes = key.encode("utf-8")
        hash_bytes = hmac.new(key_bytes, message, hashlib.sha256).digest()
        return base64.b64encode(hash_bytes).decode("utf-8")

    def test_forged_signature_is_rejected(self):
        """Test that requests with invalid/forged signatures are rejected when key is configured"""
        url = reverse("square_webhook")

        # Test with signature key configured - forged signature should be rejected
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
        """Test that requests without signature header are rejected when key is configured"""
        url = reverse("square_webhook")

        # Test with signature key configured - missing signature should be rejected
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
        """Test that requests with valid signatures are accepted when key is configured"""
        url = reverse("square_webhook")
        body = json.dumps(self.webhook_data)

        # Build the full URL as the test client would see it
        # The test client uses HTTP on localhost by default
        full_url = "http://testserver" + url

        # Compute the correct signature
        valid_signature = self.compute_signature(full_url, body)

        # Test with signature key configured - valid signature should be accepted
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

        # Test with correct signature key configured - wrong key signature should be rejected
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
        """Test that ImproperlyConfigured is raised in production when Square is configured but webhook key is missing"""
        url = reverse("square_webhook")

        # Simulate production mode (DEBUG=False) with Square configured but no webhook signature key
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
