"""Invoice models: what an invoice contains, when it is created, and when it notifies."""

from decimal import Decimal

from django.utils import timezone

from auctions.models import (
    AuctionTOS,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
)
from auctions.tests import StandardTestCase


class InvoiceModelTests(StandardTestCase):
    def test_invoices(self):
        assert self.invoice.auction == self.online_auction

        assert self.invoiceB.flat_value_adjustments == 0
        assert self.invoiceB.percent_value_adjustments == 0

        assert self.invoiceB.total_sold == 0
        assert self.invoiceB.total_bought == 30
        assert self.invoiceB.subtotal == -30
        self.assertAlmostEqual(self.invoiceB.tax, Decimal(7.5))
        assert self.invoiceB.net == -37.5
        assert self.invoiceB.rounded_net == -37
        assert self.invoiceB.absolute_amount == 37
        assert self.invoiceB.lots_sold == 0
        assert self.invoiceB.lots_sold_successfully_count == 0
        assert self.invoiceB.unsold_lots == 0
        assert self.invoiceB.lots_bought == 3

        assert self.invoice.total_sold == 6.5
        assert self.invoice.total_bought == 0
        assert self.invoice.subtotal == 6.5
        assert self.invoice.tax == 0
        assert self.invoice.net == 6.5
        assert self.invoice.rounded_net == 7
        assert self.invoice.absolute_amount == 7
        assert self.invoice.lots_sold == 4
        assert self.invoice.lots_sold_successfully_count == 3
        assert self.invoice.unsold_lots == 1
        assert self.invoice.lots_bought == 0
        assert self.invoiceB.location == self.location
        assert self.invoiceB.contact_email == "test@example.com"
        assert self.invoiceB.is_online
        assert self.invoiceB.unsold_lot_warning == ""
        assert str(self.invoice) == f"{self.online_tos.name}'s invoice for {self.online_tos.auction}"

        # adjustments
        self.adjustment_add.amount = 0
        self.adjustment_add.save()
        assert self.invoiceB.net == -27.5
        self.adjustment_discount.amount = 0
        self.adjustment_discount.save()
        assert self.invoiceB.net == -37.5
        self.adjustment_add_percent.amount = 0
        self.adjustment_add_percent.save()
        assert self.invoiceB.net == -34.5
        self.adjustment_discount_percent.amount = 0
        self.adjustment_discount_percent.save()
        assert self.invoiceB.net == -37.5


class InvoiceCreateViewTests(StandardTestCase):
    """Test invoice creation view"""

    def test_invoice_create_success(self):
        """Test creating an invoice for a user without one"""
        # Create a new user without an invoice
        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Ensure no invoice exists
        assert new_tos.invoice is None

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Create invoice
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")

        # Check redirect to invoice page
        assert response.status_code == 302

        # Verify invoice was created
        new_tos = AuctionTOS.objects.get(pk=new_tos.pk)
        assert new_tos.invoice is not None
        assert new_tos.invoice.auctiontos_user == new_tos
        assert new_tos.invoice.auction == self.online_auction

    def test_invoice_create_duplicate_handling(self):
        """Test that creating a second invoice for the same AuctionTOS deduplicates on save: keeps oldest, merges data"""

        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Create first (oldest) invoice with a payment and an adjustment
        first_invoice = Invoice.objects.create(auctiontos_user=new_tos, auction=self.online_auction)
        first_invoice_pk = first_invoice.pk
        InvoicePayment.objects.create(invoice=first_invoice, amount=10, payment_method="Cash")
        InvoiceAdjustment.objects.create(invoice=first_invoice, amount=5, notes="test adj")

        # Create a second invoice (simulates a race-condition duplicate); save() should auto-deduplicate
        Invoice.objects.create(auctiontos_user=new_tos, auction=self.online_auction)

        # Exactly one invoice remains, and it's the oldest
        assert Invoice.objects.filter(auctiontos_user=new_tos).count() == 1
        surviving = Invoice.objects.filter(auctiontos_user=new_tos).first()
        assert surviving.pk == first_invoice_pk

        # The view-based create also redirects to the existing invoice
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")
        assert response.status_code == 302
        assert Invoice.objects.filter(auctiontos_user=new_tos).count() == 1

    def test_invoice_create_non_admin_denied(self):
        """Test that non-admins cannot create invoices"""
        # Create a new user without an invoice
        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Login as non-admin user
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")

        # Try to create invoice
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")

        # Check for permission error (403 or redirect)
        assert response.status_code in [302, 403]

        # Verify no invoice was created
        new_tos = AuctionTOS.objects.get(pk=new_tos.pk)
        assert new_tos.invoice is None


class InvoiceNotificationDueTests(StandardTestCase):
    """Test invoice notification due logic in views"""

    def test_invoice_status_to_ready_sets_notification_due(self):
        """Test that setting invoice to UNPAID (ready) sets notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Ensure invoice starts without notification due
        self.invoice.status = "DRAFT"
        self.invoice.invoice_notification_due = None
        self.invoice.save()

        # Set invoice to ready
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/UNPAID")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was set
        assert self.invoice.status == "UNPAID"
        assert self.invoice.invoice_notification_due is not None
        # Should be set to ~15 seconds in the future
        assert self.invoice.invoice_notification_due > timezone.now()

    def test_invoice_status_to_paid_sets_notification_due(self):
        """Test that setting invoice to PAID sets notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Ensure invoice starts without notification due
        self.invoice.status = "UNPAID"
        self.invoice.invoice_notification_due = None
        self.invoice.save()

        # Set invoice to paid
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was set
        assert self.invoice.status == "PAID"
        assert self.invoice.invoice_notification_due is not None
        # Should be set to ~15 seconds in the future
        assert self.invoice.invoice_notification_due > timezone.now()

    def test_invoice_status_to_open_clears_notification_due(self):
        """Test that setting invoice to DRAFT (open) clears notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Start with invoice that has notification due set
        self.invoice.status = "UNPAID"
        self.invoice.invoice_notification_due = timezone.now()
        self.invoice.save()

        # Set invoice back to draft
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/DRAFT")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was cleared
        assert self.invoice.status == "DRAFT"
        assert self.invoice.invoice_notification_due is None
