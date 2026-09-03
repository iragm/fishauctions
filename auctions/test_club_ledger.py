"""The club ledger on a cash basis: what a paid invoice freezes, and how dues reverse."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Club,
    ClubMember,
    ClubMoney,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    PickupLocation,
)
from auctions.tests import StandardTestCase


class ClubMoneyLedgerCashBasisTests(TestCase):
    """The cash-basis club ledger booked from invoices (Invoice.sync_club_money).

    A PAID invoice books one entry per component (buyer payment, seller payout, tax,
    membership, adjustment, first-bid payout, rounding); they sum to the cash that moves
    and the club's auction commission is sales minus payouts. Booking is reversible.
    """

    def setUp(self):
        self.creator = User.objects.create_user("ledger_creator", "lc@example.com", "pw")
        self.club = Club.objects.create(
            name="Ledger Cash Club", enable_membership=True, membership_annual_fee=Decimal("25.00")
        )
        self._n = 0

    def _auction(self, tax=0, rounding=False, club_pct=20):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Ledger Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=club_pct,
            tax=tax,
            lot_entry_fee=0,
            unsold_lot_fee=0,
            invoice_rounding=rounding,
        )
        PickupLocation.objects.create(name="Ledger Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _sold_lot(self, auction, seller, buyer, price):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            quantity=1,
        )

    def _paid_invoice(self, tos):
        invoice = Invoice.objects.get_or_create(auctiontos_user=tos)[0]
        invoice.status = "PAID"
        invoice.save()
        return invoice

    def _by_category(self, invoice):
        result = {}
        for entry in ClubMoney.objects.filter(invoice=invoice):
            result[entry.category] = result.get(entry.category, Decimal("0.00")) + entry.amount
        return result

    def _ledger_total(self, **filters):
        return sum((entry.amount for entry in ClubMoney.objects.filter(**filters)), Decimal("0.00"))

    def test_sale_and_payout_are_separate_and_imply_commission(self):
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        seller_invoice = self._paid_invoice(seller)
        buyer_invoice = self._paid_invoice(buyer)

        self.assertEqual(self._by_category(seller_invoice)[ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT], Decimal("-80.00"))
        self.assertEqual(self._by_category(buyer_invoice)[ClubMoney.CATEGORY_AUCTION_SALE], Decimal("100.00"))
        # Commission (club cut) = sales - payouts = 100 - 80 = 20, which is the club's balance.
        self.assertEqual(self._ledger_total(club=self.club), Decimal("20.00"))

    def test_tax_is_broken_out(self):
        auction = self._auction(tax=10, club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = self._paid_invoice(buyer)
        entries = self._by_category(buyer_invoice)
        self.assertEqual(entries[ClubMoney.CATEGORY_AUCTION_SALE], Decimal("100.00"))
        self.assertEqual(entries[ClubMoney.CATEGORY_TAX], Decimal("10.00"))

    def test_adjustment_is_its_own_category(self):
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        InvoiceAdjustment.objects.create(invoice=buyer_invoice, adjustment_type="ADD", amount=5, notes="late fee")
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._by_category(buyer_invoice)[ClubMoney.CATEGORY_INVOICE_ADJUSTMENT], Decimal("5.00"))

    def test_membership_is_its_own_category(self):
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        buyer_invoice.renewal_needed = True
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._by_category(buyer_invoice)[ClubMoney.CATEGORY_MEMBERSHIP], Decimal("25.00"))

    def test_rounding_is_broken_out(self):
        # club_pct=15 on a $10 lot -> seller cut 8.50; rounding (in the seller's favor) pays 9.
        auction = self._auction(club_pct=15, rounding=True)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 10)
        seller_invoice = self._paid_invoice(seller)
        entries = self._by_category(seller_invoice)
        self.assertEqual(entries[ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT], Decimal("-8.50"))
        self.assertEqual(entries[ClubMoney.CATEGORY_ROUNDING], Decimal("-0.50"))
        # The entries still sum to the cash that actually changed hands (a whole 9 dollars out).
        self.assertEqual(self._ledger_total(invoice=seller_invoice), Decimal("-9.00"))

    def test_entries_sum_to_rounded_invoice_total(self):
        auction = self._auction(tax=10, club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = self._paid_invoice(buyer)
        buyer_invoice.refresh_from_db()
        self.assertEqual(self._ledger_total(invoice=buyer_invoice), -Decimal(buyer_invoice.rounded_net))

    def test_unpaid_to_paid_changes_balance_by_invoice_total(self):
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        before = self._ledger_total(invoice=buyer_invoice)
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        after = self._ledger_total(invoice=buyer_invoice)
        self.assertEqual(before, Decimal("0.00"))
        self.assertEqual(after, Decimal("100.00"))  # the buyer paid the club 100

    def test_paid_unpaid_paid_is_neutral(self):
        auction = self._auction(tax=10, club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = self._paid_invoice(buyer)
        first = self._ledger_total(invoice=buyer_invoice)
        buyer_invoice.status = "UNPAID"
        buyer_invoice.save()
        self.assertEqual(self._ledger_total(invoice=buyer_invoice), Decimal("0.00"))
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._ledger_total(invoice=buyer_invoice), first)

    def test_report_commission_is_sales_minus_payouts(self):
        from auctions.views import ClubTreasurerReportView

        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        self._paid_invoice(seller)
        self._paid_invoice(buyer)
        view = ClubTreasurerReportView()
        view.club = self.club
        entries = ClubMoney.objects.filter(club=self.club)
        summary = view._report_summary(entries, datetime.date(2026, 3, 1), datetime.date(2026, 3, 31))
        self.assertEqual(summary["auction_sales"], Decimal("100.00"))
        self.assertEqual(summary["seller_payouts"], Decimal("80.00"))
        self.assertEqual(summary["auction_commission"], Decimal("20.00"))

    def test_legacy_category_rows_are_not_perpetuated(self):
        # A database carried over from the old ledger may hold rows in retired categories.
        # Reconciling an invoice must not write new rows in those dead categories.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        ClubMoney.objects.create(
            club=self.club,
            invoice=buyer_invoice,
            date=datetime.date(2026, 3, 15),
            amount=Decimal("20.00"),
            category="auction_profit",  # retired category
        )
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        # The seeded legacy row is left as-is (count stays 1) and current categories are booked.
        self.assertEqual(ClubMoney.objects.filter(invoice=buyer_invoice, category="auction_profit").count(), 1)
        self.assertTrue(
            ClubMoney.objects.filter(invoice=buyer_invoice, category=ClubMoney.CATEGORY_AUCTION_SALE).exists()
        )

    # --- Cash-basis dating: entries date to when the cash moved, not auction.date_start ---
    #
    # Every auction built by self._auction() starts on 2026-03-15. An online invoice can be
    # paid weeks later, and the ledger must book that revenue to the settlement date so the
    # treasurer's date-range reports attribute it to the right period.
    AUCTION_START = datetime.date(2026, 3, 15)
    PAID_ON = datetime.datetime(2026, 4, 20, 10, 0, tzinfo=datetime.timezone.utc)

    def _record_payment(self, invoice, when, amount="100.00"):
        """Attach a recorded payment dated ``when`` (bypassing createdon's auto_now_add)."""
        payment = InvoicePayment.objects.create(invoice=invoice, amount=Decimal(amount))
        InvoicePayment.objects.filter(pk=payment.pk).update(createdon=when)
        return payment

    def _entry_dates(self, invoice):
        return {entry.date for entry in ClubMoney.objects.filter(invoice=invoice)}

    def test_ledger_dates_to_payment_date_not_auction_start(self):
        # An invoice paid weeks after the auction opened books to the payment date.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        self._record_payment(buyer_invoice, self.PAID_ON)
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertTrue(ClubMoney.objects.filter(invoice=buyer_invoice).exists())
        self.assertEqual(self._entry_dates(buyer_invoice), {self.PAID_ON.date()})
        self.assertNotIn(self.AUCTION_START, self._entry_dates(buyer_invoice))

    def test_ledger_dates_to_date_paid_when_no_recorded_payment(self):
        # Cash paid at the door has no InvoicePayment; the stamped date_paid drives the date.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        buyer_invoice.date_paid = self.PAID_ON
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._entry_dates(buyer_invoice), {self.PAID_ON.date()})

    def test_date_paid_is_stamped_on_paid_transition(self):
        # Marking an invoice PAID stamps date_paid, and the ledger books to that date.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = self._paid_invoice(buyer)
        buyer_invoice.refresh_from_db()
        self.assertIsNotNone(buyer_invoice.date_paid)
        self.assertEqual(self._entry_dates(buyer_invoice), {timezone.localdate(buyer_invoice.date_paid)})

    def test_resync_does_not_shift_entry_dates(self):
        # A re-sync appends its deltas under the ORIGINAL booking date rather than moving them to
        # a new period. Settled invoices are frozen against plain re-saves (see Invoice.save), so
        # the legitimate way to re-book is the admin un-pay -> edit -> re-pay correction cycle;
        # that cycle must still respect the original settlement date.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        self._record_payment(buyer_invoice, self.PAID_ON)
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._entry_dates(buyer_invoice), {self.PAID_ON.date()})
        # Un-pay, add an adjustment, record a brand-new (later) payment, then re-pay. The reversal,
        # the re-booking, and the new adjustment delta must all land on the original settlement
        # date -- never on the later payment's date.
        buyer_invoice.status = "UNPAID"
        buyer_invoice.save()
        InvoiceAdjustment.objects.create(invoice=buyer_invoice, adjustment_type="ADD", amount=5, notes="late fee")
        self._record_payment(buyer_invoice, datetime.datetime(2026, 6, 1, 9, 0, tzinfo=datetime.timezone.utc), "5.00")
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(self._by_category(buyer_invoice)[ClubMoney.CATEGORY_INVOICE_ADJUSTMENT], Decimal("5.00"))
        self.assertEqual(self._entry_dates(buyer_invoice), {self.PAID_ON.date()})

    def test_paid_unpaid_paid_keeps_stable_date(self):
        # Toggling PAID -> UNPAID -> PAID never overwrites date_paid and books every reversal
        # and re-booking to the one stable date, so the entries stay in a single period.
        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        self._record_payment(buyer_invoice, self.PAID_ON)
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        original_date_paid = Invoice.objects.get(pk=buyer_invoice.pk).date_paid
        buyer_invoice.status = "UNPAID"
        buyer_invoice.save()
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        self.assertEqual(Invoice.objects.get(pk=buyer_invoice.pk).date_paid, original_date_paid)
        self.assertEqual(self._entry_dates(buyer_invoice), {self.PAID_ON.date()})
        self.assertEqual(self._ledger_total(invoice=buyer_invoice), Decimal("100.00"))

    def test_treasurer_report_attributes_revenue_to_payment_period(self):
        # The revenue lands in the month the invoice was paid (April), not the month the
        # auction opened (March).
        from auctions.views import ClubTreasurerReportView

        auction = self._auction(club_pct=20)
        seller, buyer = self._tos(auction), self._tos(auction)
        self._sold_lot(auction, seller, buyer, 100)
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        self._record_payment(buyer_invoice, self.PAID_ON)
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        view = ClubTreasurerReportView()
        view.club = self.club
        march = view._report_summary(
            view._filtered_entries(datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)),
            datetime.date(2026, 3, 1),
            datetime.date(2026, 3, 31),
        )
        april = view._report_summary(
            view._filtered_entries(datetime.date(2026, 4, 1), datetime.date(2026, 4, 30)),
            datetime.date(2026, 4, 1),
            datetime.date(2026, 4, 30),
        )
        self.assertEqual(march["auction_sales"], Decimal("0.00"))
        self.assertEqual(april["auction_sales"], Decimal("100.00"))


class PaidInvoiceFreezeTests(StandardTestCase):
    """Item 9: once an invoice is PAID it is settled and frozen.

    Viewing it must not recalculate its total, and a plain re-save must not re-sync the
    ClubMoney ledger from current auction/club settings -- otherwise a later change to
    membership_annual_fee or the auction's tax rate would silently rewrite booked history the
    next time a settled invoice is merely touched. Only a status transition books (mark paid) or
    reverses (un-pay) the ledger; un-paying is the correction escape hatch that thaws the invoice.
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Freeze Club", enable_membership=True, membership_annual_fee=Decimal("25.00")
        )
        self.online_auction.club = self.club
        self.online_auction.tax = 25
        self.online_auction.save(update_fields=["club", "tax"])
        ClubMoney.objects.all().delete()

    def _ledger_by_category(self, invoice):
        result = {}
        for entry in ClubMoney.objects.filter(invoice=invoice):
            result[entry.category] = result.get(entry.category, Decimal("0.00")) + entry.amount
        return result

    def _ledger_rows(self, invoice):
        """A stable snapshot of the booked rows -- byte-for-byte equal iff nothing re-booked."""
        return sorted(ClubMoney.objects.filter(invoice=invoice).values_list("pk", "amount", "date", "category"))

    def _ledger_total(self, invoice):
        return sum((entry.amount for entry in ClubMoney.objects.filter(invoice=invoice)), Decimal("0.00"))

    def _view_invoice(self, invoice):
        # A real admin GET of the invoice page -- this is the exact path (InvoiceView.get ->
        # recalculate) that used to silently rewrite a settled invoice's total on every view.
        self.client.login(username=self.admin_user.username, password="testpassword")
        return self.client.get(reverse("invoice_by_pk", kwargs={"pk": invoice.pk}))

    def test_marking_paid_books_correct_ledger(self):
        # tosB bought three $10 lots (sale 30) in a 25%-tax auction and renews (dues 25).
        self.invoiceB.renewal_needed = True
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        by_cat = self._ledger_by_category(self.invoiceB)
        self.assertEqual(by_cat[ClubMoney.CATEGORY_AUCTION_SALE], Decimal("30.00"))
        self.assertEqual(by_cat[ClubMoney.CATEGORY_TAX], Decimal("7.50"))
        self.assertEqual(by_cat[ClubMoney.CATEGORY_MEMBERSHIP], Decimal("25.00"))
        self.invoiceB.refresh_from_db()
        # The settled total is snapshotted at the transition, and the booked entries reconcile
        # to it (buyer entries are the cash into the club, i.e. -rounded_net).
        self.assertEqual(self.invoiceB.calculated_total, self.invoiceB.rounded_net)
        self.assertEqual(self._ledger_total(self.invoiceB), -Decimal(self.invoiceB.rounded_net))

    def test_paid_invoice_frozen_against_fee_and_tax_changes(self):
        self.invoiceB.renewal_needed = True
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        self.invoiceB.refresh_from_db()
        frozen_total = self.invoiceB.calculated_total
        frozen_ledger = self._ledger_rows(self.invoiceB)
        self.assertIsNotNone(frozen_total)
        self.assertEqual(self._ledger_by_category(self.invoiceB)[ClubMoney.CATEGORY_MEMBERSHIP], Decimal("25.00"))

        # The club triples its dues and the auction drops its tax -- the classic "rewrite settled
        # history" triggers. None of this may touch the already-settled invoice.
        self.club.membership_annual_fee = Decimal("75.00")
        self.club.save(update_fields=["membership_annual_fee"])
        self.online_auction.tax = 0
        self.online_auction.save(update_fields=["tax"])

        # Viewing the invoice (InvoiceView.get -> recalculate) must not rewrite the total...
        self.assertEqual(self._view_invoice(self.invoiceB).status_code, 200)
        # ...and neither may a plain re-save nor a direct recalculate() re-sync the ledger.
        self.invoiceB.save()
        self.invoiceB.recalculate()

        self.invoiceB.refresh_from_db()
        self.assertEqual(self.invoiceB.calculated_total, frozen_total)
        self.assertEqual(self._ledger_rows(self.invoiceB), frozen_ledger)
        # The membership dues stay at the settled 25, not the new 75.
        self.assertEqual(self._ledger_by_category(self.invoiceB)[ClubMoney.CATEGORY_MEMBERSHIP], Decimal("25.00"))

    def test_unpay_reverses_ledger_and_thaws_recalculation(self):
        self.invoiceB.renewal_needed = True
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        self.invoiceB.refresh_from_db()
        paid_total = self.invoiceB.calculated_total
        self.assertNotEqual(self._ledger_total(self.invoiceB), Decimal("0.00"))

        # Un-pay: the ledger reverses to zero (existing behavior) and the invoice thaws.
        self.invoiceB.status = "UNPAID"
        self.invoiceB.save()
        self.assertEqual(self._ledger_total(self.invoiceB), Decimal("0.00"))

        # Now a settings change DOES flow through, because the invoice is no longer settled.
        self.online_auction.tax = 0
        self.online_auction.save(update_fields=["tax"])
        # Reload the invoice so it sees the auction's new tax (as a fresh request would), rather
        # than the auction object cached on this in-memory instance from the earlier saves.
        invoice = Invoice.objects.get(pk=self.invoiceB.pk)
        invoice.recalculate()
        invoice.refresh_from_db()
        self.assertNotEqual(invoice.calculated_total, paid_total)
        # With tax removed the buyer owes less, so the (negative) total moved toward zero.
        self.assertGreater(invoice.calculated_total, paid_total)

    def test_refund_on_paid_invoice_keeps_totals_and_ledger_frozen(self):
        # Record the buyer's payment, then settle the invoice.
        payment = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            external_id="PAY-FREEZE-1",
            amount=Decimal("62.50"),
            amount_available_to_refund=Decimal("62.50"),
            currency="USD",
            payment_method="PayPal",
        )
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        self.invoiceB.refresh_from_db()
        frozen_total = self.invoiceB.calculated_total
        frozen_ledger = self._ledger_rows(self.invoiceB)

        # A refund arrives: the webhook records a negative InvoicePayment, decrements the
        # refundable balance, then calls invoice.recalculate() (see handle_refund).
        InvoicePayment.objects.create(
            invoice=self.invoiceB,
            external_id="REFUND-FREEZE-1",
            amount=Decimal("-20.00"),
            currency="USD",
            payment_method="PayPal Refund",
        )
        payment.amount_available_to_refund -= Decimal("20.00")
        payment.save()
        self.invoiceB.recalculate()

        # The refund is recorded (payment row + reduced refundable balance) but the settled
        # line-item total and the booked ledger are untouched -- exactly the freeze.
        payment.refresh_from_db()
        self.assertEqual(payment.amount_available_to_refund, Decimal("42.50"))
        self.assertTrue(InvoicePayment.objects.filter(external_id="REFUND-FREEZE-1", amount=Decimal("-20.00")).exists())
        self.invoiceB.refresh_from_db()
        self.assertEqual(self.invoiceB.calculated_total, frozen_total)
        self.assertEqual(self._ledger_rows(self.invoiceB), frozen_ledger)


class InvoiceDedupeLedgerTests(StandardTestCase):
    """Item 10: Invoice.save() dedupes duplicate invoices for one AuctionTOS, keeping the oldest
    and deleting the rest. ClubMoney.invoice is SET_NULL, so a deleted duplicate that carried
    booked ledger rows used to orphan them (invoice=NULL), break ledger<->invoice traceability,
    and (for a PAID duplicate) leave the club double-booked.

    The dedupe now re-homes a duplicate's ledger rows onto the canonical invoice and reverses the
    duplicate's own contribution, so nothing is orphaned, nothing is double-booked, and a settled
    (PAID) canonical stays frozen (Item 9) because it is never re-derived from current settings.
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(
            name="Dedupe Club", enable_membership=True, membership_annual_fee=Decimal("25.00")
        )
        self.online_auction.club = self.club
        self.online_auction.save(update_fields=["club"])
        ClubMoney.objects.all().delete()

    # --- helpers -----------------------------------------------------------------------------

    def _by_category(self, invoice):
        result = {}
        for entry in ClubMoney.objects.filter(invoice=invoice):
            result[entry.category] = result.get(entry.category, Decimal("0.00")) + entry.amount
        return result

    def _ledger_rows(self, invoice):
        """Byte-for-byte snapshot of an invoice's booked rows -- equal iff nothing re-booked them."""
        return sorted(ClubMoney.objects.filter(invoice=invoice).values_list("pk", "amount", "date", "category"))

    def _ledger_total(self, **filters):
        return sum((entry.amount for entry in ClubMoney.objects.filter(**filters)), Decimal("0.00"))

    def _paid_canonical(self):
        """Mark self.invoiceB PAID so it books its own ledger, and return it as the canonical."""
        self.invoiceB.status = "PAID"
        self.invoiceB.save()
        self.invoiceB.refresh_from_db()
        return self.invoiceB

    def _make_duplicate(self, canonical, paid=True):
        """Create a second invoice for the canonical's AuctionTOS that coexists with it.

        bulk_create bypasses Invoice.save(), so the duplicate is not immediately deduped and can
        carry booked ledger rows -- the state production reaches via races or backdated imports.
        The duplicate is dated after the canonical so the canonical stays the oldest.
        """
        tos = canonical.auctiontos_user
        Invoice.objects.bulk_create(
            [Invoice(auctiontos_user=tos, auction=canonical.auction, status="PAID" if paid else "DRAFT")]
        )
        dup = Invoice.objects.filter(auctiontos_user=tos).exclude(pk=canonical.pk).get()
        Invoice.objects.filter(pk=dup.pk).update(date=canonical.date + datetime.timedelta(days=1))
        dup.refresh_from_db()
        if paid:
            # Book the duplicate's ledger exactly as marking it PAID would have.
            dup.sync_club_money()
        return dup

    def _assert_no_orphans(self):
        self.assertFalse(
            ClubMoney.objects.filter(invoice__isnull=True).exists(),
            "dedupe left ClubMoney rows orphaned with invoice=NULL",
        )

    # --- tests -------------------------------------------------------------------------------

    def test_dedupe_unpaid_duplicate_leaves_canonical_untouched(self):
        canonical = self._paid_canonical()
        frozen_ledger = self._ledger_rows(canonical)
        frozen_total = canonical.calculated_total
        self.assertTrue(frozen_ledger)

        self._make_duplicate(canonical, paid=False)
        # A plain re-save of the PAID canonical runs the dedupe (Path 2) without a status change.
        canonical.save()

        self.assertEqual(Invoice.objects.filter(auctiontos_user=self.tosB).count(), 1)
        self._assert_no_orphans()
        canonical.refresh_from_db()
        self.assertEqual(canonical.calculated_total, frozen_total)
        self.assertEqual(self._ledger_rows(canonical), frozen_ledger)

    def test_dedupe_paid_duplicate_path2_no_double_booking(self):
        # Path 2: the canonical (oldest) is the one being saved; newer PAID duplicate is absorbed.
        canonical = self._paid_canonical()
        canonical_total = self._ledger_total(invoice=canonical)
        canonical_by_cat = self._by_category(canonical)

        dup = self._make_duplicate(canonical, paid=True)
        dup_total = self._ledger_total(invoice=dup)
        self.assertNotEqual(dup_total, Decimal("0.00"))  # the duplicate really carries booked rows
        # Bug precondition: the club is double-booked while both invoices exist.
        self.assertEqual(self._ledger_total(club=self.club), canonical_total + dup_total)

        canonical.save()  # triggers dedupe Path 2

        self.assertEqual(Invoice.objects.filter(auctiontos_user=self.tosB).count(), 1)
        self._assert_no_orphans()
        # The double-booking is gone: the ledger reflects only the canonical's own booking.
        self.assertEqual(self._ledger_total(club=self.club), canonical_total)
        self.assertEqual(self._by_category(canonical), canonical_by_cat)

    def test_dedupe_paid_duplicate_path1_no_double_booking(self):
        # Path 1: the newer duplicate is the one being saved and merges itself into the canonical.
        canonical = self._paid_canonical()
        canonical_total = self._ledger_total(invoice=canonical)
        canonical_by_cat = self._by_category(canonical)

        dup = self._make_duplicate(canonical, paid=True)
        self.assertNotEqual(self._ledger_total(invoice=dup), Decimal("0.00"))

        dup.save()  # dup is newer than canonical -> dedupe Path 1

        self.assertEqual(Invoice.objects.filter(auctiontos_user=self.tosB).count(), 1)
        self._assert_no_orphans()
        self.assertEqual(self._ledger_total(club=self.club), canonical_total)
        self.assertEqual(self._by_category(canonical), canonical_by_cat)

    def test_dedupe_paid_duplicate_keeps_canonical_frozen(self):
        # Item 9 guard: dedupe of a duplicate must not re-derive the settled canonical from
        # current settings -- its snapshotted total and its own booked rows stay put.
        canonical = self._paid_canonical()
        frozen_total = canonical.calculated_total
        frozen_ledger = set(self._ledger_rows(canonical))
        canonical_by_cat = self._by_category(canonical)

        # The classic "rewrite settled history" triggers fire between payment and dedupe.
        self.club.membership_annual_fee = Decimal("75.00")
        self.club.save(update_fields=["membership_annual_fee"])
        self.online_auction.tax = 0
        self.online_auction.save(update_fields=["tax"])

        self._make_duplicate(canonical, paid=True)
        canonical.save()  # dedupe Path 2

        self._assert_no_orphans()
        canonical.refresh_from_db()
        self.assertEqual(canonical.calculated_total, frozen_total)
        # The canonical's own rows are still present, byte-for-byte, and its net per category is
        # unchanged -- the tax drop and dues hike never touched the settled ledger.
        self.assertTrue(frozen_ledger.issubset(set(self._ledger_rows(canonical))))
        self.assertEqual(self._by_category(canonical), canonical_by_cat)

    def test_unpay_after_dedupe_reverses_ledger_cleanly(self):
        canonical = self._paid_canonical()
        self._make_duplicate(canonical, paid=True)
        canonical.save()  # dedupe Path 2 -> ledger reflects only the canonical's booking
        self.assertNotEqual(self._ledger_total(invoice=canonical), Decimal("0.00"))

        canonical.refresh_from_db()
        canonical.status = "UNPAID"
        canonical.save()

        self._assert_no_orphans()
        self.assertEqual(self._ledger_total(invoice=canonical), Decimal("0.00"))
        self.assertEqual(self._ledger_total(club=self.club), Decimal("0.00"))


class ClubMembershipDuesReversalTests(TestCase):
    """Item 11: un-paying a club-only (no-auction) membership/dues invoice must reverse the dues
    entry it booked into the club ledger (ClubMoney).

    Club-only membership invoices have no auction, so they don't share the auction-invoice ledger
    computation; their single membership-dues entry is booked and reversed by
    Invoice.sync_club_money on the PAID/un-pay status transition. The ledger stays append-only:
    un-paying appends a negated reversal row (the original is never deleted), repeated saves in the
    un-paid state don't stack reversals, and re-paying books a fresh entry so the net stays correct.
    Previously the entry was booked directly by _process_invoice_membership_renewal and un-paying
    never reversed it, so the ledger permanently overstated dues income.
    """

    def setUp(self):
        self.member_user = User.objects.create_user("dues_member", "dues_member@example.com", "pw")
        self.club = Club.objects.create(
            name="Dues Reversal Club",
            enable_membership=True,
            membership_system="rolling",
            membership_annual_fee=Decimal("40.00"),
        )
        self.member = ClubMember.objects.create(
            club=self.club, user=self.member_user, name="Dues Member", email="dues_member@example.com"
        )

    def _invoice(self, status="UNPAID"):
        return Invoice.objects.create(
            club=self.club,
            club_member=self.member,
            buyer=self.member_user,
            status=status,
            renewal_needed=True,
        )

    def _membership_rows(self, invoice):
        return ClubMoney.objects.filter(invoice=invoice, category=ClubMoney.CATEGORY_MEMBERSHIP)

    def _ledger_total(self, invoice):
        return sum((row.amount for row in ClubMoney.objects.filter(invoice=invoice)), Decimal("0.00"))

    def _row_count(self, invoice):
        return ClubMoney.objects.filter(invoice=invoice).count()

    def test_pay_books_single_dues_entry(self):
        invoice = self._invoice()
        invoice.status = "PAID"
        invoice.save()
        rows = self._membership_rows(invoice)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().amount, Decimal("40.00"))
        self.assertEqual(self._ledger_total(invoice), Decimal("40.00"))

    def test_unpay_appends_reversal_and_nets_to_zero(self):
        invoice = self._invoice()
        invoice.status = "PAID"
        invoice.save()
        self.assertEqual(self._ledger_total(invoice), Decimal("40.00"))
        booked_pks = set(self._membership_rows(invoice).values_list("pk", flat=True))

        invoice.status = "UNPAID"
        invoice.save()

        # Reversal appended -> net zero, and the original booked row is still there (append-only).
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))
        self.assertEqual(self._membership_rows(invoice).count(), 2)
        self.assertTrue(booked_pks.issubset(set(self._membership_rows(invoice).values_list("pk", flat=True))))

    def test_repeated_unpaid_saves_do_not_stack_reversals(self):
        invoice = self._invoice()
        invoice.status = "PAID"
        invoice.save()
        invoice.status = "UNPAID"
        invoice.save()
        rows_after_first_unpay = self._row_count(invoice)
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))

        # Saving again while un-paid (a DRAFT hop and even a direct re-sync) must not append more
        # reversals -- the ledger is already reconciled to zero for this invoice.
        invoice.save()
        invoice.status = "DRAFT"
        invoice.save()
        invoice.sync_club_money()
        self.assertEqual(self._row_count(invoice), rows_after_first_unpay)
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))

    def test_repay_books_fresh_entry_with_correct_net(self):
        invoice = self._invoice()
        invoice.status = "PAID"
        invoice.save()
        invoice.status = "UNPAID"
        invoice.save()
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))

        invoice.status = "PAID"
        invoice.save()

        # A fresh dues entry is appended (three rows: book, reverse, re-book) and the net is one fee.
        self.assertEqual(self._membership_rows(invoice).count(), 3)
        self.assertEqual(self._ledger_total(invoice), Decimal("40.00"))

    def test_ledger_rows_are_never_deleted_across_toggles(self):
        invoice = self._invoice()
        counts = []
        for status in ("PAID", "UNPAID", "PAID", "DRAFT", "PAID"):
            invoice.status = status
            invoice.save()
            counts.append(self._row_count(invoice))
        # Every transition only ever appends, so the row count is monotonically non-decreasing.
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[0], 1)  # the first PAID booked exactly one row
        # The final state is PAID, so the net is exactly one membership fee.
        self.assertEqual(self._ledger_total(invoice), Decimal("40.00"))

    def test_non_renewal_club_invoice_books_nothing(self):
        # A club-only invoice that isn't renewing dues (renewal_needed False) moves no cash, so
        # marking it PAID must not book a membership entry.
        invoice = Invoice.objects.create(
            club=self.club, club_member=self.member, buyer=self.member_user, status="UNPAID", renewal_needed=False
        )
        invoice.status = "PAID"
        invoice.save()
        self.assertEqual(self._membership_rows(invoice).count(), 0)
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))

    def test_full_admin_endpoint_pay_then_unpay(self):
        # End-to-end through the admin pay-invoice endpoint: the dues entry is booked exactly once
        # (by sync_club_money, no longer by the renewal helper) and un-paying reverses it to zero.
        User.objects.create_superuser("dues_admin", "dues_admin@example.com", "pw")
        invoice = self._invoice()
        client = Client()
        client.login(username="dues_admin", password="pw")

        self.assertEqual(client.post(f"/api/payinvoice/{invoice.pk}/PAID").status_code, 200)
        self.assertEqual(self._membership_rows(invoice).count(), 1)
        self.assertEqual(self._ledger_total(invoice), Decimal("40.00"))

        self.assertEqual(client.post(f"/api/payinvoice/{invoice.pk}/UNPAID").status_code, 200)
        self.assertEqual(self._ledger_total(invoice), Decimal("0.00"))
        # Append-only: the reversal is a new row, the original booking is retained.
        self.assertEqual(self._membership_rows(invoice).count(), 2)


class MakeClubAdminAssignsAuctionsTests(TestCase):
    """The superuser "make {creator} admin of {club}" button assigns the creator's clubless
    auctions to their club and books the club ledger for them, but never reassigns auctions
    that already belong to a club."""

    def setUp(self):
        self.creator = User.objects.create_superuser("mca_creator", "mca@example.com", "pw")
        self.club = Club.objects.create(name="MCA Club")
        self.creator.userdata.club = self.club
        self.creator.userdata.save()
        self.other_club = Club.objects.create(name="MCA Other Club")

    def _auction(self, club=None, title="MCA Auction"):
        auction = Auction.objects.create(
            created_by=self.creator,
            title=title,
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=club,
            winning_bid_percent_to_club=20,
            lot_entry_fee=0,
            unsold_lot_fee=0,
        )
        PickupLocation.objects.create(name="MCA Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _make_club_admin(self, auction):
        client = Client()
        client.force_login(self.creator)
        return client.get(reverse("auction_main", kwargs={"slug": auction.slug}) + "?make_club_admin=true")

    def test_assigns_clubless_auction_and_books_ledger(self):
        auction = self._auction(club=None)
        pickup = PickupLocation.objects.filter(auction=auction).first()
        seller = AuctionTOS.objects.create(name="Seller", auction=auction, pickup_location=pickup)
        buyer = AuctionTOS.objects.create(name="Buyer", auction=auction, pickup_location=pickup)
        Lot.objects.create(
            lot_name="L",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(100),
            active=False,
            quantity=1,
        )
        buyer_invoice = Invoice.objects.get_or_create(auctiontos_user=buyer)[0]
        buyer_invoice.status = "PAID"
        buyer_invoice.save()
        # No club yet, so nothing is in the ledger.
        self.assertFalse(ClubMoney.objects.filter(invoice=buyer_invoice).exists())

        response = self._make_club_admin(auction)
        self.assertEqual(response.status_code, 200)
        auction.refresh_from_db()
        self.assertEqual(auction.club, self.club)
        # The bulk assignment bypassed Auction.save(), but the ledger is still booked.
        self.assertTrue(
            ClubMoney.objects.filter(invoice=buyer_invoice, category=ClubMoney.CATEGORY_AUCTION_SALE).exists()
        )

    def test_does_not_reassign_auction_already_in_a_club(self):
        auction = self._auction(club=self.other_club)
        self._make_club_admin(auction)
        auction.refresh_from_db()
        self.assertEqual(auction.club, self.other_club)


class BapTop10ChartTests(TestCase):
    """Cumulative points-over-time chart for the top 10 club members.

    Green = current user, red = current first place, blue = everyone else; the chart
    mirrors the data behind the "my points" chart but for the leaderboard's top 10,
    and respects the year-to-date toggle.
    """

    GREEN = "#198754"
    RED = "#dc3545"
    BLUE = "#0d6efd"

    def setUp(self):
        from auctions.views.base import _club_top10_chart_data, _last_n_month_starts, _ytd_month_starts

        self._chart = _club_top10_chart_data
        self._all_months = _last_n_month_starts(60)
        self._ytd_months = _ytd_month_starts()
        self.club = Club.objects.create(name="Chart Club", slug="chartclub", enable_breeder_award_program=True)
        self.this_year = timezone.now().year
        # Three members so we can see all three colors at once.
        self.first = self._member("First Place", user_name="firstplace")
        self.current = self._member("Current User", user_name="currentuser")
        self.third = self._member("Third Member", user_name="thirdmember")
        # First place: most points. Current user: middle. Third: least.
        self._award(self.first, points=30, month_offset=2)
        self._award(self.first, points=20, month_offset=1)
        self._award(self.current, points=15, month_offset=2)
        self._award(self.current, points=10, month_offset=0)
        self._award(self.third, points=5, month_offset=1)

    def _member(self, name, user_name):
        user = User.objects.create_user(user_name, f"{user_name}@example.com", "pw")
        return ClubMember.objects.create(club=self.club, user=user, name=name)

    def _award(self, member, points, month_offset=0, year=None, lot=None):
        today = timezone.now().date().replace(day=1)
        month = today.month - month_offset
        year = year or self.this_year
        while month <= 0:
            month += 12
            year -= 1
        return BapAward.objects.create(club_member=member, date=datetime.date(year, month, 1), points=points, lot=lot)

    def _chart_data(self, current_member=None, is_ytd=False):
        months = self._ytd_months if is_ytd else self._all_months
        return self._chart(self.club, "bap_points", "points", current_member, months, is_ytd=is_ytd)

    def test_color_scheme(self):
        data = self._chart_data(current_member=self.current)
        by_label = {d["label"]: d for d in data["datasets"]}
        self.assertEqual(by_label["First Place"]["borderColor"], self.RED)
        self.assertEqual(by_label["Current User"]["borderColor"], self.GREEN)
        self.assertEqual(by_label["Third Member"]["borderColor"], self.BLUE)

    def test_current_user_wins_when_also_first_place(self):
        # If the viewer is the leader, the current-user color (green) takes precedence.
        data = self._chart_data(current_member=self.first)
        by_label = {d["label"]: d for d in data["datasets"]}
        self.assertEqual(by_label["First Place"]["borderColor"], self.GREEN)

    def test_first_place_dataset_is_ordered_first(self):
        data = self._chart_data()
        self.assertEqual(data["datasets"][0]["label"], "First Place")

    def test_cumulative_running_totals(self):
        data = self._chart_data(current_member=self.current)
        by_label = {d["label"]: d for d in data["datasets"]}
        # First place ends at 30 + 20 = 50, monotonic non-decreasing.
        first_series = by_label["First Place"]["data"]
        self.assertEqual(first_series[-1], 50)
        self.assertEqual(first_series, sorted(first_series))

    def test_limited_to_ten_members(self):
        for i in range(12):
            m = self._member(f"Filler {i}", user_name=f"filler{i}")
            self._award(m, points=1, month_offset=1)
        data = self._chart_data()
        self.assertEqual(len(data["datasets"]), 10)

    def test_returns_none_without_points(self):
        empty = Club.objects.create(name="Empty", slug="emptyclub", enable_breeder_award_program=True)
        self.assertIsNone(self._chart(empty, "bap_points", "points", None, self._all_months, is_ytd=False))

    def test_ytd_excludes_prior_years(self):
        # An award from a prior year must not appear in the YTD running total.
        self._award(self.current, points=100, year=self.this_year - 1, month_offset=0)
        self.current.refresh_from_db()
        data = self._chart_data(current_member=self.current, is_ytd=True)
        by_label = {d["label"]: d for d in data["datasets"]}
        # YTD total for current user is 15 + 10 = 25, not 125.
        self.assertEqual(by_label["Current User"]["data"][-1], 25)

    def test_all_time_includes_prior_years(self):
        self._award(self.current, points=100, year=self.this_year - 1, month_offset=0)
        data = self._chart_data(current_member=self.current, is_ytd=False)
        by_label = {d["label"]: d for d in data["datasets"]}
        self.assertEqual(by_label["Current User"]["data"][-1], 125)

    def test_deleted_lot_award_excluded_to_match_leaderboard(self):
        # Awards tied to a deleted lot are dropped from the leaderboard totals, so the
        # chart must drop them too (otherwise the line ends above the leaderboard number).
        auction = Auction.objects.create(
            created_by=self.current.user,
            title="Chart Auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=4),
            club=self.club,
        )
        lot = Lot.objects.create(lot_name="Deleted lot", auction=auction, quantity=1, is_deleted=True)
        self._award(self.current, points=99, month_offset=0, lot=lot)
        self.current.refresh_from_db()
        data = self._chart_data(current_member=self.current)
        by_label = {d["label"]: d for d in data["datasets"]}
        self.assertEqual(by_label["Current User"]["data"][-1], self.current.bap_points)
        self.assertEqual(by_label["Current User"]["data"][-1], 25)

    def test_hap_and_cap_use_their_own_fields(self):
        BapAward.objects.create(
            club_member=self.first, date=timezone.now().date().replace(day=1), hap_points=7, cap_points=3
        )
        self.first.refresh_from_db()
        hap = self._chart(self.club, "hap_points", "hap_points", None, self._all_months)
        cap = self._chart(self.club, "culture_points", "cap_points", None, self._all_months)
        self.assertEqual(hap["datasets"][0]["data"][-1], 7)
        self.assertEqual(cap["datasets"][0]["data"][-1], 3)

    def test_chart_markup_renders_in_bap_tab(self):
        client = Client()
        client.force_login(self.current.user)
        response = client.get(reverse("club_detail_tab", kwargs={"slug": self.club.slug, "tab": "bap"}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Both the canvas and its json_script payload must be present for the chart to draw.
        self.assertIn('id="bap-top10-chart-ytd"', html)
        self.assertIn('id="bap-top10-chart-ytd-data"', html)
        self.assertIsNotNone(response.context.get("bap_top10_chart_ytd"))


class ClubTreasurerReportViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="treasury_owner", password="testpass", email="owner@example.com")
        self.money_user = User.objects.create_user(
            username="treasury_user", password="testpass", email="money@example.com"
        )
        self.club = Club.objects.create(name="Treasury Club", enable_membership=True)
        ClubMember.objects.create(club=self.club, user=self.money_user, permission_money=True)
        ClubMember.objects.create(
            club=self.club, name="Renewed Member", membership_last_paid=datetime.date(2026, 5, 10), is_deleted=False
        )
        ClubMoney.objects.create(
            club=self.club,
            date=datetime.date(2026, 5, 5),
            amount=Decimal("12.00"),
            description="In range",
            category=ClubMoney.CATEGORY_DONATION,
        )
        ClubMoney.objects.create(
            club=self.club,
            date=datetime.date(2026, 4, 5),
            amount=Decimal("8.00"),
            description="Out of range",
            category=ClubMoney.CATEGORY_DONATION,
        )
        auction = Auction.objects.create(
            created_by=self.owner,
            title="Treasury Auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=4),
            club=self.club,
        )
        pickup = PickupLocation.objects.create(name="Treasury Pickup", auction=auction, pickup_time=timezone.now())
        tos = AuctionTOS.objects.create(user=self.owner, auction=auction, pickup_location=pickup)
        Invoice.objects.create(auctiontos_user=tos, status="DRAFT")
        self.client.force_login(self.money_user)

    def test_treasurer_report_page_loads(self):
        response = self.client.get(
            reverse("club_treasurer_report", kwargs={"slug": self.club.slug}),
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Treasurer's Report")
        self.assertContains(response, "In range")

    def test_treasurer_report_export_respects_date_filters(self):
        response = self.client.get(
            reverse("club_treasurer_report_export", kwargs={"slug": self.club.slug}),
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("In range", content)
        self.assertNotIn("Out of range", content)

    def test_treasurer_report_add_record_returns_json(self):
        response = self.client.post(
            reverse("club_money_add", kwargs={"slug": self.club.slug}),
            {
                "date": "2026-05-20",
                "amount": "-4.00",
                "description": "Room rental",
                "category": ClubMoney.CATEGORY_MEETING_LOCATION_COST,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(ClubMoney.objects.filter(club=self.club, description="Room rental").exists())

    def test_treasurer_report_balance_books_creates_adjustment(self):
        response = self.client.post(
            reverse("club_money_balance", kwargs={"slug": self.club.slug}),
            {"account_balance": "25.00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(ClubMoney.objects.filter(club=self.club, category=ClubMoney.CATEGORY_ADJUSTMENT).exists())


class ClubTreasurerOutstandingInvoiceTests(TestCase):
    """The treasurer report's "outstanding invoices" figure.

    An invoice is outstanding only when, after payments, the member still owes the club.
    Regression guard for invoices being reported as outstanding when they had actually
    been paid (the old code looked at the invoice total before payments).
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="oi_owner", password="pw", email="oi_owner@example.com")
        self.money_user = User.objects.create_user(username="oi_money", password="pw", email="oi_money@example.com")
        self.club = Club.objects.create(name="Outstanding Club")
        ClubMember.objects.create(club=self.club, user=self.money_user, permission_money=True)
        self.auction = Auction.objects.create(
            created_by=self.owner,
            title="Outstanding Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 5, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 5, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        self.pickup = PickupLocation.objects.create(name="OI Pickup", auction=self.auction, pickup_time=timezone.now())
        self.start = datetime.date(2026, 5, 1)
        self.end = datetime.date(2026, 5, 31)
        self._tos_count = 0

    def _invoice(self, calculated_total, status="UNPAID", payment=None, auction=None):
        auction = auction or self.auction
        self._tos_count += 1
        tos = AuctionTOS.objects.create(
            name=f"Member {self._tos_count}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )
        invoice = Invoice.objects.create(auctiontos_user=tos, status=status)
        Invoice.objects.filter(pk=invoice.pk).update(calculated_total=calculated_total)
        if payment is not None:
            InvoicePayment.objects.create(invoice=invoice, amount=Decimal(str(payment)))
        return invoice

    def _summary(self):
        from auctions.views import ClubTreasurerReportView

        view = ClubTreasurerReportView()
        view.club = self.club
        return view._outstanding_invoices(self.start, self.end)

    def test_unpaid_invoice_with_balance_is_outstanding(self):
        self._invoice(calculated_total=-50, status="UNPAID")
        result = self._summary()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["amount"], Decimal("50.00"))

    def test_invoice_owing_cents_is_outstanding(self):
        """A member owing $0.75 must be counted and its balance reported.

        Regression for calculated_total being an IntegerField: -0.75 was truncated to 0, so the
        balance came out to 0 and the invoice was silently dropped from the outstanding total.
        """
        self._invoice(calculated_total=Decimal("-0.75"), status="UNPAID")
        result = self._summary()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["amount"], Decimal("0.75"))

    def test_fully_paid_invoice_is_not_outstanding(self):
        # Paid in full but still UNPAID (admin hasn't flipped status) — must NOT be counted.
        self._invoice(calculated_total=-30, status="UNPAID", payment=30)
        result = self._summary()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["amount"], Decimal("0.00"))

    def test_partial_payment_counts_only_remaining_balance(self):
        self._invoice(calculated_total=-10, status="UNPAID", payment=4)
        result = self._summary()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["amount"], Decimal("6.00"))

    def test_paid_status_invoice_is_not_outstanding(self):
        self._invoice(calculated_total=-25, status="PAID")
        self.assertEqual(self._summary()["count"], 0)

    def test_seller_invoice_owed_by_club_is_not_outstanding(self):
        # Positive total == the club owes the seller; that is a payout, not an outstanding receivable.
        self._invoice(calculated_total=40, status="UNPAID")
        self.assertEqual(self._summary()["count"], 0)

    def test_counts_and_amounts_aggregate(self):
        self._invoice(calculated_total=-50, status="UNPAID")  # owes 50
        self._invoice(calculated_total=-30, status="UNPAID", payment=30)  # settled
        self._invoice(calculated_total=-10, status="UNPAID", payment=4)  # owes 6
        self._invoice(calculated_total=40, status="UNPAID")  # club owes seller
        self._invoice(calculated_total=-20, status="PAID")  # paid
        result = self._summary()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["amount"], Decimal("56.00"))

    def test_auction_outside_date_range_is_excluded(self):
        other = Auction.objects.create(
            created_by=self.owner,
            title="Old Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 1, 10, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 1, 11, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        PickupLocation.objects.create(name="Old Pickup", auction=other, pickup_time=timezone.now())
        self._invoice(calculated_total=-99, status="UNPAID", auction=other)
        self.assertEqual(self._summary()["count"], 0)

    def test_report_page_shows_outstanding_amount(self):
        self._invoice(calculated_total=-50, status="UNPAID")
        client = Client()
        client.force_login(self.money_user)
        response = client.get(
            reverse("club_treasurer_report", kwargs={"slug": self.club.slug}),
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
        )
        self.assertEqual(response.status_code, 200)
        summary = response.context["report_summary"]
        self.assertEqual(summary["outstanding_invoices"], 1)
        self.assertEqual(summary["outstanding_invoices_amount"], Decimal("50.00"))
        self.assertContains(response, "still owed to the club")

    def test_summary_reports_money_in_out_and_net(self):
        ClubMoney.objects.create(
            club=self.club,
            date=datetime.date(2026, 5, 10),
            amount=Decimal("100.00"),
            category=ClubMoney.CATEGORY_DONATION,
        )
        ClubMoney.objects.create(
            club=self.club,
            date=datetime.date(2026, 5, 11),
            amount=Decimal("-40.00"),
            category=ClubMoney.CATEGORY_SPEAKER_COSTS,
        )
        client = Client()
        client.force_login(self.money_user)
        response = client.get(
            reverse("club_treasurer_report", kwargs={"slug": self.club.slug}),
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
        )
        summary = response.context["report_summary"]
        self.assertEqual(summary["money_in"], Decimal("100.00"))
        self.assertEqual(summary["money_out"], Decimal("40.00"))
        self.assertEqual(summary["net"], Decimal("60.00"))
