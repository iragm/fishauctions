"""Round 2 review fixes: an invoice adjustment is refused once the invoice is no longer open, under the
invoice's row lock; marking an invoice PAID freezes a total that includes an adjustment saved just before;
the set-winners banner for a lot with only an account winner; and the cadence answer page says when an
answer is refused.
"""

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from auctions import club_emails
from auctions.models import Club, Invoice, InvoiceAdjustment, Lot
from auctions.tests import StandardTestCase


class InvoiceAdjustmentLockTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        Invoice.objects.filter(pk=self.invoice.pk).update(status="DRAFT")
        self.url = reverse("invoice_adjustment_save", kwargs={"pk": self.invoice.pk})
        self.row = {"row": "n1", "adjustment_type": "ADD", "amount": "5", "notes": "club dues"}

    def test_adjustment_refused_once_not_draft_and_checked_under_lock(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="PAID")
        self.client.force_login(self.admin_user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.url, self.row)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoice).exists())
        self.assertTrue(any("FOR UPDATE" in q["sql"] for q in queries.captured_queries))

    def test_delete_refused_once_not_draft(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, self.row)
        adjustment = InvoiceAdjustment.objects.get(invoice=self.invoice)
        Invoice.objects.filter(pk=self.invoice.pk).update(status="UNPAID")
        response = self.client.post(
            self.url, {"row": f"r{adjustment.pk}", "adjustment": str(adjustment.pk), "delete": "1"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(InvoiceAdjustment.objects.filter(pk=adjustment.pk).exists())


class MarkPaidFreezesAdjustedTotalTests(StandardTestCase):
    def test_paid_total_includes_adjustment_saved_just_before(self):
        self.client.force_login(self.admin_user)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.recalculate()
        baseline = Invoice.objects.get(pk=self.invoice.pk).calculated_total
        # Saved directly, as an adjustment that committed a moment before the status click.
        InvoiceAdjustment.objects.create(invoice=self.invoice, adjustment_type="ADD", amount=5, user=self.admin_user)
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")
        self.assertEqual(response.status_code, 200)
        paid = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(paid.status, "PAID")
        self.assertIsNotNone(paid.date_paid)
        # An ADD adjustment lowers the net by its amount, so the frozen total must include it.
        self.assertEqual(paid.calculated_total, baseline - 5)


class SoldBannerAccountWinnerTests(StandardTestCase):
    def test_lot_with_only_an_account_winner_gets_the_banner(self):
        lot = self.lot
        Lot.objects.filter(pk=lot.pk).update(auctiontos_winner=None, winner=self.user, winning_price=10)
        self.client.force_login(self.admin_user)
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(
            url,
            {"lot": lot.lot_number_int, "price": "12", "winner": self.tosC.bidder_number, "action": "save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("already sold", response.json()["success_message"])


class CadenceAnswerRefusedTests(StandardTestCase):
    def test_refused_answer_says_so(self):
        club = Club.objects.create(name="Refusal Club")
        outsider = User.objects.create_user("r2_outsider", "r2_outsider@example.com", "pw")
        token = club_emails.answer_token(club, outsider)
        url = reverse("club_cadence_answer", kwargs={"slug": club.slug, "token": token})
        response = self.client.post(url, {"answer": "monthly"})
        self.assertContains(response, "Only this club&#x27;s staff can answer.")
        club.refresh_from_db()
        self.assertEqual(club.auction_cadence, "")
