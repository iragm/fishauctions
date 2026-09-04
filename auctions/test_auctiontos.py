"""``AuctionTOS``: the admin filter over it, feedback, and merging two participants."""

import datetime

from django.urls import reverse
from django.utils import timezone

from auctions.filters import LotAdminFilter
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Bid,
    Club,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PageView,
    PickupLocation,
)
from auctions.tests import StandardTestCase


class LotAdminFilterTests(StandardTestCase):
    """Test the keyword filters in LotAdminFilter"""

    def setUp(self):
        super().setUp()
        # Create additional test data for filter testing

        # Create a lot with no bids
        self.lot_no_bids = Lot.objects.create(
            lot_name="Lot with no bids",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=True,
        )

        # Create a lot with bids
        self.lot_with_bids = Lot.objects.create(
            lot_name="Lot with bids",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=True,
        )
        Bid.objects.create(
            user=self.userB,
            lot_number=self.lot_with_bids,
            amount=5,
        )

        # Create a lot with winner who viewed via QR
        self.lot_winner_viewed_qr = Lot.objects.create(
            lot_name="Winner viewed via QR",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=15,
            winner=self.userB,
            auctiontos_winner=self.tosB,
            active=False,
        )
        PageView.objects.create(
            user=self.userB,
            lot_number=self.lot_winner_viewed_qr,
            source="qr",
        )

        # Create a lot with winner who did not view via QR
        self.lot_winner_not_viewed_qr = Lot.objects.create(
            lot_name="Winner not viewed via QR",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=20,
            winner=self.user_with_no_lots,
            auctiontos_winner=self.tosC,
            active=False,
        )

    def test_hasbids_filter(self):
        """Test that 'hasbids' filter returns lots with bids"""
        from auctions.filters import LotAdminFilter

        # Create a queryset of all lots in the auction
        qs = Lot.objects.filter(auction=self.online_auction)

        # Create an instance of LotAdminFilter to use its generic method
        filter_instance = LotAdminFilter()
        filter_instance.queryset = qs

        # Search for lots with bids
        filtered_qs = filter_instance.generic(qs, "hasbids")

        # Should include lot with bids
        self.assertIn(self.lot_with_bids, filtered_qs)

        # Should not include lot without bids
        self.assertNotIn(self.lot_no_bids, filtered_qs)

    def test_nobids_filter(self):
        """Test that 'nobids' filter returns lots without bids"""
        from auctions.filters import LotAdminFilter

        # Create a queryset of all lots in the auction
        qs = Lot.objects.filter(auction=self.online_auction)

        # Create an instance of LotAdminFilter to use its generic method
        filter_instance = LotAdminFilter()
        filter_instance.queryset = qs

        # Search for lots without bids
        filtered_qs = filter_instance.generic(qs, "nobids")

        # Should include lot without bids
        self.assertIn(self.lot_no_bids, filtered_qs)

        # Should not include lot with bids
        self.assertNotIn(self.lot_with_bids, filtered_qs)

    def test_qrviewed_filter(self):
        """Test that 'qrviewed' filter returns lots where winner viewed via QR code"""
        from auctions.filters import LotAdminFilter

        # Create a queryset of all lots in the auction
        qs = Lot.objects.filter(auction=self.online_auction)

        # Create an instance of LotAdminFilter to use its generic method
        filter_instance = LotAdminFilter()
        filter_instance.queryset = qs

        # Search for lots where winner viewed via QR
        filtered_qs = filter_instance.generic(qs, "qrviewed")

        # Should include lot with winner who viewed via QR
        self.assertIn(self.lot_winner_viewed_qr, filtered_qs)

        # Should not include lot with winner who did not view via QR
        self.assertNotIn(self.lot_winner_not_viewed_qr, filtered_qs)

        # Should not include lots without winners
        self.assertNotIn(self.lot_no_bids, filtered_qs)

    def test_qrnotviewed_filter(self):
        """Test that 'qrnotviewed' filter returns lots where winner has not viewed via QR code"""
        from auctions.filters import LotAdminFilter

        # Create a queryset of all lots in the auction
        qs = Lot.objects.filter(auction=self.online_auction)

        # Create an instance of LotAdminFilter to use its generic method
        filter_instance = LotAdminFilter()
        filter_instance.queryset = qs

        # Search for lots where winner has not viewed via QR
        filtered_qs = filter_instance.generic(qs, "qrnotviewed")

        # Should include lot with winner who did not view via QR
        self.assertIn(self.lot_winner_not_viewed_qr, filtered_qs)

        # Should not include lot with winner who viewed via QR
        self.assertNotIn(self.lot_winner_viewed_qr, filtered_qs)

        # Should not include lots without winners
        self.assertNotIn(self.lot_no_bids, filtered_qs)

    def test_custom_dropdown_search(self):
        self.lot_no_bids.custom_dropdown = "River"
        self.lot_no_bids.save()
        qs = Lot.objects.filter(auction=self.online_auction)
        filter_instance = LotAdminFilter()
        filter_instance.queryset = qs
        filtered_qs = filter_instance.generic(qs, "River")
        self.assertIn(self.lot_no_bids, filtered_qs)


class FeedbackTestCase(StandardTestCase):
    """Test feedback functionality for buyers and sellers"""

    def test_feedback_text_length_limit(self):
        """Test that feedback text can be up to 500 characters"""
        # Login as the winner
        self.client.login(username="no_tos", password="testpassword")

        # Create a long feedback text (500 characters)
        long_text = "A" * 500

        # Submit feedback as winner (buyer)
        response = self.client.post(f"/api/feedback/{self.lot.lot_number}/winner/", {"text": long_text, "rating": 1})

        # Should be successful
        self.assertEqual(response.status_code, 200)

        # Verify the feedback was saved
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.feedback_text, long_text)
        self.assertEqual(lot.feedback_rating, 1)

    def test_feedback_text_truncation(self):
        """Test that feedback text longer than 500 characters is truncated"""
        # Login as the winner
        self.client.login(username="no_tos", password="testpassword")

        # Create a feedback text longer than 500 characters
        very_long_text = "B" * 600

        # Submit feedback as winner (buyer)
        response = self.client.post(
            f"/api/feedback/{self.lot.lot_number}/winner/", {"text": very_long_text, "rating": 1}
        )

        # Should be successful
        self.assertEqual(response.status_code, 200)

        # Verify the feedback was truncated to 500 characters
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(len(lot.feedback_text), 500)
        self.assertEqual(lot.feedback_text, very_long_text[:500])

    def test_seller_feedback_text_length(self):
        """Test that seller feedback text can be up to 500 characters"""
        # Login as the seller
        self.client.login(username="my_lot", password="testpassword")

        # Create a long feedback text (500 characters)
        long_text = "C" * 500

        # Submit feedback as seller
        response = self.client.post(f"/api/feedback/{self.lot.lot_number}/seller/", {"text": long_text, "rating": -1})

        # Should be successful
        self.assertEqual(response.status_code, 200)

        # Verify the feedback was saved
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.winner_feedback_text, long_text)
        self.assertEqual(lot.winner_feedback_rating, -1)

    def test_seller_feedback_text_truncation(self):
        """Test that seller feedback text longer than 500 characters is truncated"""
        # Login as the seller
        self.client.login(username="my_lot", password="testpassword")

        # Create a feedback text longer than 500 characters
        very_long_text = "D" * 600

        # Submit feedback as seller
        response = self.client.post(
            f"/api/feedback/{self.lot.lot_number}/seller/", {"text": very_long_text, "rating": -1}
        )

        # Should be successful
        self.assertEqual(response.status_code, 200)

        # Verify the feedback was truncated to 500 characters
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(len(lot.winner_feedback_text), 500)
        self.assertEqual(lot.winner_feedback_text, very_long_text[:500])


class AuctionHistoryTestCase(StandardTestCase):
    """Tests for auction history creation"""

    def test_create_history_for_unsaved_auction(self):
        """Test that creating history for an unsaved auction doesn't raise an error"""
        # Create an auction instance without saving it
        auction = Auction(
            created_by=self.user,
            title="Test unsaved auction",
            is_online=True,
            date_start=datetime.datetime(2051, 1, 1, tzinfo=datetime.timezone.utc),  # Invalid year to trigger fix_year
            date_end=datetime.datetime(2051, 1, 2, tzinfo=datetime.timezone.utc),
            winning_bid_percent_to_club=25,
        )

        # This should not raise an error even though create_history is called in fix_year
        auction.save()

        # Verify the auction was saved successfully
        self.assertIsNotNone(auction.pk)

        # Verify that fix_year() corrected the invalid dates to current year
        current_year = timezone.now().year
        self.assertEqual(auction.date_start.year, current_year)
        self.assertEqual(auction.date_end.year, current_year)

        # Verify no history was created during initial save
        history_count = AuctionHistory.objects.filter(auction=auction).count()
        self.assertEqual(history_count, 0)

    def test_create_history_for_saved_auction(self):
        """Test that creating history for a saved auction works correctly"""
        # Use an existing auction
        auction = self.online_auction

        # Manually create history
        auction.create_history("RULES", "Test action")

        # Verify history was created
        history_count = AuctionHistory.objects.filter(auction=auction, action="Test action").count()
        self.assertEqual(history_count, 1)


class MergeAuctionTOSTests(StandardTestCase):
    """Test duplicate AuctionTOS detection and merging"""

    def setUp(self):
        super().setUp()
        # Give online_tos a real email so duplicate checks work
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(email="canonical@example.com")
        self.online_tos.refresh_from_db()
        # Use a DIFFERENT email so save() doesn't auto-merge this duplicate on creation
        # (these tests exercise the explicit merge_duplicate() method, not the auto-merge)
        self.duplicate_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            email="duplicate@example.com",
            name="Duplicate User",
        )

    def test_merge_duplicate_moves_won_lots(self):
        """Merging should reassign won lots from duplicate to canonical TOS"""
        lot = Lot.objects.create(
            lot_name="Won lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=self.duplicate_tos,
            quantity=1,
            winning_price=5,
            active=False,
        )
        self.online_tos.merge_duplicate(self.duplicate_tos)
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_winner, self.online_tos)

    def test_merge_duplicate_moves_sold_lots(self):
        """Merging should reassign sold lots from duplicate to canonical TOS"""
        lot = Lot.objects.create(
            lot_name="Sold lot",
            auction=self.online_auction,
            auctiontos_seller=self.duplicate_tos,
            quantity=1,
            active=False,
        )
        self.online_tos.merge_duplicate(self.duplicate_tos)
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_seller, self.online_tos)

    def test_merge_duplicate_moves_invoice_adjustments(self):
        """Merging should move InvoiceAdjustments from duplicate's invoice to canonical invoice"""
        duplicate_invoice = Invoice.objects.create(
            auctiontos_user=self.duplicate_tos,
            auction=self.online_auction,
        )
        adjustment = InvoiceAdjustment.objects.create(
            invoice=duplicate_invoice,
            adjustment_type="DISCOUNT",
            amount=10,
            notes="Test adjustment",
        )
        self.online_tos.merge_duplicate(self.duplicate_tos)
        adjustment.refresh_from_db()
        canonical_invoice = Invoice.objects.filter(auctiontos_user=self.online_tos).first()
        self.assertEqual(adjustment.invoice, canonical_invoice)

    def test_merge_duplicate_creates_invoice_if_missing(self):
        """Merging should create an invoice for canonical TOS if it doesn't exist"""
        # Remove existing invoice if present
        Invoice.objects.filter(auctiontos_user=self.online_tos).delete()
        self.online_tos.merge_duplicate(self.duplicate_tos)
        invoice = Invoice.objects.filter(auctiontos_user=self.online_tos).first()
        self.assertIsNotNone(invoice)

    def test_merge_duplicate_deletes_duplicate(self):
        """Merging should delete the duplicate AuctionTOS"""
        duplicate_pk = self.duplicate_tos.pk
        self.online_tos.merge_duplicate(self.duplicate_tos)
        self.assertFalse(AuctionTOS.objects.filter(pk=duplicate_pk).exists())

    def test_merge_duplicate_clears_possible_duplicate_link(self):
        """The kept record must not be left pointing at the deleted duplicate, in the database or in memory.

        possible_duplicate is a self-FK, so a stale value here becomes a dangling id: the database
        gets it right via SET_NULL, but this instance keeps the old id and the caller's next save()
        writes it back, which MariaDB rejects with a foreign key error.
        """
        duplicate_pk = self.duplicate_tos.pk
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(possible_duplicate=duplicate_pk)
        AuctionTOS.objects.filter(pk=duplicate_pk).update(possible_duplicate=self.online_tos.pk)
        self.online_tos.refresh_from_db()
        self.online_tos.merge_duplicate(self.duplicate_tos)
        self.assertIsNone(self.online_tos.possible_duplicate_id)
        self.online_tos.save()  # raises IntegrityError if the deleted id gets written back
        self.online_tos.refresh_from_db()
        # save() may re-flag this record against some other live record; it must never point at the
        # row the merge just deleted.
        self.assertNotEqual(self.online_tos.possible_duplicate_id, duplicate_pk)

    def test_merge_duplicate_creates_auction_history(self):
        """Merging should create an AuctionHistory entry attributed to system"""
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        self.online_tos.merge_duplicate(self.duplicate_tos)
        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        self.assertEqual(new_count, initial_count + 1)
        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest("timestamp")
        self.assertIsNone(history.user)
        self.assertIn("Merged", history.action)
        self.assertIn(self.duplicate_tos.bidder_number, history.action)

    def test_admin_add_rejects_duplicate_email(self):
        """Adding a user via admin form with an existing email should make the form invalid"""
        self.client.login(username="admin_user", password="testpassword")
        initial_count = AuctionTOS.objects.filter(auction=self.online_auction).count()
        url = reverse("auctiontosadmin", kwargs={"pk": self.online_auction.slug})
        response = self.client.post(
            url,
            {
                "name": "Duplicate Name",
                "email": self.online_tos.email,
                "pickup_location": self.location.pk,
                "bidder_number": "",
                "phone_number": "",
                "address": "",
                "is_admin": False,
                "bidding_allowed": True,
                "selling_allowed": True,
                "is_club_member": False,
                "memo": "",
            },
        )
        # Form should be invalid — no new TOS created
        new_count = AuctionTOS.objects.filter(auction=self.online_auction).count()
        self.assertEqual(new_count, initial_count)
        # Response should not be a redirect
        self.assertNotEqual(response.status_code, 302)

    def test_duplicate_name_validation_returns_warning_message(self):
        """Duplicate-name validation should warn when the name is already in this auction"""
        self.client.login(username="admin_user", password="testpassword")
        self.online_tos.name = "Duplicate Test User"
        self.online_tos.bidder_number = "123"
        self.online_tos.save()
        url = reverse("auctiontos_validation", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url, {"name": self.online_tos.name})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["name_tooltip"],
            f"There's already a user in this auction named {self.online_tos.name} "
            f"(bidder number: {self.online_tos.bidder_number})",
        )

    def test_duplicate_name_autofill_searches_clubs_with_manage_membership_permission(self):
        """AuctionTOS autofill should search auctions from clubs where the user can manage members"""
        self.client.login(username="admin_user", password="testpassword")
        club = Club.objects.create(name="Autofill Club")
        ClubMember.objects.create(club=club, user=self.admin_user, permission_add_edit=True)
        club_auction = Auction.objects.create(
            created_by=self.user,
            club=club,
            title="Club Managed Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=2),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        club_location = PickupLocation.objects.create(
            name="club location",
            auction=club_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        AuctionTOS.objects.create(
            auction=club_auction,
            pickup_location=club_location,
            manually_added=True,
            name="Club Managed User",
            email="club-managed@example.com",
            bidder_number="777",
        )
        url = reverse("auctiontos_validation", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url, {"name": "Club Managed User"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id_email"], "club-managed@example.com")
        self.assertEqual(response.json()["id_bidder_number"], "")

    def test_add_user_modal_uses_inline_name_note_for_duplicates(self):
        """The add-user modal should render JS that shows duplicate-name warnings inline"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "function setFieldNote(fieldId, message)")
        self.assertContains(response, 'setFieldNote("id_name", response.name_tooltip);')
        self.assertContains(response, "data-htmx-modal-root")
        self.assertContains(response, "window.mountHtmxModal(")

    def test_add_user_modal_save_uses_modal_close_action(self):
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auctiontosadmin", kwargs={"pk": self.online_auction.slug})
        response = self.client.post(
            url,
            {
                "name": "Brand New User",
                "email": "brand-new@example.com",
                "pickup_location": self.location.pk,
                "bidder_number": "",
                "phone_number": "",
                "address": "",
                "is_admin": False,
                "bidding_allowed": True,
                "selling_allowed": True,
                "is_club_member": False,
                "memo": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("closeModal", body)
        self.assertIn("reload-page", body)


class AuctionTOSMergeViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        # Use update() here so the kept record starts linked to a user before the merge flow edits its email.
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(
            name="Kept User",
            email="kept@example.com",
            manually_added=False,
            user=self.user,
        )
        self.online_tos.refresh_from_db()
        self.source_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
            name="Source User",
            email="source@example.com",
            phone_number="5551112222",
            address="111 Source St",
        )

    def test_merge_flow_renders_review_then_updates_kept_user(self):
        url = reverse("auctiontosdelete", kwargs={"pk": self.source_tos.pk}) + "?action=merge"
        response = self.client.post(
            url,
            {
                "action": "merge",
                "target": str(self.online_tos.pk),
                "auction": self.online_auction.pk,
                "exclude_auctiontos": self.source_tos.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This will deactivate")
        self.assertContains(response, "Kept User")

        response = self.client.post(
            url,
            {
                "action": "merge",
                "step": "review",
                "target": str(self.online_tos.pk),
                "name": "Merged Winner",
                "email": "updated@example.com",
                "phone_number": "5553334444",
                "address": "222 Updated Ave",
                "pickup_location": self.location.pk,
            },
        )
        self.assertRedirects(response, reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.name, "Merged Winner")
        self.assertEqual(self.online_tos.email, "updated@example.com")
        self.assertEqual(self.online_tos.phone_number, "5553334444")
        self.assertEqual(self.online_tos.address, "222 Updated Ave")
        self.assertIsNone(self.online_tos.user)
        self.assertFalse(AuctionTOS.objects.filter(pk=self.source_tos.pk).exists())

    def test_merge_review_keeping_target_when_reviewed_email_matches_source(self):
        """Reviewing with the source's email must not 500 via save()'s auto-merge deleting the target.

        Regression: submitting the review with the target's email set to the source's email used to
        trip AuctionTOS.save()'s exact-email auto-merge, which kept the older source and deleted the
        target — the next merge_duplicate() call then raised "Unsaved model instance ... in an ORM
        query". The target must survive and keep the email; the source must be gone.
        """
        won_lot = Lot.objects.create(
            lot_name="Won by source",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=self.source_tos,
            winning_price=5,
        )
        url = reverse("auctiontosdelete", kwargs={"pk": self.source_tos.pk}) + "?action=merge"
        response = self.client.post(
            url,
            {
                "action": "merge",
                "step": "review",
                "target": str(self.online_tos.pk),
                "name": "Kept User",
                "email": self.source_tos.email,  # same email as the source we're deleting
                "phone_number": "5553334444",
                "address": "222 Updated Ave",
                "pickup_location": self.location.pk,
            },
        )
        self.assertRedirects(response, reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.email, "source@example.com")
        self.assertFalse(AuctionTOS.objects.filter(pk=self.source_tos.pk).exists())
        # The source's won lot moved to the kept target (not lost to a backwards auto-merge).
        won_lot.refresh_from_db()
        self.assertEqual(won_lot.auctiontos_winner, self.online_tos)

    def test_merge_review_when_the_two_records_are_flagged_as_duplicates(self):
        """Merging two records that point at each other via possible_duplicate must not 500.

        Regression: this is the normal path in from the duplicate review list, so both rows have
        possible_duplicate set to the other. Deleting the source SET_NULLs the kept row in the
        database but not the in-memory instance the review form saves, so saving the reviewed
        fields wrote the deleted id back and raised IntegrityError (1452).
        """
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(possible_duplicate=self.source_tos.pk)
        AuctionTOS.objects.filter(pk=self.source_tos.pk).update(possible_duplicate=self.online_tos.pk)
        url = reverse("auctiontosdelete", kwargs={"pk": self.source_tos.pk}) + "?action=merge"
        response = self.client.post(
            url,
            {
                "action": "merge",
                "step": "review",
                "target": str(self.online_tos.pk),
                "name": "Merged Winner",
                "email": "updated@example.com",
                "phone_number": "5553334444",
                "address": "222 Updated Ave",
                "pickup_location": self.location.pk,
            },
        )
        self.assertRedirects(response, reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.name, "Merged Winner")
        self.assertIsNone(self.online_tos.possible_duplicate_id)
        self.assertFalse(AuctionTOS.objects.filter(pk=self.source_tos.pk).exists())
