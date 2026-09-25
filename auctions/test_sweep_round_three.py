"""Round three of the sweep: writes that clobbered live rows, and who may grant auction admin.

Each of these saved a whole row it had loaded earlier (or on a GET), which put back whatever a bid, a
webhook or another admin had changed in between.
"""

from decimal import Decimal

from django.urls import reverse

from auctions.models import AuctionTOS, Bid, Club, ClubMember, Invoice, Lot
from auctions.tests import StandardTestCase


class InvoiceOpenedTests(StandardTestCase):
    def test_opening_your_invoice_does_not_put_back_a_stale_status(self):
        """The buyer's view marks it opened with one column, so a payment landing meanwhile stays paid."""
        invoice = Invoice.objects.get(pk=self.invoiceB.pk)
        self.client.force_login(self.userB)
        # Simulate the webhook marking it paid after the view loaded its copy: patch save to check fields.
        original = Invoice.save
        seen = {}

        def spy(instance, *args, **kwargs):
            seen.setdefault("update_fields", kwargs.get("update_fields"))
            return original(instance, *args, **kwargs)

        Invoice.save = spy
        try:
            self.client.get(reverse("invoice_by_pk", kwargs={"pk": invoice.pk}))
        finally:
            Invoice.save = original
        self.assertEqual(seen.get("update_fields"), ["opened"])


class MaxBidPeekTests(StandardTestCase):
    def test_looking_at_the_max_bid_writes_one_column(self):
        """A GET during live bidding: a full save of the lot put back a bid's end-time extension."""
        Lot.objects.filter(pk=self.lot.pk).update(winning_price=None, auctiontos_winner=None, active=True)
        self.client.force_login(self.user)
        original = Lot.save
        seen = []

        def spy(instance, *args, **kwargs):
            seen.append(kwargs.get("update_fields"))
            return original(instance, *args, **kwargs)

        Lot.save = spy
        try:
            self.client.get(reverse("auction_show_high_bidder", kwargs={"pk": self.lot.pk}))
        finally:
            Lot.save = original
        self.assertEqual(seen, [["max_bid_revealed_by"]])
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).max_bid_revealed_by, self.user)


class RefundDialogTests(StandardTestCase):
    def test_opening_the_dialog_leaves_an_existing_refund_alone(self):
        Lot.objects.filter(pk=self.lot.pk).update(partial_refund_percent=50)
        self.client.force_login(self.user)
        response = self.client.get(reverse("lot_refund", kwargs={"pk": self.lot.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).partial_refund_percent, 50)


class GrantingAuctionAdminTests(StandardTestCase):
    def test_club_add_edit_people_cannot_make_anyone_auction_admin(self):
        club = Club.objects.create(name="Grant club")
        self.online_auction.club = club
        self.online_auction.manage_users_through_club = "all"
        self.online_auction.save()
        ClubMember.objects.create(club=club, user=self.userB, name="Helper", permission_add_edit=True)
        # A participant with no club record, so the plain participant form is the one that opens.
        tos = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, name="Walk-in", bidder_number="777"
        )
        self.client.force_login(self.userB)
        self.client.post(
            reverse("auctiontosadmin", kwargs={"pk": tos.pk}),
            {
                "bidder_number": tos.bidder_number,
                "pickup_location": self.location.pk,
                "name": "Helper",
                "email": "",
                "phone_number": "",
                "address": "",
                "is_admin": "on",
                "bidding_allowed": "on",
                "selling_allowed": "on",
                "memo": "",
            },
        )
        tos.refresh_from_db()
        self.assertEqual(tos.name, "Helper")  # the edit itself went through
        self.assertFalse(tos.is_admin)


class SellToOnlineHighBidderTests(StandardTestCase):
    def test_a_lot_already_sold_on_the_floor_is_not_resold(self):
        lot = Lot.objects.get(pk=self.lot.pk)
        Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=Decimal(5))
        self.client.force_login(self.user)
        self.client.post(
            reverse("bulk_set_lots_won", kwargs={"slug": self.online_auction.slug}), {"got_it": "on", "query": ""}
        )
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_winner, self.tosB)
        self.assertEqual(lot.winning_price, 10)


class BulkInvoiceStatusTests(StandardTestCase):
    def test_an_invoice_paid_meanwhile_is_not_set_back(self):
        """Mark-ready lists open invoices; one a webhook paid before the loop reached it stays paid."""
        Invoice.objects.filter(pk=self.invoice.pk).update(status="PAID")
        self.client.force_login(self.user)
        self.client.post(reverse("auction_invoices_ready", kwargs={"slug": self.online_auction.slug}), {})
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "PAID")
