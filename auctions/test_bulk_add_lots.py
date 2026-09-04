"""The bulk add-lots table, its per-row save, and the CSV import view."""

import datetime
import io
from decimal import Decimal

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionHistory,
    AuctionTOS,
    Bid,
    Club,
    ClubMember,
    Invoice,
    Lot,
)
from auctions.tests import StandardTestCase


class BulkAddLotsAutoTests(StandardTestCase):
    """Tests for the new auto-save bulk add lots functionality"""

    def setUp(self):
        super().setUp()
        # Set up auction with lot limits
        self.in_person_auction.max_lots_per_user = 3
        self.in_person_auction.allow_additional_lots_as_donation = True
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=7)
        self.in_person_auction.save()

    def _get_bulk_add_input_tags(self, response, field_name):
        html = response.content.decode("utf-8")
        tags = []
        for input_chunk in html.split("<input")[1:]:
            tag = "<input" + input_chunk.split(">", 1)[0] + ">"
            if f'data-field="{field_name}"' in tag:
                tags.append(tag)
        return tags

    def test_bulk_add_lots_view_access(self):
        """Test that users can access bulk add lots page"""
        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_lots_admin_access(self):
        """Test that admins can access bulk add for other users"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(
            reverse(
                "bulk_add_lots_auto",
                kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_buyer.bidder_number},
            )
        )
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_lots_shows_required_custom_dropdown_indicator(self):
        self.in_person_auction.use_custom_dropdown_field = "required"
        self.in_person_auction.custom_dropdown_name = "Habitat"
        self.in_person_auction.save()
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="River")
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Pond")
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Habitat <span class="text-danger">*</span>')
        self.assertContains(response, 'data-field="custom_dropdown" required')

    def test_bulk_add_lots_auto_saves_text_fields_on_change(self):
        """Auto-save wiring should use change events and avoid input/keyup-style listeners."""
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("input.addEventListener('change'", html)
        self.assertNotIn("input.addEventListener('input'", html)
        self.assertNotIn("input.addEventListener('blur'", html)

    def test_bulk_add_lots_whole_dollar_inputs_use_integer_step(self):
        """Price inputs use whole-dollar client-side validation when auction requires whole-dollar bids"""
        self.in_person_auction.only_whole_dollar_bids = True
        self.in_person_auction.save()
        self.client.login(username="no_lots", password="testpassword")

        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )

        reserve_price_tags = self._get_bulk_add_input_tags(response, "reserve_price")
        buy_now_price_tags = self._get_bulk_add_input_tags(response, "buy_now_price")
        self.assertTrue(
            any('min="1"' in tag and 'step="1"' in tag and 'max="2000"' in tag for tag in reserve_price_tags)
        )
        self.assertTrue(
            any('min="1"' in tag and 'step="1"' in tag and 'max="1000"' in tag for tag in buy_now_price_tags)
        )

    def test_bulk_add_lots_decimal_inputs_use_cent_step(self):
        """Price inputs use cent-level client-side validation when auction allows decimal bids"""
        self.in_person_auction.only_whole_dollar_bids = False
        self.in_person_auction.save()
        self.client.login(username="no_lots", password="testpassword")

        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )

        reserve_price_tags = self._get_bulk_add_input_tags(response, "reserve_price")
        buy_now_price_tags = self._get_bulk_add_input_tags(response, "buy_now_price")
        self.assertTrue(
            any('min="0.01"' in tag and 'step="0.01"' in tag and 'max="2000"' in tag for tag in reserve_price_tags)
        )
        self.assertTrue(
            any('min="0.01"' in tag and 'step="0.01"' in tag and 'max="1000"' in tag for tag in buy_now_price_tags)
        )

    def test_bulk_add_lots_existing_row_inputs_match_whole_dollar_rules(self):
        """Existing lot rows use the same whole-dollar min/step rules as new lot rows."""
        self.in_person_auction.only_whole_dollar_bids = True
        self.in_person_auction.save()
        existing_lot = Lot.objects.create(
            lot_name="Existing bulk lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            reserve_price=Decimal("9.00"),
            buy_now_price=Decimal("11.00"),
            quantity=1,
        )
        self.client.login(username="no_lots", password="testpassword")

        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )

        reserve_price_tags = self._get_bulk_add_input_tags(response, "reserve_price")
        buy_now_price_tags = self._get_bulk_add_input_tags(response, "buy_now_price")
        existing_reserve_tag = next(
            (tag for tag in reserve_price_tags if f'value="{existing_lot.reserve_price}"' in tag), None
        )
        existing_buy_now_tag = next(
            (tag for tag in buy_now_price_tags if f'value="{existing_lot.buy_now_price}"' in tag), None
        )
        self.assertIsNotNone(existing_reserve_tag)
        self.assertIsNotNone(existing_buy_now_tag)
        self.assertIn('min="1"', existing_reserve_tag)
        self.assertIn('step="1"', existing_reserve_tag)
        self.assertIn('min="1"', existing_buy_now_tag)
        self.assertIn('step="1"', existing_buy_now_tag)

    def test_bulk_add_lots_non_admin_cannot_access_bidder_url(self):
        """Test that non-admin users cannot access the bidder_number URL"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to access bulk add for a specific bidder number
        response = self.client.get(
            reverse(
                "bulk_add_lots_auto",
                kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_buyer.bidder_number},
            ),
            follow=True,  # Follow redirects
        )

        # Should be redirected (not allowed)
        self.assertEqual(response.status_code, 200)
        # Check for error message
        messages = list(response.context["messages"])
        self.assertTrue(any("admin" in str(m).lower() for m in messages))

    def test_save_lot_ajax_anonymous_is_rejected_not_500(self):
        """An unauthenticated POST (e.g. expired session) should be rejected cleanly by
        DRF's IsAuthenticated, not crash with AttributeError on AnonymousUser.email"""
        self.client.logout()
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot"}',
            content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_save_lot_ajax_security(self):
        """Test that non-admin users cannot add lots for others"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to add lot for another user
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("admin", data["error"].lower())

    def test_save_lot_ajax_admin_can_add_for_others(self):
        """Test that admins can add lots for other users"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Add lot for another user
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIsNotNone(data["lot_id"])

        # Verify lot was created for correct user
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)

    def test_save_lot_ajax_user_can_add_for_themselves(self):
        """Test that regular users can add lots for themselves without bidder_number"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Add lot for themselves (no bidder_number)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "My Own Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Failed to add lot for self: {data.get('error', 'Unknown error')}")
        self.assertIsNotNone(data["lot_id"])

        # Verify lot was created for the correct user (in_person_buyer)
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)

    def test_lot_limit_enforcement(self):
        """Test that lot limits are enforced for non-admin users"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Create 3 lots (the limit)
        for i in range(3):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            data = response.json()
            self.assertTrue(data["success"])

        # Try to add 4th lot (should fail)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot 4", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("maximum", data["errors"]["general"].lower())

    def test_donation_lot_beyond_limit(self):
        """Test that donation lots can be added beyond limit when allowed"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Create 3 lots (the limit)
        for i in range(3):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            self.assertTrue(response.json()["success"])

        # Try to add 4th lot as donation (should succeed)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Donation Lot", "reserve_price": 5, "donation": true}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

    def test_admin_bypass_lot_limit(self):
        """Test that admins can bypass lot limits"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Create 4 lots (beyond limit)
        for i in range(4):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            data = response.json()
            self.assertTrue(data["success"])
            if i >= 3:  # Beyond limit
                self.assertTrue(data.get("admin_bypassed_lot_limit"))

    def test_locked_lot_cannot_be_edited(self):
        """Test that lots cannot be edited after submission deadline"""
        # Create a lot for tosC (user_with_no_lots)
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.tosC, reserve_price=5
        )

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Login as regular user (owner, not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        # Check that error message relates to lot submission deadline
        error_msg = data["error"].lower()
        self.assertTrue("cannot be edited" in error_msg or "submission" in error_msg)

    def test_custom_fields_saved(self):
        """Test that custom fields are properly saved"""
        # Set up custom fields
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Species"
        self.in_person_auction.use_custom_checkbox_field = True
        self.in_person_auction.custom_checkbox_name = "Wild Caught"
        self.in_person_auction.use_quantity_field = True
        self.in_person_auction.use_donation_field = True
        self.in_person_auction.use_i_bred_this_fish_field = True
        self.in_person_auction.save()

        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")

        # Create lot with all custom fields
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "custom_field_1": "Betta", "custom_checkbox": true, "quantity": 2, "donation": true, "i_bred_this_fish": true}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify all fields saved
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.custom_field_1, "Betta")
        self.assertTrue(lot.custom_checkbox)
        self.assertEqual(lot.quantity, 2)
        self.assertTrue(lot.donation)
        self.assertTrue(lot.i_bred_this_fish)

    def test_custom_dropdown_saved(self):
        """Custom dropdown values should save through the auto-save endpoint"""
        self.in_person_auction.use_custom_dropdown_field = "allow"
        self.in_person_auction.custom_dropdown_name = "Habitat"
        self.in_person_auction.save()
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Red")
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Blue")

        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Dropdown Lot", "reserve_price": 5, "custom_dropdown": "Blue"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.custom_dropdown, "Blue")

    def test_custom_dropdown_rejects_invalid_option(self):
        """Auto-save rejects dropdown values not configured in the auction."""
        self.in_person_auction.use_custom_dropdown_field = "allow"
        self.in_person_auction.custom_dropdown_name = "Habitat"
        self.in_person_auction.save()
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Red")
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Blue")

        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Dropdown Lot", "reserve_price": 5, "custom_dropdown": "Green"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("custom_dropdown", data["errors"])

    def test_custom_dropdown_required_validation(self):
        self.in_person_auction.use_custom_dropdown_field = "required"
        self.in_person_auction.custom_dropdown_name = "Habitat"
        self.in_person_auction.save()
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Red")
        AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.admin_user, value="Blue")

        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Dropdown Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("custom_dropdown", data["errors"])

    def test_required_field_validation(self):
        """Test that required fields are validated"""
        # Set up required custom field
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Species"
        self.in_person_auction.save()

        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")

        # Try to create lot without required field
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("custom_field_1", data["errors"])

    def test_lot_update_existing(self):
        """Test that existing lots can be updated"""
        # Create a lot
        lot = Lot.objects.create(
            lot_name="Original Name",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            reserve_price=5,
        )

        # Login as user (owner)
        self.client.login(username="my_lot", password="testpassword")

        # Update the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify update
        lot.refresh_from_db()
        self.assertEqual(lot.lot_name, "Updated Name")
        self.assertEqual(lot.reserve_price, 10)

    def test_lot_not_found_for_different_user(self):
        """Test that users cannot edit other users' lots"""
        # Create a lot for user_with_no_lots
        lot = Lot.objects.create(
            lot_name="Other User's Lot", auction=self.in_person_auction, auctiontos_seller=self.tosC, reserve_price=5
        )

        # Login as different user
        self.client.login(username="my_lot", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Hacked Name", "reserve_price": 100}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("not found", data["error"].lower())

    def test_lot_locked_when_has_bids(self):
        """Test that lots with bids cannot be edited"""
        # Create a lot - use in_person_buyer TOS for in_person_auction
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_buyer, reserve_price=5
        )

        # Add a bid to the lot
        Bid.objects.create(lot_number=lot, user=self.userB, bid_time=timezone.now(), amount=10)

        # Login as lot owner
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        error_msg = data["error"].lower()
        self.assertTrue("bids" in error_msg or "cannot be edited" in error_msg)

    def test_lot_locked_when_sold(self):
        """Test that sold lots cannot be edited"""
        # Create a sold lot - use in_person_buyer TOS for in_person_auction
        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            reserve_price=5,
            auctiontos_winner=self.tosB,  # Has been sold
        )

        # Login as lot owner
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        error_msg = data["error"].lower()
        self.assertTrue("sold" in error_msg or "cannot be edited" in error_msg)

    def test_admin_can_edit_locked_lot(self):
        """Test that admins can edit locked lots"""
        # Create a lot and end lot submission
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_buyer, reserve_price=5
        )

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to edit
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Admin Updated", "reserve_price": 10, "bidder_number": "{self.in_person_buyer.bidder_number}"}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify update
        lot.refresh_from_db()
        self.assertEqual(lot.lot_name, "Admin Updated")

    def test_can_be_edited_property(self):
        """Test the can_be_edited property of Lot model"""
        # Create a basic lot
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_tos, reserve_price=5
        )

        # With open submission, lot should be editable
        self.assertTrue(lot.can_be_edited)
        self.assertFalse(lot.cannot_be_edited_reason)

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Now lot should not be editable
        lot.refresh_from_db()
        self.assertFalse(lot.can_be_edited)
        self.assertEqual(lot.cannot_be_edited_reason, "Lot submission is over for this auction")

    def test_cannot_change_reason_with_high_bidder(self):
        """Test cannot_change_reason when lot has a high bidder"""
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_tos, reserve_price=5
        )

        # Add a bid to the lot
        Bid.objects.create(lot_number=lot, user=self.userB, bid_time=timezone.now(), amount=10)

        self.assertEqual(lot.cannot_change_reason, "There are already bids placed on this lot")
        self.assertFalse(lot.can_be_edited)

    def test_cannot_change_reason_with_winner(self):
        """Test cannot_change_reason when lot has a winner"""
        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            reserve_price=5,
            auctiontos_winner=self.tosB,
        )

        self.assertEqual(lot.cannot_change_reason, "This lot has sold")
        self.assertFalse(lot.can_be_edited)

    def test_admin_can_add_lots_for_user_with_selling_not_allowed(self):
        """Test that admins can add lots for users whose selling_allowed is False, with a warning flag"""
        # Set in_person_buyer's selling_allowed to False
        self.in_person_buyer.selling_allowed = False
        self.in_person_buyer.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to add lot for user with selling_allowed=False
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Admin should be able to add lots for user: {data.get('error', '')}")
        self.assertIsNotNone(data["lot_id"])
        # Verify that admin_bypassed_selling_allowed flag is set
        self.assertTrue(data.get("admin_bypassed_selling_allowed", False))

    def test_non_admin_cannot_add_lots_when_selling_not_allowed(self):
        """Test that non-admin users cannot add lots when their selling_allowed is False"""
        # Set in_person_buyer's selling_allowed to False
        self.in_person_buyer.selling_allowed = False
        self.in_person_buyer.save()

        # Login as non-admin user (in_person_buyer)
        self.client.login(username="no_lots", password="testpassword")

        # Try to add lot for themselves (should fail)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("permission", data["error"].lower())

    def test_admin_can_add_lots_for_themselves_when_their_selling_not_allowed(self):
        """Test that admins can bypass their own selling_allowed restriction"""
        # Set admin's selling_allowed to False
        self.admin_in_person_tos.selling_allowed = False
        self.admin_in_person_tos.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to add lot for themselves (no bidder_number)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Admin Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Admin should bypass their own selling_allowed: {data.get('error', '')}")
        self.assertIsNotNone(data["lot_id"])
        # Verify that admin_bypassed_selling_allowed flag is set
        self.assertTrue(data.get("admin_bypassed_selling_allowed", False))

        # Verify lot was created for admin
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.admin_in_person_tos)

    def test_decimal_minimum_bid_accepted(self):
        """Test that decimal minimum bids (e.g. 2.50) are accepted when auction allows them"""
        self.in_person_auction.only_whole_dollar_bids = False
        self.in_person_auction.save()

        self.client.login(username="no_lots", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Decimal Bid Lot", "reserve_price": "2.50"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Decimal reserve_price should be accepted: {data}")
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.reserve_price, Decimal("2.50"))

    def test_decimal_minimum_bid_rejected_when_whole_dollar_required(self):
        """Test that decimal minimum bids are rejected when auction requires whole dollar amounts"""
        self.in_person_auction.only_whole_dollar_bids = True
        self.in_person_auction.save()

        self.client.login(username="no_lots", password="testpassword")
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Decimal Bid Lot", "reserve_price": "2.50"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("reserve_price", data.get("errors", {}))


class UpdateAuctionStatsCommandTestCase(StandardTestCase):
    """Test the update_auction_stats management command"""

    def test_command_processes_single_auction(self):
        """Test that the command processes only one auction per run"""
        import datetime

        from django.utils import timezone

        # Set up multiple auctions with due stats updates
        now = timezone.now()

        # Ensure setUp auctions don't interfere by setting their next_update_due to far future
        self.online_auction.next_update_due = now + datetime.timedelta(days=365)
        self.online_auction.save()
        self.in_person_auction.next_update_due = now + datetime.timedelta(days=365)
        self.in_person_auction.save()

        # Create three auctions with different next_update_due times
        auction1 = Auction.objects.create(
            created_by=self.user,
            title="Auction 1 - oldest",
            is_online=True,
            date_start=now - datetime.timedelta(days=5),
            date_end=now + datetime.timedelta(days=2),
        )
        auction1.next_update_due = now - datetime.timedelta(hours=5)  # Most overdue
        auction1.save()

        auction2 = Auction.objects.create(
            created_by=self.user,
            title="Auction 2 - middle",
            is_online=True,
            date_start=now - datetime.timedelta(days=4),
            date_end=now + datetime.timedelta(days=2),
        )
        auction2.next_update_due = now - datetime.timedelta(hours=3)  # Second most overdue
        auction2.save()

        auction3 = Auction.objects.create(
            created_by=self.user,
            title="Auction 3 - newest",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        auction3.next_update_due = now - datetime.timedelta(hours=1)  # Least overdue
        auction3.save()

        # Store the original next_update_due times
        original_due_1 = auction1.next_update_due
        original_due_2 = auction2.next_update_due
        original_due_3 = auction3.next_update_due

        # Run the command once (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        auction1.refresh_from_db()
        auction2.refresh_from_db()
        auction3.refresh_from_db()

        # The most overdue auction (auction1) should have been updated
        self.assertIsNotNone(auction1.last_stats_update)
        self.assertNotEqual(auction1.next_update_due, original_due_1)
        # The new next_update_due should be in the future
        self.assertGreater(auction1.next_update_due, now)

        # The other two auctions should NOT have been updated
        self.assertEqual(auction2.next_update_due, original_due_2)
        self.assertEqual(auction3.next_update_due, original_due_3)

    def test_command_orders_by_next_update_due(self):
        """Test that the command processes the most overdue auction first"""
        import datetime

        from django.utils import timezone

        now = timezone.now()

        # Ensure setUp auctions don't interfere by setting their next_update_due to far future
        self.online_auction.next_update_due = now + datetime.timedelta(days=365)
        self.online_auction.save()
        self.in_person_auction.next_update_due = now + datetime.timedelta(days=365)
        self.in_person_auction.save()

        # Create two auctions with different next_update_due times
        newer_auction = Auction.objects.create(
            created_by=self.user,
            title="Newer auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        newer_auction.next_update_due = now - datetime.timedelta(hours=1)  # Less overdue
        newer_auction.save()

        older_auction = Auction.objects.create(
            created_by=self.user,
            title="Older auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=5),
            date_end=now + datetime.timedelta(days=2),
        )
        older_auction.next_update_due = now - datetime.timedelta(hours=5)  # More overdue
        older_auction.save()

        # Run the command (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        newer_auction.refresh_from_db()
        older_auction.refresh_from_db()

        # The older (more overdue) auction should have been processed
        self.assertIsNotNone(older_auction.last_stats_update)
        self.assertGreater(older_auction.next_update_due, now)

        # The newer auction should not have been processed yet
        self.assertEqual(newer_auction.next_update_due, now - datetime.timedelta(hours=1))
        self.assertIsNone(newer_auction.last_stats_update)

    def test_command_handles_no_due_auctions(self):
        """Test that the command handles the case when no auctions are due"""
        import datetime

        from django.utils import timezone

        now = timezone.now()

        # Create an auction with next_update_due in the future
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        future_auction.next_update_due = now + datetime.timedelta(hours=5)
        future_auction.save()

        # Run the command - should not raise any errors (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        future_auction.refresh_from_db()

        # The auction should not have been processed
        self.assertEqual(future_auction.next_update_due, now + datetime.timedelta(hours=5))
        self.assertIsNone(future_auction.last_stats_update)


class LotsByUserViewTest(StandardTestCase):
    """Test for the LotsByUser view to ensure it handles missing 'user' parameter correctly"""

    def test_lots_by_user_missing_user_parameter(self):
        """Test that the view doesn't crash when 'user' parameter is missing"""
        # Access the URL without user parameter, only with auction parameter
        url = reverse("user_lots") + f"?auction={self.online_auction.slug}"
        response = self.client.get(url)

        # Should return 200, not crash with MultiValueDictKeyError
        self.assertEqual(response.status_code, 200)

        # Context should have user set to None
        self.assertIsNone(response.context["user"])

    def test_lots_by_user_with_valid_user_parameter(self):
        """Test that the view works correctly with a valid user parameter"""
        url = reverse("user_lots") + f"?user={self.user.username}"
        response = self.client.get(url)

        # Should return 200
        self.assertEqual(response.status_code, 200)

        # Context should have the correct user
        self.assertEqual(response.context["user"], self.user)
        self.assertEqual(response.context["lot_view_type"], "user")

    def test_lots_by_user_with_invalid_user_parameter(self):
        """Test that the view handles non-existent username gracefully"""
        url = reverse("user_lots") + "?user=nonexistent_user"
        response = self.client.get(url)

        # Should return 200, not crash
        self.assertEqual(response.status_code, 200)

        # Context should have user set to None
        self.assertIsNone(response.context["user"])


class ImportLotsFromCSVViewTests(StandardTestCase):
    """Test CSV lot import functionality"""

    def test_import_lots_csv_anonymous(self):
        """Anonymous users cannot import lots from CSV"""
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_import_lots_csv_non_admin(self):
        """Non-admin users cannot import lots from CSV"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        assert response.status_code in [302, 403]

    def test_import_lots_csv_admin_no_file(self):
        """Admin posting without CSV file gets error"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        # Should redirect back to the lot list with an error message
        assert response.status_code in (200, 302)

    def test_import_lots_csv_create_new_lot(self):
        """CSV import creates a new lot for existing user"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Set name and email on TOS so we can find it
        self.online_tos.name = "Test User"
        self.online_tos.email = "testuser@example.com"
        self.online_tos.save()

        # Create CSV content
        csv_content = (
            "Name,Email,Lot Name,Quantity,Reserve Price\n"
            f"{self.online_tos.name},{self.online_tos.email},Test Lot from CSV,5,10\n"
        )

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully
        assert response.status_code == 200

        # Check that lot was created
        new_lot = Lot.objects.filter(lot_name="Test Lot from CSV", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.quantity == 5
        assert new_lot.reserve_price == 10
        assert new_lot.auctiontos_seller == self.online_tos

    def test_import_lots_csv_update_existing_lot(self):
        """CSV import updates existing lot by lot number"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Use existing lot
        lot_number = self.lot.lot_number_int

        # Create CSV content to update the lot
        csv_content = f"Lot Number,Lot Name,Quantity,Reserve Price\n{lot_number},Updated Lot Name,3,15\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully
        assert response.status_code == 200

        # Check that lot was updated
        self.lot.refresh_from_db()
        assert self.lot.lot_name == "Updated Lot Name"
        assert self.lot.quantity == 3
        assert self.lot.reserve_price == 15

    def test_import_lots_csv_create_new_user_and_lot(self):
        """CSV import creates both user and lot"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV content with new user
        csv_content = "Name,Email,Lot Name,Quantity\nNew User,newuser@example.com,New User Lot,2\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully
        assert response.status_code == 200

        # Check that user was created
        new_tos = AuctionTOS.objects.filter(email="newuser@example.com", auction=self.online_auction).first()
        assert new_tos is not None
        assert new_tos.name == "New User"

        # Check that lot was created
        new_lot = Lot.objects.filter(lot_name="New User Lot", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.auctiontos_seller == new_tos

    def test_import_lots_csv_new_seller_gets_a_club_member(self):
        """In a club-managed auction, an imported seller needs a ClubMember like any participant.

        The club owns the bidder number there, so a seller row with no member behind it carries a
        number the club has never heard of."""
        club = Club.objects.create(name="Import Club")
        ClubMember.objects.create(club=club, user=self.admin_user, name="Admin", permission_admin=True)
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.in_person_auction.slug})
        csv_content = "Name,Email,Lot Name,Quantity\nImported Seller,imported@example.com,Bag of guppies,2\n"
        csv_file = io.BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 200)

        member = ClubMember.objects.get(club=club, email="imported@example.com")
        self.assertTrue(member.bidder_number)
        sellers = AuctionTOS.objects.filter(auction=self.in_person_auction, email="imported@example.com")
        self.assertEqual(sellers.count(), 1)  # the member's shadow row was adopted, not duplicated
        seller = sellers.first()
        self.assertEqual(seller.clubmember, member)
        self.assertEqual(seller.bidder_number, member.bidder_number)
        lot = Lot.objects.filter(lot_name="Bag of guppies", auction=self.in_person_auction).first()
        self.assertIsNotNone(lot)
        self.assertEqual(lot.auctiontos_seller, seller)

    def test_import_lots_csv_new_seller_without_club_management_has_no_member(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.in_person_auction.slug})
        csv_content = "Name,Email,Lot Name,Quantity\nPlain Seller,plain@example.com,Bag of snails,1\n"
        csv_file = io.BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        self.run_csv_import(url, csv_file)
        self.assertTrue(AuctionTOS.objects.filter(auction=self.in_person_auction, email="plain@example.com").exists())
        self.assertFalse(ClubMember.objects.filter(email="plain@example.com").exists())

    def test_import_lots_csv_boolean_fields(self):
        """CSV import handles boolean fields correctly"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV with boolean fields
        csv_content = (
            "Name,Email,Lot Name,Breeder Points,Donation\n"
            f"{self.online_tos.name},{self.online_tos.email},Bred Fish,yes,true\n"
        )

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully
        assert response.status_code == 200

        # Check boolean fields
        new_lot = Lot.objects.filter(lot_name="Bred Fish", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.i_bred_this_fish is True
        assert new_lot.donation is True

    def test_import_lots_csv_custom_dropdown(self):
        self.online_auction.use_custom_dropdown_field = "allow"
        self.online_auction.custom_dropdown_name = "Habitat"
        self.online_auction.save()
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="River")
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="Pond")
        self.online_tos.name = "Test User"
        self.online_tos.email = "testuser@example.com"
        self.online_tos.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        csv_content = (
            f"Name,Email,Lot Name,Habitat\n{self.online_tos.name},{self.online_tos.email},Dropdown CSV Lot,River\n"
        )
        csv_file = io.BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        response = self.run_csv_import(url, csv_file)
        self.assertEqual(response.status_code, 200)
        new_lot = Lot.objects.filter(lot_name="Dropdown CSV Lot", auction=self.online_auction).first()
        self.assertIsNotNone(new_lot)
        self.assertEqual(new_lot.custom_dropdown, "River")

    def test_import_lots_csv_missing_info(self):
        """CSV import skips rows with missing required information"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV with incomplete data
        csv_content = "Name,Email\nMissing Lot Name,missing@example.com\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully but skip the row
        assert response.status_code == 200

        # Check that no lot was created
        lots = Lot.objects.filter(auctiontos_seller__email="missing@example.com", auction=self.online_auction)
        assert lots.count() == 0

    def test_import_lots_csv_idempotent(self):
        """CSV import is idempotent - repeated uploads should update, not duplicate"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Use existing lot with lot number
        lot_number = self.lot.lot_number_int

        # Create CSV content
        csv_content = f"Lot Number,Lot Name,Quantity\n{lot_number},Idempotent Lot,7\n"

        from io import BytesIO

        # Upload once
        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        response = self.run_csv_import(url, csv_file)
        assert response.status_code == 200

        # Upload again
        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        response = self.run_csv_import(url, csv_file)
        assert response.status_code == 200

        # Check that lot was updated, not duplicated
        lots = Lot.objects.filter(lot_name="Idempotent Lot", auction=self.online_auction)
        assert lots.count() == 1
        assert lots.first().quantity == 7

    def test_import_lots_csv_closed_invoice(self):
        """CSV import skips creating lots when invoice is not open"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Set name and email on TOS so we can find it
        self.online_tos.name = "Closed Invoice User"
        self.online_tos.email = "closedinvoice@example.com"
        self.online_tos.save()

        # Close the invoice
        invoice = Invoice.objects.filter(auctiontos_user=self.online_tos, auction=self.online_auction).first()
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=self.online_tos, auction=self.online_auction)
        invoice.status = "PAID"
        invoice.save()

        # Create CSV content
        csv_content = f"Name,Email,Lot Name\n{self.online_tos.name},{self.online_tos.email},Should Not Create\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.run_csv_import(url, csv_file)

        # Should redirect with warning
        assert response.status_code == 200

        # Check that lot was not created
        new_lot = Lot.objects.filter(lot_name="Should Not Create", auction=self.online_auction).first()
        assert new_lot is None

    def test_import_lots_csv_records_filename_in_history(self):
        """CSV import records the filename in auction history"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Set name and email on TOS so we can find it
        self.online_tos.name = "Test User"
        self.online_tos.email = "testuser@example.com"
        self.online_tos.save()

        # Create CSV content
        csv_content = (
            "Name,Email,Lot Name,Quantity,Reserve Price\n"
            f"{self.online_tos.name},{self.online_tos.email},History Test Lot,3,15\n"
        )

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "lots_import.csv"

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="LOTS").count()

        response = self.run_csv_import(url, csv_file)

        # Should redirect successfully
        assert response.status_code == 200

        # Check that lot was created
        new_lot = Lot.objects.filter(lot_name="History Test Lot", auction=self.online_auction).first()
        assert new_lot is not None

        # Check that history was created with filename
        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="LOTS").count()
        assert new_count == initial_count + 1

        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="LOTS").latest("timestamp")
        assert "lots_import.csv" in history.action
        assert "1 lots created" in history.action

    def _import_lot_csv(self, csv_content):
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        csv_file = io.BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        return self.run_csv_import(url, csv_file)

    def test_update_without_a_donation_column_keeps_the_flags(self):
        """These feed the invoice, so an unrelated column update must not silently clear them."""
        self.lot.donation = True
        self.lot.i_bred_this_fish = True
        self.lot.custom_checkbox = True
        self.lot.save()
        self._import_lot_csv(f"Lot Number,Lot Name\n{self.lot.lot_number_int},Renamed Lot\n")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.lot_name, "Renamed Lot")
        self.assertTrue(self.lot.donation)
        self.assertTrue(self.lot.i_bred_this_fish)
        self.assertTrue(self.lot.custom_checkbox)

    def test_update_with_a_blank_donation_cell_keeps_the_flag(self):
        self.lot.donation = True
        self.lot.save()
        self._import_lot_csv(f"Lot Number,Lot Name,Donation\n{self.lot.lot_number_int},Renamed Lot,\n")
        self.lot.refresh_from_db()
        self.assertTrue(self.lot.donation)

    def test_update_with_an_explicit_no_clears_the_flag(self):
        self.lot.donation = True
        self.lot.save()
        self._import_lot_csv(f"Lot Number,Lot Name,Donation\n{self.lot.lot_number_int},Renamed Lot,no\n")
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.donation)

    def test_new_lot_defaults_to_off_when_the_file_says_nothing(self):
        self._import_lot_csv("Name,Email,Lot Name\nBlank Flags,blankflags@example.com,Blank Flag Lot\n")
        new_lot = Lot.objects.filter(lot_name="Blank Flag Lot", auction=self.online_auction).first()
        self.assertIsNotNone(new_lot)
        self.assertFalse(new_lot.donation)
        self.assertFalse(new_lot.i_bred_this_fish)
