"""The rest of the account, and the auction and club setup pages behind it.

``update_preferences`` covered the Preferences tab and nothing else, so the four other pages the
preferences ribbon links to -- contact info, username, label printing, ignore categories -- were
reachable only by being sent to them. The same was true of an auction's pickup locations, its custom
fields, its label layout and its volunteer requests, and of three of a club's four settings pages.

Each test here is one of those, plus the two things about them that are easy to get wrong: contact
details are copied into every auction and club that holds them, and a map marker must never be
guessed from an address.
"""

import json
from pathlib import Path

from django.conf import settings
from django.test import RequestFactory

from auctions import palette_actions
from auctions.mcp import tools as mcp_tools
from auctions.models import AuctionTOS, Club, ClubAPIKey, ClubMember, Location, UserLabelPrefs
from auctions.test_palette_assist import PaletteAssistTestCase


class AccountTestCase(PaletteAssistTestCase):
    """Run an action the way an agent runs one: no page, no browser."""

    def _run(self, action, params=None, user=None):
        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.run_action(request, action, params or {})

    def _fill_in_contact_info(self):
        """What somebody who has used the contact info page once already looks like.

        The form requires a name and an address whatever else is being changed, so without this
        every test below would be testing that rule rather than the thing it is about.
        """
        self.user.first_name = "Ada"
        self.user.last_name = "Bidder"
        self.user.save()
        userdata = self.user.userdata
        userdata.address = "1 Old Street, Springfield"
        userdata.save()


class ContactInfoTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self._fill_in_contact_info()

    def test_one_field_changes_without_the_rest_of_the_page(self):
        """The page wants a name and an address together; one spoken change should not."""
        self.user.first_name = ""
        self.user.last_name = ""
        self.user.save()
        userdata = self.user.userdata
        userdata.address = ""
        userdata.save()
        result = self._run("update_contact_info", {"phone_number": "555-0123"})
        self.assertTrue(result.get("ok"), result)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.phone_number, "555-0123")

    def test_a_bad_value_is_still_refused_on_that_path(self):
        """The fallback cleans each field through the form's own field object, not around it."""
        self.user.first_name = ""
        self.user.save()
        result = self._run("update_contact_info", {"location": "Atlantis"})
        self.assertNotIn("ok", result)

    def test_a_name_said_as_one_thing_is_split(self):
        result = self._run("update_contact_info", {"name": "Ada Okafor"})
        self.assertTrue(result.get("ok"), result)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ada")
        self.assertEqual(self.user.last_name, "Okafor")

    def test_the_halves_can_be_set_on_their_own(self):
        result = self._run("update_contact_info", {"last_name": "Okafor"})
        self.assertTrue(result.get("ok"), result)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, "Okafor")

    def test_a_phone_and_address_go_through_the_form(self):
        result = self._run("update_contact_info", {"phone_number": "555-0100", "address": "12 Mill Lane"})
        self.assertTrue(result.get("ok"), result)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.phone_number, "555-0100")
        self.assertEqual(self.user.userdata.address, "12 Mill Lane")

    def test_naming_one_field_and_a_value_works_too(self):
        result = self._run("update_contact_info", {"setting": "phone", "value": "555-0199"})
        self.assertTrue(result.get("ok"), result)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.phone_number, "555-0199")

    def test_a_field_nobody_recognises_asks_rather_than_guessing(self):
        result = self._run("update_contact_info", {"setting": "favourite fish", "value": "guppy"})
        self.assertIn("more_info_needed", result)

    def test_nothing_named_at_all_asks(self):
        self.assertIn("more_info_needed", self._run("update_contact_info", {}))

    def test_the_ship_to_region_is_matched_by_name(self):
        region = Location.objects.first()
        if region is None:
            region = Location.objects.create(name="Europe")
        result = self._run("update_contact_info", {"location": region.name})
        self.assertTrue(result.get("ok"), result)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.location_id, region.pk)

    def test_a_region_that_does_not_exist_is_refused_with_the_list(self):
        result = self._run("update_contact_info", {"location": "Atlantis"})
        self.assertIn("more_info_needed", result)

    def test_a_map_marker_is_a_pair_of_coordinates(self):
        result = self._run("update_contact_info", {"location_coordinates": "42.36,-71.06"})
        self.assertTrue(result.get("ok"), result)
        self.user.userdata.refresh_from_db()
        self.assertAlmostEqual(self.user.userdata.latitude, 42.36, places=2)
        self.assertAlmostEqual(self.user.userdata.longitude, -71.06, places=2)

    def test_an_address_is_never_turned_into_a_marker(self):
        """The edge case: nothing on this site geocodes, so a marker must not follow an address."""
        result = self._run("update_contact_info", {"location_coordinates": "12 Mill Lane, Boston"})
        self.assertIn("more_info_needed", result)
        self.assertIn("latitude", result["more_info_needed"])

    def test_changing_an_address_says_the_marker_did_not_move(self):
        result = self._run("update_contact_info", {"address": "12 Mill Lane"})
        self.assertTrue(result.get("ok"), result)
        self.assertIn("marker", result.get("note", ""))

    def test_a_recent_auction_gets_the_new_details_too(self):
        """What the contact info page does, and the reason the copies exist at all."""
        tos = AuctionTOS.objects.filter(user=self.user, auction=self.online_auction).first()
        tos.name = "Old Name"
        tos.manually_added = False
        tos.save()
        result = self._run("update_contact_info", {"name": "Ada Okafor", "phone_number": "555-0100"})
        self.assertTrue(result.get("ok"), result)
        tos.refresh_from_db()
        self.assertEqual(tos.name, "Ada Okafor")
        self.assertEqual(tos.phone_number, "555-0100")
        self.assertIn("also_updated_in", result)

    def test_a_row_an_admin_typed_by_hand_is_left_alone(self):
        tos = AuctionTOS.objects.filter(user=self.user, auction=self.online_auction).first()
        tos.name = "What The Admin Typed"
        tos.manually_added = True
        tos.save()
        self._run("update_contact_info", {"name": "Ada Okafor"})
        tos.refresh_from_db()
        self.assertEqual(tos.name, "What The Admin Typed")

    def test_a_club_membership_is_corrected_as_well(self):
        club = Club.objects.create(name="Contact Info Club")
        member = ClubMember.objects.create(club=club, user=self.user, name="Old Name", email=self.user.email)
        self._run("update_contact_info", {"name": "Ada Okafor"})
        member.refresh_from_db()
        self.assertEqual(member.name, "Ada Okafor")


class UsernameTests(AccountTestCase):
    def test_a_username_can_be_changed(self):
        result = self._run("update_username", {"username": "riverbend"})
        self.assertTrue(result.get("ok"), result)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "riverbend")

    def test_the_at_symbol_rule_is_the_forms_own(self):
        """``validate_username_no_at_symbol`` -- the same rule every allauth signup applies."""
        result = self._run("update_username", {"username": "ada@example.com"})
        self.assertNotIn("ok", result)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.username, "ada@example.com")

    def test_a_username_somebody_else_has_is_refused(self):
        result = self._run("update_username", {"username": self.admin_user.username})
        self.assertNotIn("ok", result)

    def test_the_same_username_is_a_no_op_rather_than_an_error(self):
        result = self._run("update_username", {"username": self.user.username})
        self.assertTrue(result.get("ok"), result)

    def test_it_says_what_the_old_one_was(self):
        was = self.user.username
        result = self._run("update_username", {"username": "riverbend"})
        self.assertEqual(result["previously"], was)
        self.assertEqual(result["undo"]["params"]["username"], was)


class PrintingPreferenceTests(AccountTestCase):
    def test_a_preference_is_set_through_the_pages_own_form(self):
        result = self._run("update_printing_preferences", {"setting": "empty labels", "value": 3})
        self.assertTrue(result.get("ok"), result)
        prefs = UserLabelPrefs.objects.get(user=self.user)
        self.assertEqual(prefs.empty_labels, 3)

    def test_an_unknown_preference_lists_the_real_ones(self):
        result = self._run("update_printing_preferences", {"setting": "sparkles", "value": "on"})
        self.assertIn("more_info_needed", result)
        self.assertIn("preset", result["more_info_needed"])

    def test_the_print_method_is_settable_even_though_the_web_page_hides_it(self):
        result = self._run("update_printing_preferences", {"setting": "print method", "value": "pdf"})
        self.assertTrue(result.get("ok"), result)


class LotFieldTests(AccountTestCase):
    """The two per-lot fields the catalogue could not reach, and the auction's own names for them."""

    def setUp(self):
        super().setUp()
        self.auction = self.online_auction
        self.auction.use_custom_checkbox_field = True
        self.auction.custom_checkbox_name = "CARES species"
        self.auction.use_reference_link = True
        self.auction.save()

    def _lot(self):
        """The fixture's first lot. Its seller is ``online_tos``, which is the admin's row."""
        return self.lot

    def test_the_auctions_own_name_for_the_checkbox_is_reported(self):
        result = self._run("describe_auction", {"auction": self.auction.slug})
        fields = result["auction"]["lot_fields_this_auction_uses"]
        self.assertEqual(fields.get("custom_checkbox", {}).get("label"), "CARES species")

    def test_the_reference_link_says_a_video_is_embedded(self):
        """Said in describe_auction briefly and in the parameter documentation at length.

        The long version cost 168 characters of a 5000-character budget that describe_auction sends
        on every lookup, and truncated the auction's rules off the end of it.
        """
        result = self._run("describe_auction", {"auction": self.auction.slug})
        fields = result["auction"]["lot_fields_this_auction_uses"]
        self.assertIn("YouTube", fields.get("reference_link", {}).get("means", ""))
        self.assertIn("YouTube", palette_actions.ACTIONS["edit_lot"].params["reference_link"])

    def test_a_checkbox_can_be_set_on_a_lot(self):
        lot = self._lot()
        result = self._run("edit_lot", {"lot": lot.lot_name, "custom_checkbox": True}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        lot.refresh_from_db()
        self.assertTrue(lot.custom_checkbox)

    def test_a_reference_link_can_be_set_on_a_lot(self):
        lot = self._lot()
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = self._run("edit_lot", {"lot": lot.lot_name, "reference_link": url}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        lot.refresh_from_db()
        self.assertEqual(lot.reference_link, url)
        self.assertIsNotNone(lot.video_link)

    def test_something_that_is_not_a_url_is_refused(self):
        lot = self._lot()
        result = self._run(
            "edit_lot",
            {"lot": lot.lot_name, "reference_link": "ask me at the meeting"},
            user=self.admin_user,
        )
        self.assertIn("error", result)

    def test_a_field_the_auction_switched_off_is_refused_rather_than_saved(self):
        """The form hides a disabled field instead of deleting it, so it would otherwise be saved."""
        self.auction.use_custom_checkbox_field = False
        self.auction.save()
        lot = self._lot()
        result = self._run("edit_lot", {"lot": lot.lot_name, "custom_checkbox": True}, user=self.admin_user)
        self.assertIn("error", result)
        lot.refresh_from_db()
        self.assertFalse(lot.custom_checkbox)


class AuctionSetupTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.auction = self.online_auction

    def test_pickup_locations_can_be_listed(self):
        result = self._run("list_pickup_locations", {"auction": self.auction.slug})
        self.assertTrue(result.get("found"), result)
        self.assertTrue(result["locations"])

    def test_an_admin_can_add_a_pickup_location(self):
        from auctions.models import PickupLocation

        result = self._run(
            "add_pickup_location",
            {
                "auction": self.auction.slug,
                "name": "The clubhouse",
                "address": "1 Mill Lane",
                "location_coordinates": "42.36,-71.06",
            },
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(PickupLocation.objects.filter(auction=self.auction, name="The clubhouse").exists())

    def test_somebody_who_does_not_run_it_cannot_add_one(self):
        result = self._run("add_pickup_location", {"auction": self.auction.slug, "name": "Nowhere"}, user=self.userB)
        self.assertNotIn("ok", result)

    def test_a_pickup_location_can_be_edited(self):
        self.location.location_coordinates = "42.36,-71.06"
        self.location.save()
        result = self._run(
            "update_pickup_location",
            {"auction": self.auction.slug, "location": self.location.name, "setting": "address", "value": "9 New Road"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.location.refresh_from_db()
        self.assertEqual(self.location.address, "9 New Road")

    def test_a_location_with_no_map_marker_says_that_rather_than_talking_about_a_map(self):
        """``PickupLocationForm`` refuses every field until there is a marker; it says so in a
        sentence about a map that is not here."""
        result = self._run(
            "update_pickup_location",
            {"auction": self.auction.slug, "location": self.location.name, "setting": "address", "value": "9 New Road"},
            user=self.admin_user,
        )
        self.assertIn("more_info_needed", result)
        self.assertIn("location_coordinates", result["more_info_needed"])

    def test_dropdown_options_can_be_added_and_removed(self):
        from auctions.models import AuctionDropdown

        added = self._run(
            "add_dropdown_option", {"auction": self.auction.slug, "option": "Cichlid"}, user=self.admin_user
        )
        self.assertTrue(added.get("ok"), added)
        self.assertTrue(AuctionDropdown.objects.filter(auction=self.auction, value="Cichlid").exists())
        removed = self._run(
            "remove_dropdown_option", {"auction": self.auction.slug, "option": "Cichlid"}, user=self.admin_user
        )
        self.assertTrue(removed.get("ok"), removed)
        self.assertFalse(AuctionDropdown.objects.filter(auction=self.auction, value="Cichlid").exists())

    def test_a_duplicate_dropdown_option_is_not_an_error(self):
        self._run("add_dropdown_option", {"auction": self.auction.slug, "option": "Cichlid"}, user=self.admin_user)
        again = self._run(
            "add_dropdown_option", {"auction": self.auction.slug, "option": "cichlid"}, user=self.admin_user
        )
        self.assertTrue(again.get("ok"), again)

    def test_label_fields_with_nothing_named_reports_what_prints(self):
        result = self._run("update_label_fields", {"auction": self.auction.slug}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("printing_now", result)

    def test_a_label_field_can_be_turned_off_and_back_on(self):
        off = self._run(
            "update_label_fields",
            {"auction": self.auction.slug, "field": "QR Code", "value": False},
            user=self.admin_user,
        )
        self.assertTrue(off.get("ok"), off)
        self.auction.refresh_from_db()
        self.assertNotIn("qr_code", self.auction.label_print_fields.split(","))
        on = self._run(
            "update_label_fields",
            {"auction": self.auction.slug, "field": "QR Code", "value": True},
            user=self.admin_user,
        )
        self.assertTrue(on.get("ok"), on)
        self.auction.refresh_from_db()
        self.assertIn("qr_code", self.auction.label_print_fields.split(","))

    def test_a_label_field_that_does_not_exist_lists_the_real_ones(self):
        result = self._run(
            "update_label_fields",
            {"auction": self.auction.slug, "field": "hologram", "value": True},
            user=self.admin_user,
        )
        self.assertIn("more_info_needed", result)
        self.assertIn("Lot name", result["more_info_needed"])

    def test_which_lot_fields_sellers_see_is_an_auction_setting(self):
        """The custom fields page, reached by the same tool as the rules page."""
        result = self._run(
            "update_auction_setting",
            {"auction": self.auction.slug, "setting": "use_quantity_field", "value": True},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.use_quantity_field)

    def test_naming_a_field_whose_switch_is_off_says_the_form_overrode_it(self):
        """``AuctionCustomFieldsForm.clean`` blanks the name of a checkbox that is switched off."""
        self.auction.use_custom_checkbox_field = False
        self.auction.save()
        result = self._run(
            "update_auction_setting",
            {"auction": self.auction.slug, "setting": "custom_checkbox_name", "value": "CARES species"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn("note", result)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.custom_checkbox_name, "")

    def test_volunteers_can_be_asked_for_at_an_in_person_auction(self):
        from auctions.models import VolunteerJob

        result = self._run(
            "request_volunteers",
            {"auction": self.in_person_auction.slug, "description": "help carry tables", "people_needed": 2},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(VolunteerJob.objects.filter(auction=self.in_person_auction).exists())

    def test_an_online_auction_has_no_room_to_ask(self):
        result = self._run(
            "request_volunteers", {"auction": self.auction.slug, "description": "help"}, user=self.admin_user
        )
        self.assertIn("error", result)

    def test_a_request_for_help_can_be_cancelled(self):
        from auctions.models import VolunteerJob

        self._run(
            "request_volunteers",
            {"auction": self.in_person_auction.slug, "description": "help carry tables"},
            user=self.admin_user,
        )
        result = self._run(
            "cancel_volunteer_request",
            {"auction": self.in_person_auction.slug, "job": "carry tables"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(VolunteerJob.objects.filter(auction=self.in_person_auction, canceled=True).exists())


class ClubSettingsTests(AccountTestCase):
    """A club's settings live on four pages, and each page keeps its own permission."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Settings Club", enable_breeder_award_program=True)
        self.officer = ClubMember.objects.create(
            club=self.club,
            user=self.user,
            name="Ada Officer",
            email=self.user.email,
            permission_edit_club=True,
        )

    def test_a_setting_on_the_club_page_still_works(self):
        result = self._run("update_club_setting", {"club": self.club.name, "setting": "allow_joining", "value": True})
        self.assertTrue(result.get("ok"), result)
        self.club.refresh_from_db()
        self.assertTrue(self.club.allow_joining)

    def test_a_breeder_award_setting_needs_the_breeder_award_permission(self):
        refused = self._run("update_club_setting", {"club": self.club.name, "setting": "points_per_lot", "value": 5})
        self.assertIn("error", refused)
        self.officer.permission_manage_bap = True
        self.officer.save()
        allowed = self._run("update_club_setting", {"club": self.club.name, "setting": "points_per_lot", "value": 5})
        self.assertTrue(allowed.get("ok"), allowed)
        self.club.refresh_from_db()
        self.assertEqual(self.club.points_per_lot, 5)

    def test_a_membership_setting_is_reachable(self):
        result = self._run(
            "update_club_setting", {"club": self.club.name, "setting": "show_member_barcode", "value": True}
        )
        self.assertTrue(result.get("ok"), result)
        self.club.refresh_from_db()
        self.assertTrue(self.club.show_member_barcode)

    def test_an_email_setting_is_reachable(self):
        result = self._run(
            "update_club_setting",
            {"club": self.club.name, "setting": "send_welcome_email_to_new_members", "value": True},
        )
        self.assertTrue(result.get("ok"), result)
        self.club.refresh_from_db()
        self.assertTrue(self.club.send_welcome_email_to_new_members)

    def test_the_answer_says_which_page_the_setting_was_on(self):
        result = self._run(
            "update_club_setting", {"club": self.club.name, "setting": "show_member_barcode", "value": True}
        )
        self.assertEqual(result.get("on_page"), "membership")

    def test_an_unknown_setting_lists_what_can_be_changed(self):
        result = self._run("update_club_setting", {"club": self.club.name, "setting": "sparkles", "value": "on"})
        self.assertIn("more_info_needed", result)
        self.assertIn("points_per_lot", result["more_info_needed"])


class ClubSetupTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Survey Club")
        ClubMember.objects.create(
            club=self.club, user=self.user, name="Ada Officer", email=self.user.email, permission_edit_club=True
        )

    def test_it_lists_everything_the_site_does_for_a_club(self):
        result = self._run("club_setup", {"club": self.club.name})
        self.assertTrue(result.get("found"), result)
        self.assertGreater(len(result["features"]), 10)
        names = [row["feature"] for row in result["features"]]
        self.assertIn("Breeder award program", names)

    def test_every_feature_says_what_it_is_for_and_how_to_turn_it_on(self):
        result = self._run("club_setup", {"club": self.club.name})
        for row in result["features"]:
            self.assertTrue(row["what_it_does"], row)
            self.assertTrue(row["how_to_turn_it_on"], row)

    def test_the_unused_half_is_the_useful_question(self):
        result = self._run("club_setup", {"club": self.club.name, "show": "unused"})
        self.assertTrue(result["features"])
        self.assertTrue(all(not row["in_use"] for row in result["features"]))

    def test_turning_something_on_moves_it_between_the_halves(self):
        self.club.enable_breeder_award_program = True
        self.club.save()
        in_use = self._run("club_setup", {"club": self.club.name, "show": "in_use"})
        self.assertIn("Breeder award program", [row["feature"] for row in in_use["features"]])

    def test_somebody_with_no_part_in_the_club_is_refused(self):
        result = self._run("club_setup", {"club": self.club.name}, user=self.userB)
        self.assertNotIn("found", result)

    def test_somebody_with_no_club_still_learns_what_the_site_offers(self):
        """The other half of the question, asked before there is a club to ask it about."""
        result = self._run("club_setup", {}, user=self.userB)
        self.assertTrue(result.get("found"), result)
        self.assertIsNone(result["club"])
        self.assertTrue(result["features"])
        self.assertNotIn("in_use", result["features"][0])

    def test_naming_somebody_elses_club_is_still_a_refusal(self):
        result = self._run("club_setup", {"club": self.club.name}, user=self.userB)
        self.assertNotIn("found", result)


class EverySettingIsReachableTests(AccountTestCase):
    """The guards that keep this from rotting the day somebody adds a club feature.

    ``test_palette_skills`` already fails the build when a view accepting a POST is neither a skill
    nor written down in ``NOT_A_SKILL``. These are the same idea one level down: a settings *form*
    that no tool can reach, and a feature in ``club_setup`` that points at nothing, are both things
    that pass that audit and are still broken — the page is a skill, the setting on it isn't.
    """

    def test_every_club_settings_form_is_reachable_or_has_a_written_reason(self):
        from django import forms as django_forms

        from auctions import forms as site_forms
        from auctions.models import Club

        club_forms = {
            name
            for name in dir(site_forms)
            if isinstance(getattr(site_forms, name), type)
            and issubclass(getattr(site_forms, name), django_forms.ModelForm)
            and getattr(getattr(getattr(site_forms, name), "Meta", None), "model", None) is Club
        }
        reachable = {name for name, _perms, _label, _url in palette_actions._CLUB_SETTING_PAGES}
        unexplained = sorted(club_forms - reachable - set(palette_actions._CLUB_FORMS_NOT_SPOKEN))
        self.assertEqual(
            unexplained,
            [],
            "These forms edit a Club and update_club_setting can't reach them, and nobody has said "
            "why. Add them to _CLUB_SETTING_PAGES with the permission their page requires, or a "
            "reason to _CLUB_FORMS_NOT_SPOKEN.",
        )

    def test_every_field_on_a_reachable_form_can_be_named(self):
        index = palette_actions._club_setting_fields()
        for _form_class, form, _perms, label, _url in palette_actions._club_setting_pages():
            for name in form.fields:
                if name in palette_actions._CLUB_SETTINGS_NOT_SPOKEN:
                    continue
                self.assertIn(name, index, f"{name} is on the {label} form but cannot be named")

    def test_every_club_feature_points_at_something_real(self):
        """A survey row whose 'how to turn it on' names a tool that doesn't exist is worse than none."""
        for feature in palette_actions._CLUB_FEATURES:
            for setting in feature.get("settings", ()):
                self.assertIsNotNone(
                    palette_actions._resolve_club_setting(setting),
                    f"{feature['key']} says to set {setting}, which update_club_setting can't find",
                )
            if feature.get("tool"):
                self.assertIn(
                    feature["tool"],
                    palette_actions.ACTIONS,
                    f"{feature['key']} names {feature['tool']}, which isn't registered",
                )
            self.assertTrue(palette_actions._how_to_turn_it_on(feature), f"{feature['key']} says nothing")

    def test_the_only_features_with_no_tool_are_the_ones_needing_a_browser(self):
        """Every 'go to the page' row is an OAuth sign-in with somebody else, and says so."""
        for feature in palette_actions._CLUB_FEATURES:
            if feature.get("settings") or feature.get("tool"):
                continue
            self.assertIn("sign-in", feature["page"], f"{feature['key']} sends people to a page for no stated reason")

    def test_the_features_named_in_the_survey_are_the_ones_a_club_actually_has(self):
        """Every integration with a settings page is in the survey, so none is invisible."""
        keys = {feature["key"] for feature in palette_actions._CLUB_FEATURES}
        for expected in ("discord", "google_calendar", "email_campaigns", "website_embeds", "donation_tracking"):
            self.assertIn(expected, keys)

    def test_the_auction_setting_vocabulary_covers_both_of_its_forms(self):
        from auctions.models import User

        user = User.objects.filter(is_superuser=True).first() or self.admin_user
        rules = palette_actions._auction_setting_form(user)
        settable = palette_actions._auction_setting_fields(rules)
        for name in settable:
            self.assertIsNotNone(palette_actions._resolve_auction_setting(settable, name))
        lot_fields = palette_actions._lot_field_settings_form(self.online_auction)
        for name in lot_fields.fields:
            self.assertIsNotNone(
                palette_actions._resolve_form_setting(lot_fields.fields, name),
                f"{name} is on the custom fields form but update_auction_setting can't find it",
            )


class ClubIntegrationTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Integration Club")
        ClubMember.objects.create(
            club=self.club, user=self.user, name="Ada Officer", email=self.user.email, permission_edit_club=True
        )

    def test_the_calendar_switches_are_settings_like_any_other(self):
        result = self._run(
            "update_club_setting", {"club": self.club.name, "setting": "add_auctions_to_calendar", "value": False}
        )
        self.assertTrue(result.get("ok"), result)
        self.club.refresh_from_db()
        self.assertFalse(self.club.add_auctions_to_calendar)

    def test_donation_tracking_is_a_setting_too(self):
        """And turning it on asks for the address a receipt needs, which is the form's own rule."""
        first = self._run(
            "update_club_setting", {"club": self.club.name, "setting": "enable_donation_tracking", "value": True}
        )
        self.assertIn("more_info_needed", first)
        self.club.donation_mailing_address = "1 Club Street"
        self.club.save()
        result = self._run(
            "update_club_setting", {"club": self.club.name, "setting": "enable_donation_tracking", "value": True}
        )
        self.assertTrue(result.get("ok"), result)
        self.club.refresh_from_db()
        self.assertTrue(self.club.enable_donation_tracking)

    def test_syncing_a_calendar_nobody_connected_says_so(self):
        result = self._run("sync_club_calendar", {"club": self.club.name})
        self.assertIn("error", result)
        self.assertIn("OAuth", result["error"])

    def test_the_website_snippets_are_listed_with_what_would_show(self):
        result = self._run("club_website_snippets", {"club": self.club.name})
        self.assertTrue(result.get("found"), result)
        keys = {row["snippet"] for row in result["embeds"]}
        self.assertEqual(keys, {"events", "past_events", "auction", "announcement", "leaderboard"})
        leaderboard = next(row for row in result["embeds"] if row["snippet"] == "leaderboard")
        self.assertFalse(leaderboard["would_show_something_now"])

    def test_the_snippets_send_people_to_the_page_with_the_real_code(self):
        result = self._run("club_website_snippets", {"club": self.club.name})
        self.assertIn(self.club.slug, result["copy_the_code_from_url"])

    def test_somebody_with_no_part_in_the_club_gets_no_snippets(self):
        result = self._run("club_website_snippets", {"club": self.club.name}, user=self.userB)
        self.assertNotIn("found", result)


class ClubAPIToolTests(AccountTestCase):
    """``club_api``: what a club's own API can do, read by whoever is about to write against it.

    The tool exists so an agent asked for "an integration that puts our lots on our website" can
    find out what is already there instead of guessing at endpoints. So the tests are mostly about
    the two halves of that: what it says about the keys, and that the documentation it hands over
    is the page's own and still fits in one answer.
    """

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="API Club")
        ClubMember.objects.create(
            club=self.club, user=self.user, name="Ada Officer", email=self.user.email, permission_edit_club=True
        )
        raw, prefix, key_hash = ClubAPIKey.generate()
        self.raw_key = raw
        self.key = ClubAPIKey.objects.create(
            club=self.club,
            name="WordPress",
            prefix=prefix,
            key_hash=key_hash,
            can_add_club_members=False,
            can_read_auction_info=True,
            can_read_public_lots=True,
        )

    def test_the_keys_are_listed_with_what_each_one_may_do(self):
        result = self._run("club_api", {"club": self.club.name})
        self.assertTrue(result.get("found"), result)
        row = result["keys"][0]
        self.assertIn("WordPress", row["name"])
        self.assertEqual(row["prefix"], self.key.prefix)
        self.assertIn("Read public lot info", row["can"])
        self.assertNotIn("Can add club members", row["can"])

    def test_the_secret_is_not_in_the_answer_because_nothing_can_read_it(self):
        """The half of the key that is a credential was never stored, and the answer says so."""
        result = self._run("club_api", {"club": self.club.name, "topic": "auctions"})
        body = json.dumps(result)
        self.assertNotIn(self.raw_key, body)
        self.assertNotIn(self.raw_key.split(".", 1)[-1], body)
        self.assertIn("shown once", result["secrets"])

    def test_it_says_which_tick_box_a_capability_needs_and_who_has_it(self):
        result = self._run("club_api", {"club": self.club.name})
        by_box = {row["tick_box"]: row for row in result["capabilities"]}
        self.assertTrue(any("WordPress" in name for name in by_box["Read public lot info"]["keys_with_it"]))
        self.assertEqual(by_box["Can add club members"]["keys_with_it"], [])
        self.assertIn(self.club.slug, result["create_a_key_url"])

    def test_a_topic_hands_over_the_documentation_the_page_shows(self):
        result = self._run("club_api", {"club": self.club.name, "topic": "auctions"})
        documentation = result["documentation"]
        self.assertIn(f"/api/v1/clubs/{self.club.slug}/auctions/", documentation)
        self.assertIn("X-API-Key", documentation)
        # One topic, not the whole page: the species endpoints are a different call.
        self.assertNotIn("species-lookup", documentation)

    def test_every_topic_still_fits_in_one_mcp_result(self):
        """The reason the documentation is cut into topics at all. Whole, it does not fit."""
        for topic in palette_actions._API_TOPICS:
            result = self._run("club_api", {"club": self.club.name, "topic": topic})
            self.assertTrue(result["documentation"], f"{topic} documented nothing")
            self.assertLess(len(json.dumps(result, default=str)), mcp_tools.MAX_RESULT_CHARS, topic)

    def test_a_topic_this_api_has_not_got_is_refused_rather_than_ignored(self):
        result = self._run("club_api", {"club": self.club.name, "topic": "invoices"})
        self.assertIn("error", result)
        self.assertIn("members", result["error"])

    def test_a_named_key_documents_what_that_key_may_actually_call(self):
        result = self._run("club_api", {"club": self.club.name, "key": "WordPress", "topic": "auctions"})
        self.assertIn("Tick", result["documentation"])
        self.assertIn(self.key.prefix, result["documentation"])

    def test_a_key_that_cannot_reach_a_topic_is_told_which_box_it_would_need(self):
        result = self._run("club_api", {"club": self.club.name, "key": "WordPress", "topic": "species"})
        self.assertIn("error", result)
        self.assertIn("Can use species", result["error"])

    def test_a_whole_key_pasted_in_matches_on_its_prefix_and_the_secret_goes_nowhere(self):
        """The commonest way an agent will name a key is by copying one out of a config file."""
        result = self._run("club_api", {"club": self.club.name, "key": self.raw_key})
        self.assertTrue(result.get("found"), result)
        self.assertNotIn(self.raw_key.split(".", 1)[-1], json.dumps(result))

    def test_a_key_nobody_has_is_a_refusal_naming_the_ones_that_exist(self):
        result = self._run("club_api", {"club": self.club.name, "key": "Squarespace"})
        self.assertIn("error", result)
        self.assertIn("WordPress", result["error"])

    def test_a_club_with_no_keys_is_pointed_at_the_page_that_makes_one(self):
        """It cannot make one, on purpose: the tick boxes are fixed for the life of the key."""
        self.key.delete()
        result = self._run("club_api", {"club": self.club.name})
        self.assertEqual(result["keys"], [])
        self.assertIn(self.club.slug, result["create_a_key_url"])
        self.assertEqual(ClubAPIKey.objects.filter(club=self.club).count(), 0)

    def test_somebody_who_cannot_edit_the_club_cannot_read_its_keys(self):
        """The page needs permission_edit_club, and so does this. An ordinary member is not enough."""
        ClubMember.objects.create(club=self.club, user=self.userB, name="Bob Member", email=self.userB.email)
        result = self._run("club_api", {"club": self.club.name}, user=self.userB)
        self.assertNotIn("found", result)
        self.assertIn("error", result)

    def test_a_stranger_gets_nothing(self):
        result = self._run("club_api", {"club": self.club.name}, user=self.userB)
        self.assertNotIn("found", result)

    def test_the_permission_table_still_matches_the_model(self):
        """A flag added to ClubAPIKey and not to the table would be a capability nobody is told about."""
        named = {flag for flag, _, _ in palette_actions._API_PERMISSIONS}
        on_the_model = {
            field.name
            for field in ClubAPIKey._meta.get_fields()
            if field.name.startswith("can_") and getattr(field, "get_internal_type", lambda: "")() == "BooleanField"
        }
        self.assertEqual(named, on_the_model)

    def test_every_tick_box_is_spelled_the_way_the_page_spells_it(self):
        """The labels are read out to somebody looking at that page, so they have to match it."""
        page = (Path(settings.BASE_DIR) / "auctions/templates/auctions/club_api_key_create.html").read_text()
        for _, label, _ in palette_actions._API_PERMISSIONS:
            self.assertIn(label, page, f"“{label}” is not what the create page calls it any more")

    def test_the_tool_is_read_only(self):
        """It reads credentials' permissions. Nothing here may be offered to a write-shaped caller."""
        action = palette_actions.ACTIONS["club_api"]
        self.assertEqual(action.danger, palette_actions.DANGER_SAFE)
        self.assertTrue(mcp_tools.read_only(action))


class EmailChangeTests(AccountTestCase):
    def test_changing_an_email_sends_a_confirmation_rather_than_changing_it(self):
        from allauth.account.models import EmailAddress

        was = self.user.email
        result = self._run("change_email", {"email": "ada-new@example.com"})
        self.assertTrue(result.get("ok"), result)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, was)
        self.assertTrue(EmailAddress.objects.filter(user=self.user, email="ada-new@example.com").exists())
        self.assertTrue(result["nothing_was_changed_yet"])

    def test_the_address_they_already_have_is_a_no_op(self):
        result = self._run("change_email", {"email": self.user.email})
        self.assertTrue(result.get("ok"), result)

    def test_something_that_is_not_an_address_is_refused(self):
        result = self._run("change_email", {"email": "not an email"})
        self.assertNotIn("ok", result)


class LotDescriptionTests(AccountTestCase):
    def test_a_description_can_be_written_on_a_lot(self):
        result = self._run(
            "edit_lot",
            {"lot": self.lot.lot_name, "description": "F2 from a wild pair, eating frozen."},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.lot.refresh_from_db()
        self.assertIn("eating frozen", self.lot.summernote_description)

    def test_an_essay_is_refused_with_the_limit(self):
        result = self._run("edit_lot", {"lot": self.lot.lot_name, "description": "guppy " * 200}, user=self.admin_user)
        self.assertIn("error", result)
        self.assertIn(str(palette_actions.MAX_SPOKEN_DESCRIPTION_CHARS), result["error"])

    def test_an_auction_with_descriptions_switched_off_refuses_one(self):
        self.online_auction.use_description = False
        self.online_auction.save()
        result = self._run("edit_lot", {"lot": self.lot.lot_name, "description": "A short one."}, user=self.admin_user)
        self.assertIn("error", result)
