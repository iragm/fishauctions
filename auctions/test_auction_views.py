"""Auction pages an admin edits: permissions, the edit form, custom fields and cloning."""

import datetime
from decimal import Decimal

from django import forms
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    AuctionEditForm,
    quick_add_lot_form_class,
)
from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionHistory,
    Club,
    PayPalSeller,
)
from auctions.tests import StandardTestCase


class AuctionViewPermissionTests(StandardTestCase):
    """Test view permissions for different user types"""

    def test_auction_view_anonymous_user(self):
        """Test that anonymous users can view auction page"""
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_logged_in_not_joined(self):
        """Test logged in user who hasn't joined the auction"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        # Should see option to join
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_logged_in_joined(self):
        """Test logged in user who has joined the auction"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_admin_user(self):
        """Test admin user viewing auction"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_bulk_add_button_points_to_auto_url(self):
        """Test that the bulk add lots button uses the auto bulk add URL"""
        # Set up in-person auction to allow bulk adding
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.save()

        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(self.in_person_auction.get_absolute_url())
        assert response.status_code == 200

        # Check that the response contains the auto bulk add URL
        auto_bulk_add_url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.assertContains(response, auto_bulk_add_url)

        # Make sure it doesn't contain the old bulk add URL
        old_bulk_add_url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.assertNotContains(response, old_bulk_add_url)


class AuctionEditViewTests(StandardTestCase):
    """Test auction edit view with different user types"""

    def test_auction_edit_anonymous_user(self):
        """Anonymous users should not be able to edit"""
        response = self.client.get(self.online_auction.get_edit_url())
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_edit_non_admin(self):
        """Non-admin users should not be able to edit"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        # Should be denied - can be either 302 (redirect to error/login page) or 403 (forbidden)
        # depending on permission middleware configuration
        assert response.status_code in [302, 403]

    def test_auction_edit_admin_user(self):
        """Admin users should be able to edit"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        assert response.status_code == 200

    def test_auction_edit_creator(self):
        """Auction creator should be able to edit"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        assert response.status_code == 200

    def test_auction_edit_preserves_use_categories(self):
        """Saving AuctionEditForm should not reset use_categories to False"""
        self.online_auction.use_categories = True
        self.online_auction.save()
        self.client.login(username=self.user.username, password="testpassword")
        form_data = {
            "summernote_description": self.online_auction.summernote_description or "",
            "lot_entry_fee": str(self.online_auction.lot_entry_fee or "0"),
            "unsold_lot_fee": str(self.online_auction.unsold_lot_fee or "0"),
            "winning_bid_percent_to_club": str(self.online_auction.winning_bid_percent_to_club or "0"),
            "winning_bid_percent_to_club_for_club_members": str(
                self.online_auction.winning_bid_percent_to_club_for_club_members or "0"
            ),
            "lot_entry_fee_for_club_members": str(self.online_auction.lot_entry_fee_for_club_members or "0"),
            "pre_register_lot_discount_percent": str(self.online_auction.pre_register_lot_discount_percent or "0"),
            "alternate_split_mode": self.online_auction.alternate_split_mode,
            "alternative_split_label": self.online_auction.alternative_split_label or "",
            "tax": str(self.online_auction.tax or "0"),
            "online_bidding": self.online_auction.online_bidding,
            "date_start": self.online_auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": self.online_auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_rounding": str(self.online_auction.invoice_rounding),
            "only_whole_dollar_bids": "",
            "minimum_bid": str(self.online_auction.minimum_bid),
            # use_categories intentionally omitted — it should not be touched by AuctionEditForm
        }
        response = self.client.post(self.online_auction.get_edit_url(), data=form_data, follow=False)
        self.assertEqual(
            response.status_code,
            302,
            f"Form was not saved: {response.context['form'].errors if response.context else ''}",
        )
        self.online_auction.refresh_from_db()
        self.assertTrue(
            self.online_auction.use_categories,
            "use_categories was reset to False by AuctionEditForm even though it was not included in the form",
        )

    def test_auction_edit_preserves_sealed_bid(self):
        """Saving AuctionEditForm should not reset sealed_bid to False"""
        self.online_auction.sealed_bid = True
        self.online_auction.save()
        self.client.login(username=self.user.username, password="testpassword")
        form_data = {
            "summernote_description": self.online_auction.summernote_description or "",
            "lot_entry_fee": str(self.online_auction.lot_entry_fee or "0"),
            "unsold_lot_fee": str(self.online_auction.unsold_lot_fee or "0"),
            "winning_bid_percent_to_club": str(self.online_auction.winning_bid_percent_to_club or "0"),
            "winning_bid_percent_to_club_for_club_members": str(
                self.online_auction.winning_bid_percent_to_club_for_club_members or "0"
            ),
            "lot_entry_fee_for_club_members": str(self.online_auction.lot_entry_fee_for_club_members or "0"),
            "pre_register_lot_discount_percent": str(self.online_auction.pre_register_lot_discount_percent or "0"),
            "alternate_split_mode": self.online_auction.alternate_split_mode,
            "alternative_split_label": self.online_auction.alternative_split_label or "",
            "tax": str(self.online_auction.tax or "0"),
            "online_bidding": self.online_auction.online_bidding,
            "date_start": self.online_auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": self.online_auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_rounding": str(self.online_auction.invoice_rounding),
            "only_whole_dollar_bids": "",
            "minimum_bid": str(self.online_auction.minimum_bid),
            # sealed_bid intentionally omitted — it should not be touched by AuctionEditForm
        }
        response = self.client.post(self.online_auction.get_edit_url(), data=form_data, follow=False)
        self.assertEqual(
            response.status_code,
            302,
            f"Form was not saved: {response.context['form'].errors if response.context else ''}",
        )
        self.online_auction.refresh_from_db()
        self.assertTrue(
            self.online_auction.sealed_bid,
            "sealed_bid was reset to False by AuctionEditForm even though it was not included in the form",
        )

    def test_auction_edit_preserves_advanced_lot_adding(self):
        """Saving AuctionEditForm should not reset advanced_lot_adding to False"""
        self.in_person_auction.advanced_lot_adding = True
        self.in_person_auction.save()
        self.client.login(username=self.user.username, password="testpassword")
        form_data = {
            "summernote_description": self.in_person_auction.summernote_description or "",
            "lot_entry_fee": str(self.in_person_auction.lot_entry_fee or "0"),
            "unsold_lot_fee": str(self.in_person_auction.unsold_lot_fee or "0"),
            "winning_bid_percent_to_club": str(self.in_person_auction.winning_bid_percent_to_club or "0"),
            "winning_bid_percent_to_club_for_club_members": str(
                self.in_person_auction.winning_bid_percent_to_club_for_club_members or "0"
            ),
            "lot_entry_fee_for_club_members": str(self.in_person_auction.lot_entry_fee_for_club_members or "0"),
            "pre_register_lot_discount_percent": str(self.in_person_auction.pre_register_lot_discount_percent or "0"),
            "alternate_split_mode": self.in_person_auction.alternate_split_mode,
            "alternative_split_label": self.in_person_auction.alternative_split_label or "",
            "tax": str(self.in_person_auction.tax or "0"),
            "online_bidding": self.in_person_auction.online_bidding,
            "date_start": self.in_person_auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            # date_end is omitted intentionally: for offline auctions the form sets date_end to HiddenInput
            # and the field is nullable, so it is not required in POST data.
            "invoice_rounding": str(self.in_person_auction.invoice_rounding),
            "only_whole_dollar_bids": "",
            "minimum_bid": str(self.in_person_auction.minimum_bid),
            "use_seller_dash_lot_numbering": self.in_person_auction.use_seller_dash_lot_numbering,
            # advanced_lot_adding intentionally omitted — it should not be touched by AuctionEditForm
        }
        response = self.client.post(self.in_person_auction.get_edit_url(), data=form_data, follow=False)
        self.assertEqual(
            response.status_code,
            302,
            f"Form was not saved: {response.context['form'].errors if response.context else ''}",
        )
        self.in_person_auction.refresh_from_db()
        self.assertTrue(
            self.in_person_auction.advanced_lot_adding,
            "advanced_lot_adding was reset to False by AuctionEditForm even though it was not included in the form",
        )


class AuctionCustomFieldsViewTests(StandardTestCase):
    """Test auction edit custom field behavior"""

    def _custom_fields_data(self, use_custom_dropdown=False):
        data = {
            "custom_field_1": self.online_auction.custom_field_1,
            "custom_field_1_name": self.online_auction.custom_field_1_name or "Notes",
            "custom_checkbox_name": self.online_auction.custom_checkbox_name or "",
            "custom_dropdown_name": self.online_auction.custom_dropdown_name or "My dropdown",
            "use_custom_dropdown_field": self.online_auction.use_custom_dropdown_field,
            "reserve_price": self.online_auction.reserve_price,
            "buy_now": self.online_auction.buy_now,
        }
        true_boolean_fields = [
            "allow_bulk_adding_lots",
            "use_categories",
            "use_quantity_field",
            "use_donation_field",
            "use_i_bred_this_fish_field",
            "use_reference_link",
            "use_description",
            "use_custom_checkbox_field",
        ]
        for field_name in true_boolean_fields:
            if getattr(self.online_auction, field_name):
                data[field_name] = "on"
        if use_custom_dropdown:
            data["use_custom_dropdown_field"] = "allow"
        return data

    def test_custom_dropdown_requires_two_options(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("edit_auction_custom_fields", kwargs={"slug": self.online_auction.slug}),
            data=self._custom_fields_data(use_custom_dropdown=True),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.online_auction.refresh_from_db()
        self.assertEqual(self.online_auction.use_custom_dropdown_field, "disable")
        self.assertContains(response, "requires a name and at least two options")

    def test_custom_field_labels_use_custom_text_wording(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("edit_auction_custom_fields", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use custom text field")
        self.assertContains(response, "Custom text field name")

    def test_custom_fields_form_shows_seller_price_fields_before_custom_dropdown(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("edit_auction_custom_fields", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode(response.charset or "utf-8")
        self.assertLess(html.index("Allow bulk adding lots"), html.index("Seller set minimum bid"))
        self.assertLess(html.index("Seller set minimum bid"), html.index("Buy now"))
        self.assertLess(html.index("Buy now"), html.index("Use custom dropdown field"))

    def test_custom_dropdown_model_creates_history(self):
        option = AuctionDropdown.objects.create(auction=self.online_auction, user=self.user, value="Red")
        option.value = "Blue"
        option.user = self.user
        option.save()
        option.delete()
        actions = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="RULES").values_list(
            "action", flat=True
        )
        self.assertTrue(any("Added custom dropdown option 'Red'" in action for action in actions))
        self.assertTrue(any("Renamed custom dropdown option 'Red' to 'Blue'" in action for action in actions))
        self.assertTrue(any("Removed custom dropdown option 'Blue'" in action for action in actions))

    def test_custom_dropdown_save_keeps_oldest_duplicate(self):
        oldest = AuctionDropdown.objects.create(auction=self.online_auction, user=self.user, value="River")
        newer = AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="river")
        options = AuctionDropdown.objects.filter(auction=self.online_auction, value__iexact="river")
        self.assertEqual(options.count(), 1)
        self.assertEqual(options.first().pk, oldest.pk)
        self.assertEqual(options.first().user, self.user)
        newer.refresh_from_db()
        self.assertEqual(newer.pk, oldest.pk)

    def test_label_print_fields_include_custom_dropdown(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("auction_label_config", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Custom dropdown")

    def test_custom_dropdown_options_api_admin_can_create(self):
        """Regression: the API view must set self.auction so is_auction_admin works (used to 500)."""
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(
            reverse("auction_custom_dropdown_options", kwargs={"slug": self.online_auction.slug}),
            data={"action": "create", "value": "River"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(AuctionDropdown.objects.filter(auction=self.online_auction, value="River").exists())

    def test_custom_dropdown_options_api_non_admin_denied(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(
            reverse("auction_custom_dropdown_options", kwargs={"slug": self.online_auction.slug}),
            data={"action": "create", "value": "River"},
        )
        self.assertIn(response.status_code, [302, 403])
        self.assertFalse(AuctionDropdown.objects.filter(auction=self.online_auction, value="River").exists())


class AuctionCloneCustomFieldsTests(StandardTestCase):
    """Copying an auction has to bring the custom fields with it.

    A setting that is missing from ``AuctionCreateView.fields_to_clone`` is not copied, and because
    the copy starts from a fresh ``Auction`` it silently takes the model default instead.  That is
    invisible on the create form -- the club sees a new auction that looks right -- and only turns
    up when the first seller adds a lot and the field they were told to fill in is not there.
    """

    def setUp(self):
        super().setUp()
        userdata = self.user.userdata
        # ALLOW_USERS_TO_CREATE_AUCTIONS is read from the environment, and CI's differs from dev's.
        userdata.can_create_club_auctions = True
        userdata.save()
        self.client.login(username="my_lot", password="testpassword")

    def _copy(self, source):
        """Copy ``source`` the way the Copy button does, and return the new auction."""
        response = self.client.post(
            reverse("create_auction") + f"?copy={source.slug}&clone",
            {
                "title": "Next year's auction",
                "date_start": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cloned_from": source.slug,
            },
        )
        self.assertEqual(response.status_code, 302)
        clone = Auction.objects.filter(title="Next year's auction").first()
        self.assertIsNotNone(clone)
        return clone

    def test_copying_an_auction_keeps_the_custom_checkbox(self):
        self.online_auction.use_custom_checkbox_field = True
        self.online_auction.custom_checkbox_name = "CARES species"
        self.online_auction.save()
        clone = self._copy(self.online_auction)
        self.assertTrue(clone.use_custom_checkbox_field)
        self.assertEqual(clone.custom_checkbox_name, "CARES species")

    def test_the_copy_shows_the_custom_checkbox_when_a_lot_is_added(self):
        # The name alone is not enough: both halves are read together everywhere the field is
        # shown, so a copy that kept the name and lost the switch shows the seller nothing.
        self.online_auction.use_custom_checkbox_field = True
        self.online_auction.custom_checkbox_name = "CARES species"
        self.online_auction.save()
        form_class = quick_add_lot_form_class()
        form = form_class(auction=self._copy(self.online_auction), is_admin=True, tos=None)
        self.assertNotIsInstance(form.fields["custom_checkbox"].widget, forms.HiddenInput)
        self.assertEqual(form.fields["custom_checkbox"].label, "CARES species")

    def test_copying_an_auction_keeps_switched_off_fields_switched_off(self):
        # These two default to True, so leaving them out of the copy turns them back on -- the
        # opposite failure, and just as unwanted by a club that stripped its lot form down.
        self.online_auction.use_description = False
        self.online_auction.use_reference_link = False
        self.online_auction.save()
        clone = self._copy(self.online_auction)
        self.assertFalse(clone.use_description)
        self.assertFalse(clone.use_reference_link)

    def test_every_custom_field_setting_is_copied(self):
        """The custom fields form is entirely settings, so all of it belongs in the copy."""
        from auctions.forms import AuctionCustomFieldsForm
        from auctions.views import AuctionCreateView

        missing = [
            field for field in AuctionCustomFieldsForm.Meta.fields if field not in AuctionCreateView.fields_to_clone
        ]
        self.assertEqual(
            missing,
            [],
            f"{missing} are on the custom fields form but not in AuctionCreateView.fields_to_clone, "
            "so copying an auction resets them to the model default.",
        )


class PayPalFormFieldVisibilityTests(StandardTestCase):
    """Test that PayPal payment field is only shown when user has PayPal connected"""

    def test_enable_online_payments_field_hidden_without_paypal(self):
        """Field should be hidden when user doesn't have PayPal connected"""
        # Ensure no PayPal seller exists for this user
        PayPalSeller.objects.filter(user=self.user).delete()

        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should be hidden (widget is HiddenInput)
        assert isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)

    def test_enable_online_payments_field_visible_with_paypal(self):
        """Field should be visible when user has PayPal connected"""
        # Create a PayPal seller for this user
        PayPalSeller.objects.create(user=self.user, paypal_merchant_id="test_merchant_id")

        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should NOT be hidden
        assert not isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)

    @override_settings(PAYPAL_CLIENT_ID="test_client_id", PAYPAL_SECRET="test_secret")
    def test_enable_online_payments_field_visible_for_superuser_without_paypal(self):
        """Field should be visible for superuser even without PayPal connected (site-wide fallback)"""
        # Create superuser
        superuser = User.objects.create_superuser(
            username="superuser", password="testpassword", email="super@example.com"
        )
        # Create auction by superuser
        superuser_auction = Auction.objects.create(
            created_by=superuser,
            title="Superuser auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )

        # Ensure no PayPal seller exists for superuser
        PayPalSeller.objects.filter(user=superuser).delete()

        form = AuctionEditForm(
            instance=superuser_auction, user=superuser_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should NOT be hidden for superuser (site-wide PayPal fallback)
        assert not isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)

    def test_manage_users_through_club_field_shown_without_club(self):
        self.online_auction.club = None
        self.online_auction.save()
        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # manage_users_through_club is always rendered so JS can toggle it based on club selection
        self.assertNotIsInstance(form.fields["manage_users_through_club"].widget, forms.HiddenInput)

    def test_allow_self_checkin_field_rendered_for_js_toggling(self):
        # Like manage_users_through_club, the real widget is always rendered so the form JS can show
        # it only in check-in mode (see update_self_checkin_field in auction_edit_form.html).
        form = AuctionEditForm(
            instance=self.in_person_auction,
            user=self.in_person_auction.created_by,
            cloned_from=None,
            user_timezone="UTC",
        )
        self.assertNotIsInstance(form.fields["allow_self_checkin"].widget, forms.HiddenInput)
        self.assertFalse(form.fields["allow_self_checkin"].required)
        self.assertTrue(self.in_person_auction.allow_self_checkin)  # on by default

    def test_membership_fee_field_stays_visible_for_club_managed_auction(self):
        paid_club = Club.objects.create(name="Paid Club", membership_annual_fee=Decimal("20.00"))
        self.online_auction.club = paid_club
        self.online_auction.manage_users_through_club = "all"
        self.online_auction.save()
        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        self.assertNotIsInstance(
            form.fields["add_membership_fee_to_invoices_for_expired_members"].widget, forms.HiddenInput
        )

    def test_enable_square_payments_field_hidden_without_square_seller(self):
        self.online_auction.created_by.userdata.square_enabled = True
        self.online_auction.created_by.userdata.save(update_fields=["square_enabled"])
        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        self.assertIsInstance(form.fields["enable_square_payments"].widget, forms.HiddenInput)

    @override_settings(SINGLE_CLUB_MODE=True, NAVBAR_BRAND="Single Club")
    def test_single_club_mode_hides_club_picker_and_blocks_turning_off_management(self):
        # Use an in-person auction: check-in mode is an in-person concept and is rejected for online
        # auctions by clean_manage_users_through_club, so it only belongs in the choices here.
        Club.objects.create(name="Single Club")
        self.in_person_auction.club = None
        self.in_person_auction.manage_users_through_club = ""
        self.in_person_auction.save()

        form = AuctionEditForm(
            instance=self.in_person_auction,
            user=self.in_person_auction.created_by,
            cloned_from=None,
            user_timezone="UTC",
        )

        # The club picker is hidden and pinned to the single club...
        self.assertIsInstance(form.fields["club"].widget, forms.HiddenInput)
        self.assertEqual(form.fields["club"].initial.name, "Single Club")
        # ...but participant management stays visible with the "Off" option removed.
        self.assertNotIsInstance(form.fields["manage_users_through_club"].widget, forms.HiddenInput)
        choice_values = [value for value, _label in form.fields["manage_users_through_club"].choices]
        self.assertNotIn("", choice_values)
        self.assertEqual(set(choice_values), {"all", "checkin"})
        # New single-club auctions default to auto-adding all members; check-in stays available as an opt-in.
        self.assertEqual(form.fields["manage_users_through_club"].initial, "all")


class LotListViewTests(StandardTestCase):
    """Test lot list view with different user types"""

    def test_lot_list_anonymous_user(self):
        """Anonymous users can view lot list"""
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_logged_in_not_joined(self):
        """Logged in users who haven't joined can view lot list"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_logged_in_joined(self):
        """Logged in users who have joined can view lot list"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_admin(self):
        """Admin users can view lot list"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_auction_lot_list_csv_export_includes_donation(self):
        self.lot.donation = True
        self.lot.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("Donation", content)
        self.assertIn("True", content)

    def test_lot_link_falls_back_when_custom_lot_number_is_not_slug_compatible(self):
        self.online_auction.use_seller_dash_lot_numbering = True
        self.online_auction.save(update_fields=["use_seller_dash_lot_numbering"])
        self.lot.custom_lot_number = "bad-lot!"
        self.lot.save(update_fields=["custom_lot_number"])
        self.assertTrue(self.lot.lot_link.startswith(f"/lots/{self.lot.pk}/"))

    def test_all_lots_page_renders_with_invalid_custom_lot_number_slug(self):
        self.online_auction.use_seller_dash_lot_numbering = True
        self.online_auction.save(update_fields=["use_seller_dash_lot_numbering"])
        self.lot.custom_lot_number = "xx RH-5"
        self.lot.save(update_fields=["custom_lot_number"])

        response = self.client.get("/lots/all/")

        self.assertEqual(response.status_code, 200)

    def test_auction_lot_list_csv_export_includes_custom_dropdown(self):
        self.online_auction.use_custom_dropdown_field = "allow"
        self.online_auction.custom_dropdown_name = "Habitat"
        self.online_auction.save()
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="River")
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="Pond")
        self.lot.custom_dropdown = "River"
        self.lot.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("Habitat", content)
        self.assertIn("River", content)

    def test_auction_lot_list_csv_export_skips_custom_dropdown_when_fewer_than_two_options(self):
        self.online_auction.use_custom_dropdown_field = "allow"
        self.online_auction.custom_dropdown_name = "Habitat"
        self.online_auction.save()
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="River")
        self.lot.custom_dropdown = "River"
        self.lot.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertNotIn("Habitat", content)

    def test_auction_lot_admin_helper_text_skips_custom_dropdown_when_fewer_than_two_options(self):
        self.online_auction.use_custom_dropdown_field = "allow"
        self.online_auction.custom_dropdown_name = "Habitat"
        self.online_auction.save()
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="River")
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ", Habitat")

    def test_auction_lot_admin_helper_text_includes_custom_dropdown_when_enabled(self):
        self.online_auction.use_custom_dropdown_field = "allow"
        self.online_auction.custom_dropdown_name = "Habitat"
        self.online_auction.save()
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="River")
        AuctionDropdown.objects.create(auction=self.online_auction, user=self.admin_user, value="Pond")
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ", Habitat")

    def test_auction_lot_admin_uses_shared_query_sync_for_export_and_bulk_actions(self):
        self.online_auction.is_online = False
        self.online_auction.save(update_fields=["is_online"])
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(
            reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug}),
            {"query": f"seller:{self.online_tos.bidder_number}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["htmx_table_header_template"],
            "auctions/partials/auction_lots_table_header.html",
        )
        self.assertContains(
            response,
            f'href="{reverse("lot_list", kwargs={"slug": self.online_auction.slug})}?query=seller%3A{self.online_tos.bidder_number}"',
        )
        self.assertContains(response, 'data-query-sync-hx-vals="query"')
        self.assertContains(response, f'"query": "seller:{self.online_tos.bidder_number}"')


class MyLotsViewTests(StandardTestCase):
    """Test my lots view with different user types"""

    def test_my_lots_anonymous_user(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/selling/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [301, 302, 403]

    def test_my_lots_logged_in_user(self):
        """Logged in users can view their lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get("/selling/")
        assert response.status_code == 200


class AuctionUsersViewTests(StandardTestCase):
    """Test auction users/TOS admin view"""

    def test_auction_users_anonymous(self):
        """Anonymous users should not access user list"""
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [301, 302, 403]

    def test_auction_users_non_admin(self):
        """Non-admin users should not access user list"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should be denied
        assert response.status_code in [302, 403]

    def test_auction_users_admin(self):
        """Admin users should access user list"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_users_creator(self):
        """Auction creator should access user list"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_users_context_configures_shared_htmx_filter_ui(self):
        """Auction users view defines placeholder text and filter choices for the shared HTMX template."""
        self.client.force_login(self.admin_user)
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200
        assert response.context["filter_placeholder_text"] == "Filter by bidder number, name, email..."
        assert ("<i class='bi bi-exclamation-octagon-fill'></i> Can't sell", "no_sell") in response.context[
            "possible_filters"
        ]
        content = response.content.decode(response.charset or "utf-8")
        assert 'data-filter-key="no_sell"' in content
        assert "syncQueryUrlAndLinks" in content
        assert 'data-query-sync-url="' in content

    def test_auction_users_context_omits_bidding_filters_when_online_bidding_disabled(self):
        self.online_auction.online_bidding = "disable"
        self.online_auction.save(update_fields=["online_bidding"])
        self.client.force_login(self.admin_user)
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200
        assert ("<i class='bi bi-cash-coin'></i> Can bid", "can_bid") not in response.context["possible_filters"]
        assert ("<i class='bi bi-cash-coin'></i> Can't bid", "no_bid") not in response.context["possible_filters"]
