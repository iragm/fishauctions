"""Adjustments on the invoice page save one row at a time: a new row, a change, a delete, a blank row,
who may do it, and the invoice page with no formset left on it.
"""

from django.urls import reverse

from auctions.models import Invoice, InvoiceAdjustment
from auctions.tests import StandardTestCase


class InvoiceAdjustmentSaveTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        Invoice.objects.filter(pk=self.invoice.pk).update(status="DRAFT")
        self.url = reverse("invoice_adjustment_save", kwargs={"pk": self.invoice.pk})

    def row(self, **data):
        return {"row": "n1", "adjustment_type": "ADD", "amount": "5", "notes": "club dues", **data}

    def test_new_row_saves_and_returns_pk(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, self.row())
        self.assertEqual(response.status_code, 200)
        adjustment = InvoiceAdjustment.objects.get(invoice=self.invoice)
        self.assertEqual(adjustment.amount, 5)
        self.assertEqual(adjustment.user, self.admin_user)
        self.assertEqual(response["HX-Trigger"], "adjustmentSaved")
        self.assertContains(response, f'id="adj-pk-n1" value="{adjustment.pk}"')

    def test_change_updates_same_row(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, self.row())
        adjustment = InvoiceAdjustment.objects.get(invoice=self.invoice)
        response = self.client.post(
            self.url, self.row(row=f"r{adjustment.pk}", adjustment=str(adjustment.pk), amount="7")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvoiceAdjustment.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(InvoiceAdjustment.objects.get(pk=adjustment.pk).amount, 7)

    def test_delete_removes_row(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, self.row())
        adjustment = InvoiceAdjustment.objects.get(invoice=self.invoice)
        response = self.client.post(
            self.url, {"row": f"r{adjustment.pk}", "adjustment": str(adjustment.pk), "delete": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertFalse(InvoiceAdjustment.objects.filter(pk=adjustment.pk).exists())

    def test_blank_amount_saves_nothing(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, self.row(amount=""))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoice).exists())

    def test_non_admin_forbidden(self):
        self.client.force_login(self.userB)
        response = self.client.post(reverse("invoice_adjustment_save", kwargs={"pk": self.invoiceB.pk}), self.row())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoiceB, amount=5).exists())

    def test_not_draft_refused(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="UNPAID")
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, self.row())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoice).exists())

    def test_total_changes_after_save(self):
        self.client.force_login(self.admin_user)
        # The fixture never totals the invoice, so total it first to have a baseline.
        Invoice.objects.get(pk=self.invoice.pk).recalculate()
        before = Invoice.objects.get(pk=self.invoice.pk).calculated_total
        self.client.post(self.url, self.row(amount="50"))
        after = Invoice.objects.get(pk=self.invoice.pk).calculated_total
        # A charge lowers the net (negative means the buyer owes), so the total moves by exactly the amount.
        self.assertEqual(after, before - 50)


class InvoicePageAdjustmentTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        Invoice.objects.filter(pk=self.invoice.pk).update(status="DRAFT")
        self.url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})

    def test_invoice_page_has_no_formset_post(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "form-TOTAL_FORMS")
        self.assertNotContains(response, 'id="form-container"')
        self.assertContains(response, 'id="adjustment-row-template"')

    def test_status_buttons_render_without_dirty_warning(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        # invoice_buttons.html quotes its id with single quotes.
        self.assertContains(response, f"id='invoice-buttons-{self.invoice.pk}'")
        self.assertNotContains(response, "form_dirty_warning")
