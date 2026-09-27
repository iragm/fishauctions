"""Regression tests for taking a lot away from a winner, and the money and 500s around it.

Invoice.calculated_total is stored, and only setting a winner recalculated it, so undoing a sale, a
force_save over a sale, editing a lot, a no-show refund or deleting a bidder left the loser's invoice
stale. Also here: label printing limits, volunteer bounties, invoice adjustment caps, the lot queue
under two scanners, and POST endpoints that 500'd on a missing or non-numeric field.
"""

import uuid
from decimal import Decimal
from unittest.mock import PropertyMock, patch

from django.db import IntegrityError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_weasyprint.views import WeasyTemplateResponse

from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    LotQueueEntry,
    MobileDevice,
    PickupLocation,
    TapToPayAttempt,
    UserLabelPrefs,
    VolunteerJob,
    VolunteerSignup,
)
from auctions.tests import StandardTestCase

# push_configured() only checks this is non-empty; nothing here sends a push.
FAKE_FIREBASE = '{"type": "service_account", "project_id": "x"}'


class InvoiceAssertions:
    def assert_current(self, invoice, *, changed_from=None):
        """The stored total is what the invoice adds up to now, and (optionally) moved."""
        stored = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual(stored.calculated_total, Invoice.objects.get(pk=invoice.pk).rounded_net)
        if changed_from is not None:
            self.assertNotEqual(stored.calculated_total, changed_from)

    def stored_total(self, invoice):
        return Invoice.objects.get(pk=invoice.pk).calculated_total


class InPersonSaleBase(InvoiceAssertions, StandardTestCase):
    """in_person_lot is "101-1", sold by admin_in_person_tos; in_person_buyer is bidder 555."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_user)
        self.seller_invoice = Invoice.objects.create(auctiontos_user=self.admin_in_person_tos)
        self.set_winners_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        self.undo_url = reverse("auction_unsell_lot", kwargs={"slug": self.in_person_auction.slug})

    def sell(self, action="save", price="10", winner="555"):
        data = {"lot": "101-1", "price": price, "winner": winner, "action": action}
        return self.client.post(self.set_winners_url, data).json()

    def buyer_invoice(self):
        return Invoice.objects.get(auctiontos_user=self.in_person_buyer)

    def sold_lot(self):
        return Lot.objects.get(pk=self.in_person_lot.pk)


class UndoSaleTests(InPersonSaleBase):
    def test_undo_recalculates_the_old_winner_and_the_seller(self):
        self.sell()
        self.seller_invoice.recalculate()
        buyer_before = self.stored_total(self.buyer_invoice())
        seller_before = self.stored_total(self.seller_invoice)
        self.client.post(self.undo_url, {"lot_number": "101-1"})
        self.assertIsNone(self.sold_lot().auctiontos_winner)
        self.assert_current(self.buyer_invoice(), changed_from=buyer_before)
        self.assert_current(self.seller_invoice, changed_from=seller_before)

    def test_a_settled_invoice_is_refused_unless_forced(self):
        self.sell()
        Invoice.objects.filter(auctiontos_user=self.in_person_buyer).update(status="PAID")
        data = self.client.post(self.undo_url, {"lot_number": "101-1"}).json()
        self.assertEqual(data["banner"], "error")
        self.assertIn("paid", data["success_message"])
        self.assertEqual(self.sold_lot().auctiontos_winner, self.in_person_buyer)
        self.client.post(self.undo_url, {"lot_number": "101-1", "force": "1"})
        self.assertIsNone(self.sold_lot().auctiontos_winner)

    def test_a_non_numeric_lot_number_is_no_lot(self):
        Auction.objects.filter(pk=self.in_person_auction.pk).update(use_seller_dash_lot_numbering=False)
        response = self.client.post(self.undo_url, {"lot_number": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "No lot found")

    def test_online_auctions_are_refused(self):
        url = reverse("auction_unsell_lot", kwargs={"slug": self.online_auction.slug})
        Auction.objects.filter(pk=self.online_auction.pk).update(use_seller_dash_lot_numbering=True)
        Lot.objects.filter(pk=self.lot.pk).update(custom_lot_number="1")
        data = self.client.post(url, {"lot_number": "1"}).json()
        self.assertIn("online", data["message"])
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).auctiontos_winner, self.tosB)


class ForceSaveOverASaleTests(InPersonSaleBase):
    def test_the_previous_winner_is_recalculated(self):
        self.sell()
        before = self.stored_total(self.buyer_invoice())
        self.sell(action="force_save", price="15", winner="504")
        self.assertEqual(self.sold_lot().auctiontos_winner, self.in_person_tos)
        self.assert_current(self.buyer_invoice(), changed_from=before)


class LotAdminTests(InPersonSaleBase):
    def url(self):
        return reverse("auctionlotadmin", kwargs={"pk": self.in_person_lot.pk})

    def data(self, **overrides):
        data = {
            "lot_name": self.in_person_lot.lot_name,
            "auction": self.in_person_auction.pk,
            "species": "",
            "species_category": "",
            "summernote_description": "",
            "quantity": 1,
            "donation": "",
            "i_bred_this_fish": "",
            "buy_now_price": "",
            "reserve_price": 5,
            "banned": "",
            "auctiontos_winner": "",
            "winning_price": "",
            "custom_checkbox": "",
            "custom_field_1": "",
            "custom_dropdown": "",
        }
        data.update(overrides)
        return data

    def test_clearing_the_winner_recalculates_their_invoice_and_the_sellers(self):
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="10"))
        buyer_before = self.stored_total(self.buyer_invoice())
        self.client.post(self.url(), self.data())
        self.assertIsNone(self.sold_lot().auctiontos_winner)
        self.assert_current(self.buyer_invoice(), changed_from=buyer_before)
        self.assert_current(self.seller_invoice)

    def test_reassigning_the_winner_recalculates_the_old_one(self):
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="10"))
        before = self.stored_total(self.buyer_invoice())
        self.client.post(self.url(), self.data(auctiontos_winner=self.in_person_tos.pk, winning_price="10"))
        self.assert_current(self.buyer_invoice(), changed_from=before)

    def test_a_negative_winning_price_is_refused(self):
        response = self.client.post(
            self.url(), self.data(auctiontos_winner=self.in_person_buyer.pk, winning_price="-5")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.sold_lot().winning_price)


class NoShowRefundTests(InPersonSaleBase):
    def test_refunding_bought_lots_recalculates_both_sides(self):
        self.sell()
        self.seller_invoice.recalculate()
        buyer_before = self.stored_total(self.buyer_invoice())
        seller_before = self.stored_total(self.seller_invoice)
        url = reverse("auction_no_show_dialog", kwargs={"slug": self.in_person_auction.slug, "tos": "555"})
        self.client.post(url, {"refund_bought_lots": "on"})
        self.assertEqual(self.sold_lot().partial_refund_percent, 100)
        self.assert_current(self.buyer_invoice(), changed_from=buyer_before)
        self.assert_current(self.seller_invoice, changed_from=seller_before)


class AuctionTOSDeleteTests(InvoiceAssertions, StandardTestCase):
    def test_deleting_a_winner_recalculates_the_sellers_invoice(self):
        spare = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Spare person",
            bidder_number="777",
            manually_added=True,
        )
        Lot.objects.create(
            lot_name="Won by spare",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=spare,
            winning_price=50,
            active=False,
        )
        Invoice.objects.filter(auctiontos_user=spare).delete()
        self.invoice.recalculate()
        before = self.stored_total(self.invoice)
        self.client.force_login(self.admin_user)
        url = reverse("auctiontosdelete", kwargs={"pk": spare.pk})
        data = {
            "auction": self.online_auction.pk,
            "exclude_auctiontos": spare.pk,
            "merge_with": "",
            "delete_lots": "on",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assert_current(self.invoice, changed_from=before)


class InvoiceAbsorbTests(StandardTestCase):
    def test_absorbing_moves_payments_adjustments_and_tap_to_pay_attempts(self):
        """What merging two participants moves; a plain delete used to cascade them away."""
        other = Invoice.for_participant(self.tosC)
        payment = InvoicePayment.objects.create(invoice=other, amount=Decimal("5.00"), payment_method="cash")
        adjustment = InvoiceAdjustment.objects.create(invoice=other, amount=3, notes="dup")
        attempt = TapToPayAttempt.objects.create(invoice=other, attempt_id="dup-attempt")
        self.invoiceB.absorb(other)
        self.assertEqual(InvoicePayment.objects.get(pk=payment.pk).invoice_id, self.invoiceB.pk)
        self.assertEqual(InvoiceAdjustment.objects.get(pk=adjustment.pk).invoice_id, self.invoiceB.pk)
        self.assertEqual(TapToPayAttempt.objects.get(pk=attempt.pk).invoice_id, self.invoiceB.pk)


class LabelPrintingTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.endAuction()
        UserLabelPrefs.objects.get_or_create(user=self.user)
        self.url = reverse("print_my_labels", kwargs={"slug": self.online_auction.slug})
        self.client.force_login(self.user)

    def printed(self):
        return Lot.objects.filter(auctiontos_seller=self.online_tos, label_printed=True).count()

    def test_a_failed_render_leaves_labels_unprinted(self):
        with patch.object(WeasyTemplateResponse, "rendered_content", new_callable=PropertyMock) as rendered:
            rendered.side_effect = RuntimeError("WeasyPrint fell over")
            with self.assertRaises(RuntimeError):
                self.client.get(self.url)
        self.assertEqual(self.printed(), 0)

    def test_a_rendered_pdf_marks_its_labels_printed(self):
        response = self.client.get(self.url)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertGreater(self.printed(), 0)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.label_first_printed_by, self.user)

    def test_sheet_labels_are_capped_per_pdf(self):
        from auctions.views import LotLabelView

        with patch.object(LotLabelView, "MAX_PDF_LABELS", 2):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any("Only the first 2 labels" in message for message in messages), messages)
        self.assertEqual(self.printed(), 2)

    def test_an_unknown_auction_is_a_404(self):
        response = self.client.get(reverse("print_my_labels", kwargs={"slug": "no-such-auction"}))
        self.assertEqual(response.status_code, 404)

    def test_a_single_lot_with_no_auction_is_a_404(self):
        standalone = Lot.objects.create(lot_name="Standalone", user=self.user, quantity=1)
        response = self.client.get(reverse("single_lot_label", kwargs={"pk": standalone.pk}))
        self.assertEqual(response.status_code, 404)

    def test_a_single_lot_with_no_seller_is_only_its_owners(self):
        orphan = Lot.objects.create(lot_name="No seller", auction=self.online_auction, quantity=1)
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(reverse("single_lot_label", kwargs={"pk": orphan.pk}))
        self.assertEqual(response.status_code, 302)


@override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
class VolunteerBountyTests(InvoiceAssertions, StandardTestCase):
    def setUp(self):
        super().setUp()
        MobileDevice.objects.create(
            user=self.user_with_no_lots, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True
        )
        self.client.force_login(self.user_with_no_lots)

    def accept(self, bounty):
        job = VolunteerJob.objects.create(
            auction=self.in_person_auction, created_by=self.admin_user, description="Sort lots", bounty=bounty
        )
        self.client.post(
            reverse("auction_volunteer_job", kwargs={"slug": self.in_person_auction.slug, "job_pk": job.pk})
        )
        return job

    def test_someone_the_job_was_not_announced_to_cannot_sign_up(self):
        MobileDevice.objects.filter(user=self.user_with_no_lots).delete()
        job = self.accept(Decimal(10))
        self.assertFalse(VolunteerSignup.objects.filter(job=job).exists())
        self.assertFalse(InvoiceAdjustment.objects.filter(notes="Volunteer: Sort lots").exists())

    def test_a_bounty_needs_an_open_invoice(self):
        Invoice.objects.create(auctiontos_user=self.in_person_buyer, status="PAID")
        job = self.accept(Decimal(10))
        self.assertFalse(VolunteerSignup.objects.filter(job=job).exists())
        self.assertFalse(InvoiceAdjustment.objects.filter(notes="Volunteer: Sort lots").exists())

    def test_a_negative_bounty_adds_no_adjustment(self):
        job = self.accept(Decimal(-5))
        signup = VolunteerSignup.objects.get(job=job)
        self.assertIsNone(signup.invoice_adjustment)

    def test_the_bounty_is_on_the_stored_total(self):
        invoice = Invoice.objects.create(auctiontos_user=self.in_person_buyer)
        before = self.stored_total(invoice)
        job = self.accept(Decimal(10))
        self.assertEqual(VolunteerSignup.objects.get(job=job).invoice_adjustment.amount, 10)
        self.assert_current(invoice, changed_from=before)


class AdjustmentCapTests(StandardTestCase):
    def test_barcode_adjustments_of_infinity_or_too_much_do_not_500(self):
        club = Club.objects.create(name="Cap club")
        Auction.objects.filter(pk=self.in_person_auction.pk).update(club=club)
        self.client.force_login(self.admin_user)
        url = reverse("auction_barcode_scan", kwargs={"slug": self.in_person_auction.slug})
        data = {"apply_to_bidder_number": "555", "adjustment_type": "ADD", "adjustment_label": "fee"}
        response = self.client.post(url, {**data, "adjustment_amount": "inf"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(url, {**data, "adjustment_amount": "1000000"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice__auctiontos_user=self.in_person_buyer).exists())

    def test_the_invoice_formset_caps_the_amount(self):
        self.client.force_login(self.admin_user)
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-adjustment_type": "ADD",
            "form-0-amount": "100000",
            "form-0-notes": "typo",
        }
        response = self.client.post(reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk}), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InvoiceAdjustment.objects.filter(invoice=self.invoice).exists())


class MissingFieldTests(StandardTestCase):
    """POSTs that raised KeyError or ValueError on a missing or non-numeric field."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_user)

    def test_category_finder(self):
        response = self.client.post(reverse("guess_category"), {})
        self.assertEqual(response.json(), {"value": None})

    def test_auction_finder(self):
        self.assertEqual(self.client.post(reverse("get_auction_info"), {}).json(), {})
        self.assertEqual(self.client.post(reverse("get_auction_info"), {"auction": "abc"}).json(), {})

    def test_chat_subscribe(self):
        response = self.client.post(reverse("lot_chat_subscribe"), {"lot": self.lot.pk})
        self.assertEqual(response.json(), {"unsubscribed": False})
        self.assertEqual(self.client.post(reverse("lot_chat_subscribe"), {"lot": "abc"}).status_code, 404)

    def test_tos_memo(self):
        url = reverse("auctiontosmemo", kwargs={"pk": self.tosB.pk})
        self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_tos_memo_writes_only_the_memo(self):
        url = reverse("auctiontosmemo", kwargs={"pk": self.tosB.pk})
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.post(url, {"memo": "late"}).json(), {"result": "ok"})
        updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE `auctions_auctiontos`")]
        self.assertTrue(updates)
        for sql in updates:
            self.assertNotIn("`bidder_number`", sql)
        self.assertEqual(AuctionTOS.objects.get(pk=self.tosB.pk).memo, "late")

    def test_find_image_icon(self):
        url = reverse("auto_image_available", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_lot_queue_remove(self):
        url = reverse("auction_lot_queue", kwargs={"slug": self.in_person_auction.slug})
        self.assertEqual(self.client.post(url, {"action": "remove", "entry_id": "abc"}).status_code, 200)

    def test_pickup_location_with_no_auction(self):
        location = PickupLocation.objects.create(name="Nowhere")
        self.assertEqual(self.client.get(reverse("edit_pickup", kwargs={"pk": location.pk})).status_code, 404)
        self.assertEqual(self.client.get(reverse("delete_pickup", kwargs={"pk": location.pk})).status_code, 404)


class ViewLotActivityTests(StandardTestCase):
    def test_viewing_a_lot_writes_only_last_activity(self):
        self.client.force_login(self.user_with_no_lots)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE `auctions_userdata`")]
        self.assertTrue(updates)
        for sql in updates:
            self.assertNotIn("`email_me_about_new_auctions`", sql)


class LotQueueTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_user)
        self.url = reverse("auction_lot_queue", kwargs={"slug": self.in_person_auction.slug})

    def queued_lot(self, name, order):
        lot = Lot.objects.create(
            lot_name=name, auction=self.in_person_auction, auctiontos_seller=self.admin_in_person_tos, quantity=1
        )
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot, order=order)
        return lot

    def test_losing_a_race_to_queue_a_lot_is_already_queued(self):
        with patch.object(LotQueueEntry.objects, "get_or_create", side_effect=IntegrityError):
            response = self.client.post(self.url, {"action": "add", "value": "101-1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in the queue")

    def test_adding_goes_to_the_end(self):
        self.queued_lot("first", 7)
        self.client.post(self.url, {"action": "add", "value": "101-1"})
        self.assertEqual(LotQueueEntry.objects.get(lot=self.in_person_lot).order, 8)

    def test_set_winners_skips_lots_sold_elsewhere_and_keeps_them_queued(self):
        from auctions.views.selling import queue_next_to_record

        sold = self.queued_lot("sold on its own page", 1)
        waiting = self.queued_lot("still waiting", 2)
        Lot.objects.filter(pk=sold.pk).update(auctiontos_winner=self.in_person_buyer, winning_price=5)
        self.assertEqual(queue_next_to_record(self.in_person_auction), waiting)
        self.assertTrue(LotQueueEntry.objects.filter(lot=sold).exists())
