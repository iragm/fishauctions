"""
Test for Square refund math issue
This test verifies that when a Square refund fails, the partial_refund_percent
is NOT updated, preventing invoice calculation errors.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Invoice, InvoicePayment, Lot, PickupLocation

User = get_user_model()


class SquareRefundMathTest(TestCase):
    """Test cases for Square refund math issues"""

    def setUp(self):
        """Set up test data"""
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)

        # Create users
        self.seller = User.objects.create_user(username="seller", password="test", email="seller@test.com")
        self.buyer = User.objects.create_user(username="buyer", password="test", email="buyer@test.com")
        self.admin = User.objects.create_user(username="admin", password="test", email="admin@test.com")

        # Create auction with 0% club cut for simplicity
        self.auction = Auction.objects.create(
            created_by=self.seller,
            title="Test Auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=0,  # 0% to club, 100% to seller
            lot_entry_fee=0,
            unsold_lot_fee=0,
            tax=0,  # No tax for simplicity
        )

        self.location = PickupLocation.objects.create(name="location", auction=self.auction, pickup_time=theFuture)

        # Create TOS
        self.seller_tos = AuctionTOS.objects.create(user=self.seller, auction=self.auction, pickup_location=self.location)
        self.buyer_tos = AuctionTOS.objects.create(user=self.buyer, auction=self.auction, pickup_location=self.location)

        # Create lot sold for $1
        self.lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            quantity=1,
            winning_price=1,  # $1
            auctiontos_winner=self.buyer_tos,
            active=False,
        )

        # Get invoices
        self.buyer_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.buyer_tos)
        self.seller_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.seller_tos)

        # Create payment
        self.payment = InvoicePayment.objects.create(
            invoice=self.buyer_invoice,
            amount=Decimal("1.00"),
            amount_available_to_refund=Decimal("1.00"),
            currency="USD",
            payment_method="Square",
            external_id="TEST_PAYMENT_123",
        )

    def test_buyer_invoice_before_refund(self):
        """Test buyer invoice calculations before refund"""
        # Buyer bought $1 lot
        self.assertEqual(self.buyer_invoice.total_bought, Decimal("1.00"))
        self.assertEqual(self.buyer_invoice.subtotal, Decimal("-1.00"))
        self.assertEqual(self.buyer_invoice.net, Decimal("-1.00"))
        self.assertEqual(self.buyer_invoice.rounded_net, Decimal("-1.00"))

        # Buyer paid $1
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("1.00"))
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))

    def test_seller_invoice_before_refund(self):
        """Test seller invoice calculations before refund"""
        # Seller sold $1 lot with 0% club cut
        self.assertEqual(self.seller_invoice.total_sold, Decimal("1.00"))
        self.assertEqual(self.seller_invoice.subtotal, Decimal("1.00"))
        self.assertEqual(self.seller_invoice.net, Decimal("1.00"))
        self.assertEqual(self.seller_invoice.rounded_net, Decimal("1.00"))

    def test_square_refund_success_updates_partial_refund_percent(self):
        """Test that successful Square refund updates partial_refund_percent"""
        # Mock square_refund to return success (None)
        with patch.object(self.lot, "square_refund", return_value=None):
            # Issue 50% refund
            self.lot.refund(50, self.admin, "Test refund")

        # Verify partial_refund_percent was updated
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.partial_refund_percent, 50)

        # Verify buyer invoice calculations after partial_refund_percent update
        # (webhook hasn't created negative InvoicePayment yet)
        self.assertEqual(self.buyer_invoice.total_bought, Decimal("0.50"))  # $1 * 50% = $0.50
        self.assertEqual(self.buyer_invoice.net, Decimal("-0.50"))
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("1.00"))  # Webhook hasn't processed yet
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.50"))  # Temporarily positive

        # Simulate webhook creating negative InvoicePayment
        InvoicePayment.objects.create(
            invoice=self.buyer_invoice,
            amount=Decimal("-0.50"),
            currency="USD",
            payment_method="Square Refund",
            external_id="TEST_REFUND_123",
        )

        # Update payment available to refund
        self.payment.amount_available_to_refund = Decimal("0.50")
        self.payment.save()

        # Now verify final state
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("0.50"))  # $1 + (-$0.50)
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))  # Balanced

    def test_square_refund_failure_does_not_update_partial_refund_percent(self):
        """Test that failed Square refund does NOT update partial_refund_percent"""
        # Mock square_refund to return an error
        with patch.object(self.lot, "square_refund", return_value="Square API error"):
            # Attempt 50% refund
            self.lot.refund(50, self.admin, "Test refund")

        # Verify partial_refund_percent was NOT updated
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.partial_refund_percent, 0)  # Should remain 0

        # Verify buyer invoice calculations remain unchanged
        self.assertEqual(self.buyer_invoice.total_bought, Decimal("1.00"))  # Not reduced
        self.assertEqual(self.buyer_invoice.net, Decimal("-1.00"))
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("1.00"))
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))  # Still balanced

    def test_manual_refund_without_square_payment(self):
        """Test manual refund when no Square payment exists (e.g., cash payment)"""
        # Create a lot with no Square payment
        cash_buyer = User.objects.create_user(username="cash_buyer", password="test")
        cash_buyer_tos = AuctionTOS.objects.create(user=cash_buyer, auction=self.auction, pickup_location=self.location)
        cash_lot = Lot.objects.create(
            lot_name="Cash Lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            quantity=1,
            winning_price=1,
            auctiontos_winner=cash_buyer_tos,
            active=False,
        )
        cash_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=cash_buyer_tos)

        # Issue manual refund (no Square payment to refund)
        cash_lot.refund(50, self.admin, "Manual refund")

        # Verify partial_refund_percent was updated (manual refund path)
        cash_lot.refresh_from_db()
        self.assertEqual(cash_lot.partial_refund_percent, 50)

        # Verify invoice calculations
        self.assertEqual(cash_invoice.total_bought, Decimal("0.50"))  # $1 * 50% = $0.50
        self.assertEqual(cash_invoice.net, Decimal("-0.50"))
        self.assertEqual(cash_invoice.total_payments, Decimal("0.00"))  # No payments
        self.assertEqual(cash_invoice.net_after_payments, Decimal("-0.50"))  # User owes club

    def test_issue_scenario_square_refund_and_partial_refund_percent_both_applied(self):
        """
        Test the specific scenario from the issue:
        User buys lot for $1, pays $1 with Square, admin refunds $0.50
        
        This tests the case where partial_refund_percent is set AND Square refund is processed,
        and verifies the invoice math is correct.
        """
        # Initial state
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))

        # Admin issues 50% refund (mock successful Square refund)
        with patch.object(self.lot, "square_refund", return_value=None):
            self.lot.refund(50, self.admin, "Issue refund")

        # Verify partial_refund_percent was set
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.partial_refund_percent, 50)

        # Simulate webhook creating negative InvoicePayment
        InvoicePayment.objects.create(
            invoice=self.buyer_invoice,
            amount=Decimal("-0.50"),
            currency="USD",
            payment_method="Square Refund",
            external_id="TEST_REFUND_456",
        )
        self.payment.amount_available_to_refund = Decimal("0.50")
        self.payment.save()

        # Verify final state
        self.assertEqual(self.buyer_invoice.total_bought, Decimal("0.50"))  # Reduced by partial_refund_percent
        self.assertEqual(self.buyer_invoice.net, Decimal("-0.50"))
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("0.50"))  # $1 + (-$0.50)
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))  # Balanced, not +$0.50

        # The issue described "club owes user $0.50" which would be a positive number
        # This test verifies that's NOT the case - the invoice should be balanced
        self.assertLessEqual(
            self.buyer_invoice.net_after_payments,
            Decimal("0.00"),
            "Invoice should not show club owing user money after correct refund processing",
        )

    def test_issue_scenario_partial_refund_percent_set_without_square_refund(self):
        """
        Test the problematic scenario that causes the issue:
        If partial_refund_percent is set but Square refund fails/doesn't process,
        the invoice would show incorrect balance.
        
        This was the BUG that the fix addresses.
        """
        # Initial state
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.00"))

        # Before the fix: If admin manually set partial_refund_percent=50 without processing Square refund,
        # or if Square refund failed but partial_refund_percent was still updated,
        # the invoice would show club owes user $0.50
        
        # Simulate the old buggy behavior by manually setting partial_refund_percent
        self.lot.partial_refund_percent = 50
        self.lot.save()

        # Without Square refund creating negative InvoicePayment:
        # total_bought is reduced to $0.50, but total_payments is still $1.00
        self.assertEqual(self.buyer_invoice.total_bought, Decimal("0.50"))
        self.assertEqual(self.buyer_invoice.net, Decimal("-0.50"))
        self.assertEqual(self.buyer_invoice.total_payments, Decimal("1.00"))  # No refund processed!
        self.assertEqual(self.buyer_invoice.net_after_payments, Decimal("0.50"))  # BUG: Club owes user!

        # This is the bug that was reported: "Invoice now shows that the club owes the user $0.50"
        self.assertGreater(
            self.buyer_invoice.net_after_payments,
            Decimal("0.00"),
            "This demonstrates the bug: invoice incorrectly shows club owing user money",
        )

        # With the fix: lot.refund() won't update partial_refund_percent if Square refund fails,
        # so this problematic state won't occur unless admin manually edits the database
