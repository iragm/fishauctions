from django.urls import reverse

from auctions import free_text_usage
from auctions.free_text_usage import Row, group_by_term
from auctions.models import Auction, InvoiceAdjustment
from auctions.tests import StandardTestCase


class FreeTextUsageTests(StandardTestCase):
    def test_texts_group_under_the_term_most_auctions_use(self):
        rows = [
            Row("Membership $10", 1),
            Row("membership dues", 2),
            Row("Membership", 2),
            Row("Table fee", 3),
            Row("late fees", 4),
            Row("table fee", 4),
            Row("Late fee", 5),
            Row("donation", 5),
            Row("$5", 6),
        ]
        groups, singles = group_by_term(rows)
        self.assertEqual([(g.term, g.auctions, g.uses) for g in groups], [("fee", 3, 4), ("membership", 2, 3)])
        self.assertEqual(groups[0].phrases, [("table fee", 2), ("late fees", 1), ("late fee", 1)])
        self.assertEqual(sorted(g.term for g in singles), ["", "donation"])

    def test_adjustments_and_fields(self):
        InvoiceAdjustment.objects.create(invoice=self.invoice, adjustment_type="DISCOUNT", amount=4, notes="Test")
        Auction.objects.filter(pk=self.online_auction.pk).update(
            use_custom_checkbox_field=True, custom_checkbox_name="CARES species"
        )
        adjustments = free_text_usage.adjustments()
        (group,) = [g for g in adjustments["groups"] + adjustments["singles"] if g.term == "test"]
        tests = InvoiceAdjustment.objects.filter(notes__iexact="test")
        self.assertEqual(
            (group.uses, group.columns["discounts"]), (tests.count(), tests.filter(adjustment_type="DISCOUNT").count())
        )
        checkbox = next(s for s in free_text_usage.custom_fields() if s["title"] == "Custom checkbox")
        self.assertIn(("cares", 1), checkbox["terms"])

    def test_the_page_is_for_site_admins(self):
        self.client.force_login(self.admin_user)
        self.assertNotEqual(self.client.get(reverse("admin_free_text")).status_code, 200)
        self.admin_user.is_superuser = True
        self.admin_user.save()
        response = self.client.get(reverse("admin_free_text"))
        self.assertContains(response, "Invoice adjustment notes")
        self.assertContains(response, "Custom dropdown")
