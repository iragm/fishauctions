"""The breeder award program: which lots are eligible, and the pages that award points."""

import datetime
import importlib.util
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Category,
    Club,
    ClubAPIKey,
    ClubAPIKeyFieldMap,
    ClubBapCategoryOverride,
    ClubBapGenusOverride,
    ClubHistory,
    ClubMember,
    Lot,
    PickupLocation,
    Species,
)
from auctions.tests import StandardTestCase


class LotBapEligibilityTests(TestCase):
    """Tests for unsold_lot_no_bap_reason and auto_award_bap_points."""

    def setUp(self):
        self.user = User.objects.create_user(username="bap_seller", password="testpass", email="seller@example.com")
        self.club = Club.objects.create(
            name="BAP Eligibility Club",
            enable_breeder_award_program=True,
            auto_add_points=True,
            min_quantity=1,
            points_per_lot=5,
        )
        self.category = Category.objects.create(name="Livebearers", bap_points=5)
        self.auction = Auction.objects.create(
            title="BAP Auction",
            created_by=self.user,
            club=self.club,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        self.member = ClubMember.objects.create(club=self.club, user=self.user)
        self.location = PickupLocation.objects.create(
            name="Test Location",
            auction=self.auction,
            pickup_time=timezone.now() + datetime.timedelta(days=7),
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)

    def _make_lot(self, **kwargs):
        defaults = {
            "lot_name": "Fancy Guppies",
            "auction": self.auction,
            "auctiontos_seller": self.tos,
            "quantity": 5,
            "i_bred_this_fish": True,
            "species_category": self.category,
            "active": False,
            "winning_price": 10,
            "auctiontos_winner": self.tos,
        }
        defaults.update(kwargs)
        return Lot.objects.create(**defaults)

    def test_not_eligible_when_bap_disabled(self):
        self.club.enable_breeder_award_program = False
        self.club.save()
        lot = self._make_lot()
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_eligible")

    def test_not_eligible_when_no_auction(self):
        lot = self._make_lot(auction=None, auctiontos_seller=None)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_eligible")

    def test_not_bred_returned_when_not_bred(self):
        lot = self._make_lot(i_bred_this_fish=False)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_bred")

    def test_low_quantity_returned(self):
        self.club.min_quantity = 10
        self.club.save()
        lot = self._make_lot(quantity=3)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "low_quantity")

    def test_category_not_eligible_when_bap_points_zero(self):
        zero_cat = Category.objects.create(name="Ineligible", bap_points=0)
        lot = self._make_lot(species_category=zero_cat)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "category_not_eligible")

    def test_not_club_member_when_user_not_member(self):
        outsider = User.objects.create_user(username="outsider", password="tp", email="out@example.com")
        outsider_tos = AuctionTOS.objects.create(user=outsider, auction=self.auction, pickup_location=self.location)
        lot = self._make_lot(auctiontos_seller=outsider_tos)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_club_member")

    def test_eligible_returns_none(self):
        lot = self._make_lot()
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_same_name_rule_uses_email_when_seller_user_missing(self):
        self.club.days_between_same_name_lots = 30
        self.club.save(update_fields=["days_between_same_name_lots"])
        self.member.user = None
        self.member.email = self.user.email
        self.member.save(update_fields=["user", "email"])
        self.tos.user = None
        self.tos.email = self.user.email
        self.tos.save(update_fields=["user", "email"])
        self._make_lot(
            lot_name="Repeat Name",
            auctiontos_seller=self.tos,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(
            lot_name="Repeat Name",
            auctiontos_seller=self.tos,
            date_end=timezone.now(),
        )
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_long_enough")

    def test_same_name_rule_uses_prior_lot_user_email(self):
        self.club.days_between_same_name_lots = 30
        self.club.save(update_fields=["days_between_same_name_lots"])
        prior_user = User.objects.create_user(username="other_email_match", password="testpass", email=self.user.email)
        prior_tos = AuctionTOS.objects.create(user=prior_user, auction=self.auction, pickup_location=self.location)
        prior_tos.email = "different@example.com"
        prior_tos.save(update_fields=["email"])
        self._make_lot(
            lot_name="Repeat Name",
            user=prior_user,
            auctiontos_seller=prior_tos,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(
            lot_name="Repeat Name",
            date_end=timezone.now(),
        )
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_long_enough")

    def test_same_species_rule_blocks_the_same_fish_under_a_different_name(self):
        """The point of the rule: the name rule cannot see that these are one fish bred twice."""
        self.club.days_between_same_species_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots"])
        yellow_lab = Species.objects.create(genus="Labidochromis", species="caeruleus", common_name="Yellow lab")
        self._make_lot(
            lot_name="Yellow labs",
            species=yellow_lab,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(
            lot_name="Labidochromis caeruleus", species=yellow_lab, user=self.user, date_end=timezone.now()
        )
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_long_enough")

    def test_a_different_strain_of_the_same_species_still_earns_points(self):
        """Blue and red cherry shrimp are two things to breed, and the strains are separate rows."""
        self.club.days_between_same_species_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots"])
        neocaridina = Species.objects.create(genus="Neocaridina", species="davidi", common_name="Cherry shrimp")
        blue = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=neocaridina, source="aquarium"
        )
        red = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Fire Red", parent=neocaridina, source="aquarium"
        )
        self._make_lot(
            lot_name="Blue dream shrimp",
            species=blue,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(lot_name="Fire red shrimp", species=red, user=self.user, date_end=timezone.now())
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_the_same_strain_twice_is_still_blocked(self):
        self.club.days_between_same_species_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots"])
        neocaridina = Species.objects.create(genus="Neocaridina", species="davidi", common_name="Cherry shrimp")
        blue = Species.objects.create(
            genus="Neocaridina", species="davidi", variety="Blue Dream", parent=neocaridina, source="aquarium"
        )
        self._make_lot(
            lot_name="Blue dream shrimp",
            species=blue,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(lot_name="Blue dreams", species=blue, user=self.user, date_end=timezone.now())
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_long_enough")

    def test_the_same_species_outside_the_window_is_fine(self):
        self.club.days_between_same_species_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots"])
        species = Species.objects.create(genus="Poecilia", species="reticulata", common_name="Guppy")
        prior = self._make_lot(lot_name="Guppies", species=species, user=self.user, bap_points_awarded=5)
        # update(), not save(): Lot._do_save pulls date_end back to the auction's, which would put
        # this lot inside the window again and quietly test nothing.
        Lot.objects.filter(pk=prior.pk).update(date_end=timezone.now() - datetime.timedelta(days=45))
        lot = self._make_lot(lot_name="Guppies", species=species, user=self.user, date_end=timezone.now())
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_zero_means_the_species_rule_is_off(self):
        species = Species.objects.create(genus="Poecilia", species="reticulata", common_name="Guppy")
        self._make_lot(
            lot_name="Guppies",
            species=species,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(lot_name="Different name", species=species, user=self.user, date_end=timezone.now())
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_a_lot_with_no_species_is_untouched_by_the_species_rule(self):
        self.club.days_between_same_species_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots"])
        species = Species.objects.create(genus="Poecilia", species="reticulata", common_name="Guppy")
        self._make_lot(
            lot_name="Guppies",
            species=species,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(lot_name="Mixed bag", species=None, user=self.user, date_end=timezone.now())
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_two_lots_with_no_species_are_both_judged_on_their_names(self):
        """No species means no *opinion*, not a match against every other unnamed lot.

        The rule is guarded on ``self.species_id``, so a lot with nothing picked falls straight
        through to the next check rather than colliding with every other one.  That is why there is
        no separate setting for it: the club already has ``days_between_same_name_lots`` for the
        case it actually cares about, and it is the rule that can see these two are different.
        """
        self.club.days_between_same_species_lots = 30
        self.club.days_between_same_name_lots = 30
        self.club.save(update_fields=["days_between_same_species_lots", "days_between_same_name_lots"])
        self._make_lot(
            lot_name="Sponge filter",
            species=None,
            user=self.user,
            date_end=timezone.now() - datetime.timedelta(days=1),
            bap_points_awarded=5,
        )
        lot = self._make_lot(lot_name="Bag of gravel", species=None, user=self.user, date_end=timezone.now())
        self.assertIsNone(lot.unsold_lot_no_bap_reason)
        same = self._make_lot(lot_name="Sponge filter", species=None, user=self.user, date_end=timezone.now())
        self.assertEqual(same.unsold_lot_no_bap_reason, "not_long_enough", "the name rule is what catches these")

    def test_sold_lot_no_bap_reason_not_sold(self):
        self.club.only_sold_lots = True
        self.club.save(update_fields=["only_sold_lots"])
        lot = self._make_lot(winning_price=None, auctiontos_winner=None)
        self.assertEqual(lot.sold_lot_no_bap_reason, "not_sold")

    def test_sold_lot_no_bap_reason_unsold_eligible_when_only_sold_lots_off(self):
        lot = self._make_lot(winning_price=None, auctiontos_winner=None)
        self.assertIsNone(lot.sold_lot_no_bap_reason)

    def test_auto_award_bap_points_awards_category_points(self):
        lot = self._make_lot()
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, self.category.bap_points)
        self.assertEqual(lot.bap_auto_reason, "")

    def test_auto_award_bap_points_skipped_when_bap_disabled(self):
        self.club.enable_breeder_award_program = False
        self.club.save()
        lot = self._make_lot()
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 0)
        self.assertEqual(lot.bap_auto_reason, "not_eligible")

    def test_auto_award_uses_club_points_per_lot_when_set(self):
        self.club.points_per_lot = 12
        self.club.save()
        lot = self._make_lot()
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 12)

    def test_backfill_bap_reasons_command_updates_only_ineligible_lots(self):
        eligible = self._make_lot(lot_name="Eligible fish")
        ineligible = self._make_lot(lot_name="Not bred fish", i_bred_this_fish=False)
        ineligible.bap_auto_reason = ""
        ineligible.save(update_fields=["bap_auto_reason"])

        call_command("backfill_bap_reasons")

        eligible.refresh_from_db()
        ineligible.refresh_from_db()
        self.assertEqual(eligible.bap_auto_reason, "")
        self.assertEqual(ineligible.bap_auto_reason, "not_bred")

    def test_backfill_bap_reasons_command_skips_unsold_lots(self):
        sold_ineligible = self._make_lot(lot_name="Sold not bred fish", i_bred_this_fish=False)
        unsold = self._make_lot(lot_name="Unsold fish", winning_price=None, auctiontos_winner=None)

        call_command("backfill_bap_reasons")

        sold_ineligible.refresh_from_db()
        unsold.refresh_from_db()
        self.assertEqual(sold_ineligible.bap_auto_reason, "not_bred")
        self.assertEqual(unsold.bap_auto_reason, "")

    def test_not_donation_when_only_donation_lots_required(self):
        self.club.only_donation_lots = True
        self.club.save()
        lot = self._make_lot(donation=False)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_donation")

    def test_donation_lot_passes_only_donation_check(self):
        self.club.only_donation_lots = True
        self.club.save()
        lot = self._make_lot(donation=True)
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_live_food_cultures_ineligible_when_cap_disabled(self):
        culture_cat = Category.objects.create(name="Live food cultures", bap_points=5)
        self.club.separate_cap = False
        self.club.save()
        lot = self._make_lot(species_category=culture_cat)
        self.assertEqual(lot.unsold_lot_no_bap_reason, "category_not_eligible")

    def test_live_food_cultures_eligible_when_cap_enabled(self):
        culture_cat = Category.objects.create(name="Live food cultures", bap_points=5)
        self.club.separate_cap = True
        self.club.save()
        lot = self._make_lot(species_category=culture_cat)
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_hap_category_bypasses_min_quantity(self):
        plant_cat = Category.objects.create(name="Aquatic plants", bap_points=5)
        self.club.min_quantity = 10
        self.club.save()
        lot = self._make_lot(species_category=plant_cat, quantity=1)
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_snails_bypass_min_quantity(self):
        snail_cat = Category.objects.create(name="Snails and other inverts", bap_points=5)
        self.club.min_quantity = 10
        self.club.save()
        lot = self._make_lot(species_category=snail_cat, quantity=1)
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_not_active_member_when_membership_expired(self):
        self.club.only_active_members_can_participate = True
        self.club.save()
        self.member.membership_expiration_date = timezone.now().date() - datetime.timedelta(days=1)
        self.member.save()
        lot = self._make_lot()
        self.assertEqual(lot.unsold_lot_no_bap_reason, "not_active_member")

    def test_active_member_passes_membership_check(self):
        self.club.only_active_members_can_participate = True
        self.club.save()
        self.member.membership_expiration_date = timezone.now().date() + datetime.timedelta(days=30)
        self.member.save()
        lot = self._make_lot()
        self.assertIsNone(lot.unsold_lot_no_bap_reason)

    def test_bap_placeholder_returns_hap_for_aquatic_plants(self):
        plant_cat = Category.objects.create(name="Aquatic plants", bap_points=5)
        self.club.separate_hap = True
        self.club.save()
        lot = self._make_lot(species_category=plant_cat)
        self.assertEqual(lot.bap_placeholder, "HAP")

    def test_bap_placeholder_returns_culture_for_live_food(self):
        culture_cat = Category.objects.create(name="Live food cultures", bap_points=5)
        self.club.separate_cap = True
        self.club.save()
        lot = self._make_lot(species_category=culture_cat)
        self.assertEqual(lot.bap_placeholder, "Culture")

    def test_bap_placeholder_returns_bap_by_default(self):
        lot = self._make_lot()
        self.assertEqual(lot.bap_placeholder, "BAP")

    def test_auto_award_uses_category_override_points(self):
        ClubBapCategoryOverride.objects.create(club=self.club, category=self.category, points=20)
        lot = self._make_lot()
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 20)

    def test_auto_award_adds_bonus_points_when_custom_checkbox_set(self):
        self.club.points_for_custom_checkbox = 3
        self.club.save()
        lot = self._make_lot(custom_checkbox=True)
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, self.category.bap_points + 3)

    def test_auto_award_no_bonus_when_custom_checkbox_not_set(self):
        self.club.points_for_custom_checkbox = 3
        self.club.save()
        lot = self._make_lot(custom_checkbox=False)
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, self.category.bap_points)

    def test_auto_award_skipped_when_award_already_exists(self):
        lot = self._make_lot()
        BapAward.objects.create(club_member=self.member, date=timezone.now().date(), lot=lot, points=99)
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertEqual(lot.bap_points_awarded, 0)

    def test_auto_award_no_award_created_when_manual_approval_required(self):
        self.club.auto_add_points = False
        self.club.save()
        lot = self._make_lot()
        lot.auto_award_bap_points()
        lot.refresh_from_db()
        self.assertFalse(BapAward.objects.filter(lot=lot).exists())
        self.assertEqual(lot.bap_auto_reason, "")

    def test_auto_award_routes_hap_points_for_plant_category(self):
        plant_cat = Category.objects.create(name="Aquatic plants", bap_points=7)
        self.club.separate_hap = True
        self.club.points_per_lot = 7
        self.club.save()
        lot = self._make_lot(species_category=plant_cat)
        lot.auto_award_bap_points()
        award = BapAward.objects.get(lot=lot)
        self.assertEqual(award.hap_points, 7)
        self.assertEqual(award.points, 0)
        self.assertEqual(award.cap_points, 0)


class AuctionCalendarButtonTests(StandardTestCase):
    """The auction page's add-to-calendar control renders a web dropdown, but a single native
    button inside the mobile app (and never leaks raw calendar JS onto the page)."""

    def setUp(self):
        super().setUp()
        # Keep the auction current so the join card / pickup block render normally.
        self.online_auction.date_start = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()
        self.location.pickup_time = timezone.now() + datetime.timedelta(days=3)
        self.location.save()

    def _get(self, user_agent):
        self.client.force_login(self.user)
        return self.client.get(self.online_auction.url, HTTP_USER_AGENT=user_agent).content.decode("utf-8")

    def test_web_shows_provider_dropdown(self):
        html = self._get("Mozilla/5.0")
        self.assertIn("Add to calendar", html)
        self.assertIn("Google Calendar", html)  # the web provider menu
        self.assertNotIn("fishAddToCalendar", html)  # native bridge only in the app

    def test_app_shows_single_native_button_not_dropdown(self):
        html = self._get("FishAuctionsApp/1.0 (iOS)")
        self.assertIn("Add to calendar", html)
        self.assertIn("fishAddToCalendar", html)  # native single-button bridge
        self.assertNotIn("Google Calendar", html)  # no web provider menu in the app

    def test_map_info_window_fragment_has_no_script_tag(self):
        # Regression: the map info-window interpolates a location fragment into a JS backtick
        # string. In the app the old fragment emitted a <script>, whose </script> prematurely
        # closed the map script and dumped initMap onto the page as visible text. The map
        # fragment must never contain a script tag.
        from django.template.loader import render_to_string

        rendered = render_to_string(
            "location_fragment_map.html",
            {"location": self.location},
        )
        self.assertNotIn("<script", rendered)
        self.assertNotIn("</script>", rendered)


class EndauctionsPrettyMuchOverTests(TestCase):
    """Tests for endauctions.deactivate_pretty_much_over_lots: deactivation + unsold BAP awards."""

    def setUp(self):
        self.user = User.objects.create_user(username="pmo_seller", password="testpass", email="pmo@example.com")
        self.club = Club.objects.create(
            name="PMO Club",
            enable_breeder_award_program=True,
            auto_add_points=True,
            only_sold_lots=False,
            min_quantity=1,
            points_per_lot=5,
        )
        self.category = Category.objects.create(name="PMO Livebearers", bap_points=5)
        self.member = ClubMember.objects.create(club=self.club, user=self.user)
        # In-person auction started well over 24h ago -> pretty_much_over.
        self.over_auction = Auction.objects.create(
            title="Over Auction",
            created_by=self.user,
            club=self.club,
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=3),
        )
        self.location = PickupLocation.objects.create(
            name="pmo loc", auction=self.over_auction, pickup_time=timezone.now() - datetime.timedelta(days=2)
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.over_auction, pickup_location=self.location)

    def _make_lot(self, **kwargs):
        defaults = {
            "lot_name": "PMO Guppies",
            "auction": self.over_auction,
            "auctiontos_seller": self.tos,
            "quantity": 5,
            "i_bred_this_fish": True,
            "species_category": self.category,
            "active": True,
        }
        defaults.update(kwargs)
        return Lot.objects.create(**defaults)

    def test_active_lots_deactivated_when_pretty_much_over(self):
        from auctions.management.commands.endauctions import deactivate_pretty_much_over_lots

        lot = self._make_lot()
        self.assertTrue(lot.active)
        deactivate_pretty_much_over_lots()
        lot.refresh_from_db()
        self.assertFalse(lot.active)

    def test_current_auction_lots_stay_active(self):
        from auctions.management.commands.endauctions import deactivate_pretty_much_over_lots

        current_auction = Auction.objects.create(
            title="Current Auction",
            created_by=self.user,
            club=self.club,
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(hours=1),
        )
        current_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=current_auction,
            pickup_location=PickupLocation.objects.create(
                name="cur", auction=current_auction, pickup_time=timezone.now() + datetime.timedelta(days=1)
            ),
        )
        lot = Lot.objects.create(
            lot_name="Current lot", auction=current_auction, auctiontos_seller=current_tos, quantity=5, active=True
        )
        deactivate_pretty_much_over_lots()
        lot.refresh_from_db()
        self.assertTrue(lot.active)

    def test_unsold_lot_awarded_bap_when_club_awards_unsold(self):
        from auctions.management.commands.endauctions import deactivate_pretty_much_over_lots

        lot = self._make_lot()  # unsold (no winner/price)
        self.assertFalse(lot.sold)
        deactivate_pretty_much_over_lots()
        lot.refresh_from_db()
        self.assertFalse(lot.active)
        self.assertEqual(lot.bap_points_awarded, self.category.bap_points)
        self.assertTrue(BapAward.objects.filter(lot=lot).exists())

    def test_unsold_lot_not_awarded_when_only_sold_lots(self):
        from auctions.management.commands.endauctions import deactivate_pretty_much_over_lots

        self.club.only_sold_lots = True
        self.club.save(update_fields=["only_sold_lots"])
        lot = self._make_lot()  # unsold
        deactivate_pretty_much_over_lots()
        lot.refresh_from_db()
        self.assertFalse(lot.active)  # still deactivated
        self.assertFalse(BapAward.objects.filter(lot=lot).exists())  # but no points

    def test_deactivated_lot_can_still_be_marked_sold(self):
        from auctions.management.commands.endauctions import deactivate_pretty_much_over_lots

        lot = self._make_lot()
        deactivate_pretty_much_over_lots()
        lot.refresh_from_db()
        self.assertFalse(lot.active)
        # sold is independent of active: set a winner + price and it reads as sold.
        lot.auctiontos_winner = self.tos
        lot.winning_price = 15
        lot.save()
        lot.refresh_from_db()
        self.assertTrue(lot.sold)


class LotFilterRegardingAuctionStatusTests(TestCase):
    """LotFilter.status must stay "open" (active=True lots only) by default when scoped to a
    still-running auction via regardingAuction/?auction=slug, unless the caller explicitly passes
    ?status= or the auction is already closed (filter_by_auction already forces "all" once
    auction.closed is True -- that part is existing, intentional behavior and out of scope here).

    This covers the /lots/?auction=slug path used by AllLots (e.g. the "last auction you used"
    redirect in ToDefaultLandingPage) and AuctionInfo's embedded lot list, while the auction is
    still live. Forcing status="all" unconditionally for any regardingAuction scope would make
    already-ended lots reappear in that default view even for a running auction, which is the
    exact regression a prior "fix lot list showing closed lots" commit avoided."""

    def setUp(self):
        self.user = User.objects.create_user(username="lf_seller", password="testpass", email="lf@example.com")
        self.auction = Auction.objects.create(
            title="LF Auction",
            created_by=self.user,
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        self.location = PickupLocation.objects.create(
            name="lf loc", auction=self.auction, pickup_time=timezone.now() + datetime.timedelta(days=2)
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)
        self.active_lot = Lot.objects.create(
            lot_name="Active lot", auction=self.auction, auctiontos_seller=self.tos, quantity=1, active=True
        )
        self.ended_lot = Lot.objects.create(
            lot_name="Ended lot", auction=self.auction, auctiontos_seller=self.tos, quantity=1, active=False
        )
        # LotFilter excludes lots posted in the last 20 minutes ("very new lot") for online auctions;
        # backdate so both lots are eligible to show up regardless of that unrelated exclusion.
        Lot.objects.filter(pk__in=[self.active_lot.pk, self.ended_lot.pk]).update(
            date_posted=timezone.now() - datetime.timedelta(hours=1)
        )
        self.assertFalse(self.auction.closed)

    def test_all_lots_view_scoped_to_auction_hides_ended_lots_by_default(self):
        response = self.client.get(reverse("allLots"), {"auction": self.auction.slug})
        self.assertContains(response, "Active lot")
        self.assertNotContains(response, "Ended lot")

    def test_all_lots_view_scoped_to_auction_shows_ended_lots_with_explicit_status(self):
        response = self.client.get(reverse("allLots"), {"auction": self.auction.slug, "status": "all"})
        self.assertContains(response, "Active lot")
        self.assertContains(response, "Ended lot")


class BapAwardRecalculateTests(TestCase):
    """Tests for BapAward.recalculate_member_points and its save/delete hooks."""

    def setUp(self):
        self.user = User.objects.create_user(username="recalc_user", password="testpass", email="recalc@example.com")
        self.club = Club.objects.create(name="Recalc Club", enable_breeder_award_program=True)
        self.member = ClubMember.objects.create(club=self.club, user=self.user)

    def test_save_updates_member_bap_points(self):
        BapAward.objects.create(club_member=self.member, date=timezone.now().date(), points=10)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bap_points, 10)

    def test_delete_resets_member_bap_points(self):
        award = BapAward.objects.create(club_member=self.member, date=timezone.now().date(), points=10)
        award.delete()
        self.member.refresh_from_db()
        self.assertEqual(self.member.bap_points, 0)

    def test_ytd_points_counted_for_current_year_only(self):
        BapAward.objects.create(club_member=self.member, date=timezone.now().date(), points=5)
        BapAward.objects.create(club_member=self.member, date=datetime.date(2019, 1, 1), points=3)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bap_points, 8)
        self.assertEqual(self.member.bap_points_ytd, 5)

    def test_hap_points_tracked_separately_from_bap(self):
        BapAward.objects.create(club_member=self.member, date=timezone.now().date(), points=0, hap_points=4)
        self.member.refresh_from_db()
        self.assertEqual(self.member.hap_points, 4)
        self.assertEqual(self.member.bap_points, 0)


class BapBackfillMigrationTests(TestCase):
    def test_0273_migration_has_no_operations(self):
        # Numeric migration module names require dynamic import syntax.
        migration_module = importlib.import_module("auctions.migrations.0273_backfill_bap_auto_reason")
        self.assertEqual(migration_module.Migration.operations, [])


class ClubBapLotsViewTests(TestCase):
    """Permission and basic access tests for ClubBapLotsView."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="bap_lots_owner", password="testpass", email="bap_lots_owner@example.com"
        )
        self.club = Club.objects.create(name="BAP Lots Club", enable_breeder_award_program=True)
        self.bap_user = User.objects.create_user(
            username="bap_lots_user", password="testpass", email="bap_lots_user@example.com"
        )
        self.bap_member = ClubMember.objects.create(club=self.club, user=self.bap_user, permission_manage_bap=True)
        self.plain_user = User.objects.create_user(
            username="bap_lots_plain", password="testpass", email="bap_lots_plain@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.plain_user)
        self.seller_user = User.objects.create_user(
            username="mike_seller",
            password="testpass",
            email="mike@example.com",
            first_name="Mike",
            last_name="Smith",
        )
        self.buyer_user = User.objects.create_user(username="bap_lots_buyer", password="testpass")
        self.seller_member = ClubMember.objects.create(
            club=self.club,
            user=self.seller_user,
            name="Mike Smith",
            email="mike@example.com",
        )
        self.category = Category.objects.create(name="Foo Bar", bap_points=5)
        self.other_category = Category.objects.create(name="Egglayers", bap_points=4)
        self.auction = Auction.objects.create(
            created_by=self.owner,
            club=self.club,
            title="Spring Auction",
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=1),
            winning_bid_percent_to_club=25,
            lot_entry_fee=0,
            unsold_lot_fee=0,
            tax=0,
        )
        self.other_auction = Auction.objects.create(
            created_by=self.owner,
            club=self.club,
            title="Summer Auction",
            date_start=timezone.now() - datetime.timedelta(days=6),
            date_end=timezone.now() - datetime.timedelta(days=4),
            winning_bid_percent_to_club=25,
            lot_entry_fee=0,
            unsold_lot_fee=0,
            tax=0,
        )
        location = PickupLocation.objects.create(
            name="Club Hall",
            auction=self.auction,
            pickup_time=timezone.now() + datetime.timedelta(days=1),
        )
        other_location = PickupLocation.objects.create(
            name="Club Hall 2",
            auction=self.other_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=2),
        )
        self.seller_tos = AuctionTOS.objects.create(
            user=self.seller_user, auction=self.auction, pickup_location=location
        )
        self.other_seller_tos = AuctionTOS.objects.create(
            user=self.seller_user, auction=self.other_auction, pickup_location=other_location
        )
        self.buyer_tos = AuctionTOS.objects.create(user=self.buyer_user, auction=self.auction, pickup_location=location)
        self.other_buyer_tos = AuctionTOS.objects.create(
            user=self.buyer_user, auction=self.other_auction, pickup_location=other_location
        )
        self.pending_lot = Lot.objects.create(
            lot_name="Pending Foo Lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            auctiontos_winner=self.buyer_tos,
            active=False,
            winning_price=Decimal("12.00"),
            quantity=1,
            i_bred_this_fish=True,
            species_category=self.category,
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        self.approved_lot = Lot.objects.create(
            lot_name="Approved Egg Lot",
            auction=self.other_auction,
            auctiontos_seller=self.other_seller_tos,
            auctiontos_winner=self.other_buyer_tos,
            active=False,
            winning_price=Decimal("15.00"),
            quantity=1,
            i_bred_this_fish=True,
            species_category=self.other_category,
            date_end=timezone.now() - datetime.timedelta(days=2),
        )
        self.rejected_lot = Lot.objects.create(
            lot_name="Rejected Lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            auctiontos_winner=self.buyer_tos,
            active=False,
            winning_price=Decimal("10.00"),
            quantity=1,
            i_bred_this_fish=True,
            species_category=self.other_category,
            manually_approved=True,
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        BapAward.objects.create(
            club_member=self.seller_member, date=timezone.now().date(), lot=self.approved_lot, points=5
        )
        self.url = reverse("club_bap_lots", kwargs={"slug": self.club.slug})

    def test_anon_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_plain_member_gets_403(self):
        self.client.login(username="bap_lots_plain", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_bap_admin_can_access(self):
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_bap_disabled_returns_404(self):
        self.club.enable_breeder_award_program = False
        self.club.save()
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_query_pending_filter(self):
        """Pending filter shows only sold lots without manually_approved=True."""
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(self.url, {"query": "pending"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending Foo Lot")
        self.assertNotContains(response, "Approved Egg Lot")

    def test_query_approved_filter(self):
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(self.url, {"query": "approved"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approved Egg Lot")
        self.assertNotContains(response, "Pending Foo Lot")

    def test_default_shows_pending_without_query(self):
        """No query param should still return 200 (defaults to pending filter)."""
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending Foo Lot")
        self.assertNotContains(response, "Approved Egg Lot")

    def test_query_supports_user_category_and_auction_keywords(self):
        self.client.login(username="bap_lots_user", password="testpass")
        response = self.client.get(
            self.url, {"query": 'pending user:"Mike Smith" category:"Foo Bar" auction:spring-auction'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending Foo Lot")
        self.assertNotContains(response, "Approved Egg Lot")
        self.assertNotContains(response, "Rejected Lot")

    def test_category_badge_modal_updates_lot_category(self):
        self.client.login(username="bap_lots_user", password="testpass")
        url = reverse("club_bap_lot_category", kwargs={"pk": self.pending_lot.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set category")
        response = self.client.post(url, {"species_category": self.other_category.pk})
        self.assertEqual(response.status_code, 200)
        self.pending_lot.refresh_from_db()
        self.assertEqual(self.pending_lot.species_category, self.other_category)
        self.assertContains(response, "bapLotListChanged")

    # --- the three buttons on each row --------------------------------------
    #
    # These went through ``services.review_lot_points`` when ``review_points`` needed to press them
    # without being a browser, and until then nothing tested them at all.

    def _press(self, lot, action, **extra):
        return self.client.post(reverse("lot_bap_points", kwargs={"pk": lot.pk}), {"action": action, **extra})

    def test_approving_creates_the_award_and_a_history_line(self):
        self.client.login(username="bap_lots_user", password="testpass")
        response = self._press(self.pending_lot, "approve", bap_points=7)
        self.assertEqual(response.status_code, 200)
        award = BapAward.objects.get(lot=self.pending_lot)
        self.assertEqual(award.points, 7)
        self.assertEqual(award.club_member, self.seller_member)
        self.pending_lot.refresh_from_db()
        self.assertTrue(self.pending_lot.manually_approved)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, applies_to="BAP", action__startswith="Awarded"))

    def test_rejecting_leaves_no_award(self):
        self.client.login(username="bap_lots_user", password="testpass")
        self._press(self.pending_lot, "reject")
        self.assertFalse(BapAward.objects.filter(lot=self.pending_lot).exists())
        self.pending_lot.refresh_from_db()
        self.assertTrue(self.pending_lot.manually_approved)

    def test_undo_puts_the_lot_back_and_says_so_in_the_history(self):
        self.client.login(username="bap_lots_user", password="testpass")
        self._press(self.approved_lot, "undo")
        self.assertFalse(BapAward.objects.filter(lot=self.approved_lot).exists())
        self.approved_lot.refresh_from_db()
        self.assertFalse(self.approved_lot.manually_approved)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, applies_to="BAP", action__startswith="Undid"))

    def test_a_plain_member_cannot_press_anything(self):
        self.client.login(username="bap_lots_plain", password="testpass")
        response = self._press(self.pending_lot, "approve", bap_points=7)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BapAward.objects.filter(lot=self.pending_lot).exists())

    def test_the_default_the_button_offers_follows_the_genus_rule(self):
        """It read the category override and not the genus one, so the row re-rendered with a
        number the table had never shown."""
        species = Species.objects.create(scientific_name="Tropheus moorii", genus="Tropheus", species="moorii")
        self.pending_lot.species = species
        self.pending_lot.save()
        ClubBapGenusOverride.objects.create(club=self.club, genus="Tropheus", points=15)
        self.client.login(username="bap_lots_user", password="testpass")
        response = self._press(self.pending_lot, "undo")
        self.assertContains(response, 'name="bap_points" value="15"')


class ClubAPIKeyModelTests(TestCase):
    """Unit tests for ClubAPIKey.generate() and ClubAPIKey.verify()."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="apikey_owner", password="testpass", email="apikey_owner@example.com"
        )
        self.club = Club.objects.create(name="API Key Club")

    def _make_key(self):
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Test Key", prefix=prefix, key_hash=key_hash, created_by=self.owner
        )
        return raw_key, api_key

    def test_generate_prefix_starts_with_ck(self):
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.assertTrue(prefix.startswith("ck_"))

    def test_generate_raw_key_contains_prefix(self):
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.assertTrue(raw_key.startswith(prefix + "."))

    def test_verify_returns_key_for_valid_raw_key(self):
        raw_key, api_key = self._make_key()
        found = ClubAPIKey.verify(raw_key)
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, api_key.pk)

    def test_verify_returns_none_for_wrong_secret(self):
        raw_key, api_key = self._make_key()
        prefix = api_key.prefix
        result = ClubAPIKey.verify(f"{prefix}.wrongsecretvalue")
        self.assertIsNone(result)

    def test_verify_returns_none_for_inactive_key(self):
        raw_key, api_key = self._make_key()
        api_key.is_active = False
        api_key.save()
        self.assertIsNone(ClubAPIKey.verify(raw_key))

    def test_verify_returns_none_for_malformed_key(self):
        self.assertIsNone(ClubAPIKey.verify("nodotseparator"))
        self.assertIsNone(ClubAPIKey.verify(""))


class ClubMemberCreateAPITests(TestCase):
    """Integration tests for POST /api/v1/clubs/<slug>/members/ via API key."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="ingest_owner", password="testpass", email="ingest_owner@example.com"
        )
        self.club = Club.objects.create(name="Ingest Club")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Test Integration",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.owner,
            can_add_club_members=True,
        )
        self.raw_key = raw_key
        self.url = reverse("api_club_members", kwargs={"slug": self.club.slug})

    def _post(self, data, key=None):
        headers = {}
        if key is not False:
            headers["HTTP_X_API_KEY"] = key or self.raw_key
        return self.client.post(self.url, data, content_type="application/json", **headers)

    def test_no_api_key_returns_401(self):
        response = self._post({"email": "test@example.com"}, key=False)
        self.assertEqual(response.status_code, 401)

    def test_bad_api_key_returns_401(self):
        response = self._post({"email": "test@example.com"}, key="ck_bad.wrong")
        self.assertEqual(response.status_code, 401)

    def test_wrong_slug_returns_403(self):
        other_club = Club.objects.create(name="Other Club")
        wrong_url = reverse("api_club_members", kwargs={"slug": other_club.slug})
        response = self.client.post(
            wrong_url, {"email": "test@example.com"}, content_type="application/json", HTTP_X_API_KEY=self.raw_key
        )
        self.assertEqual(response.status_code, 403)

    def test_valid_key_creates_member(self):
        response = self._post({"email": "new@example.com", "first_name": "Alice"})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["email"], "new@example.com")
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="new@example.com").exists())

    def test_add_member_permission_required(self):
        self.api_key.can_add_club_members = False
        self.api_key.save(update_fields=["can_add_club_members"])
        response = self._post({"email": "blocked@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ClubMember.objects.filter(club=self.club, email="blocked@example.com").exists())

    def test_email_lowercased_on_creation(self):
        response = self._post({"email": "UPPER@Example.COM"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="upper@example.com").exists())

    def test_invalid_payload_returns_400(self):
        response = self._post({})
        self.assertEqual(response.status_code, 400)

    def test_club_history_created_on_success(self):
        before = ClubHistory.objects.filter(club=self.club).count()
        self._post({"email": "hist@example.com"})
        self.assertEqual(ClubHistory.objects.filter(club=self.club).count(), before + 1)

    def test_field_mapping_applied(self):
        ClubAPIKeyFieldMap.objects.create(api_key=self.api_key, external_field="email_address", internal_field="email")
        response = self._post({"email_address": "mapped@example.com"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ClubMember.objects.filter(club=self.club, email="mapped@example.com").exists())

    def test_last_used_at_updated(self):
        self.assertIsNone(self.api_key.last_used_at)
        self._post({"email": "lastusedat@example.com"})
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)

    def test_last_used_at_updated_on_get(self):
        """last_used_at is touched even on read-only GET requests."""
        self.api_key.can_read_club_member_list = True
        self.api_key.save(update_fields=["can_read_club_member_list"])
        self.assertIsNone(self.api_key.last_used_at)
        self.client.get(self.url, HTTP_X_API_KEY=self.raw_key)
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)

    def test_member_source_is_api_key_name(self):
        self._post({"email": "source@example.com"})
        member = ClubMember.objects.get(club=self.club, email="source@example.com")
        self.assertEqual(member.source, self.api_key.name)

    def test_source_cannot_be_overridden_by_caller(self):
        """Clients cannot set source — it is always the API key name."""
        self._post({"email": "srcoverride@example.com", "source": "hacked"})
        member = ClubMember.objects.get(club=self.club, email="srcoverride@example.com")
        self.assertEqual(member.source, self.api_key.name)

    def test_first_name_alias_stored_as_name(self):
        """first_name passed via API is stored on the single ``name`` field."""
        self._post({"email": "alice@example.com", "first_name": "Alice"})
        member = ClubMember.objects.get(club=self.club, email="alice@example.com")
        self.assertEqual(member.name, "Alice")

    def test_first_and_last_name_combined_into_name(self):
        """first_name + last_name are combined into a single ``name``."""
        self._post({"email": "bobsmith@example.com", "first_name": "Bob", "last_name": "Smith"})
        member = ClubMember.objects.get(club=self.club, email="bobsmith@example.com")
        self.assertEqual(member.name, "Bob Smith")

    def test_single_name_field_stored_directly(self):
        """A single ``name`` field is stored verbatim."""
        self._post({"email": "carol@example.com", "name": "Carol Q Smith"})
        member = ClubMember.objects.get(club=self.club, email="carol@example.com")
        self.assertEqual(member.name, "Carol Q Smith")

    def test_mapped_first_name_combined_with_last_name(self):
        """Field mappings can rename external fields to first_name/last_name; the result is one name."""
        ClubAPIKeyFieldMap.objects.create(api_key=self.api_key, external_field="given", internal_field="first_name")
        ClubAPIKeyFieldMap.objects.create(api_key=self.api_key, external_field="surname", internal_field="last_name")
        self._post({"email": "mapped@example.com", "given": "Mapped", "surname": "User"})
        member = ClubMember.objects.get(club=self.club, email="mapped@example.com")
        self.assertEqual(member.name, "Mapped User")

    def test_filter_by_name_param(self):
        """GET ?name=alice returns only matching members."""
        self.api_key.can_read_club_member_list = True
        self.api_key.save(update_fields=["can_read_club_member_list"])
        ClubMember.objects.create(club=self.club, name="Alice Smith", email="alice@example.com")
        ClubMember.objects.create(club=self.club, name="Bob Jones", email="bob@example.com")
        response = self.client.get(self.url + "?name=alice", HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 200)
        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["email"], "alice@example.com")

    def test_filter_by_filter_param(self):
        """GET ?filter=<token> applies the same search logic as the admin member list."""
        self.api_key.can_read_club_member_list = True
        self.api_key.save(update_fields=["can_read_club_member_list"])
        ClubMember.objects.create(club=self.club, name="Alice Smith", email="alice@example.com")
        ClubMember.objects.create(club=self.club, name="Bob Jones", email="bob@example.com")
        response = self.client.get(self.url + "?filter=alice", HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 200)
        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["email"], "alice@example.com")


class OrphanColumnRepairTests(TransactionTestCase):
    """A column no model describes can stop a table taking rows at all.

    Live databases collected several of these from feature branches migrated against them and
    then abandoned -- ``auctions_clubapikey.can_add_species``, ``auctions_club.enable_event_rsvp``
    and ``auctions_clubevent.rsvp_enabled`` among them.  ``NOT NULL`` with no default, each one
    turned every insert on its table into ``IntegrityError (1364)``: creating an API key, a club
    or an event was a 500 with no workaround anywhere in application code.  Migration 0418 drops
    them, and drops only the fatal ones.
    """

    TABLE = "auctions_clubapikey"

    def _repair(self):
        import importlib

        from django.apps import apps
        from django.db import connection

        module = importlib.import_module("auctions.migrations.0418_drop_orphan_columns")
        with connection.schema_editor() as schema_editor:
            module.drop_orphan_columns(apps, schema_editor)

    def _columns(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                [self.TABLE],
            )
            return {row[0] for row in cursor.fetchall()}

    def _add_column(self, definition):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {self.TABLE} ADD COLUMN {definition}")

    def _drop_column(self, column):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {self.TABLE} DROP COLUMN {column}")

    def test_a_fatal_orphan_is_dropped_and_the_table_takes_rows_again(self):
        from django.db import IntegrityError, transaction

        club = Club.objects.create(name="Orphan Column Club")
        before = self._columns()
        self._add_column("can_add_species tinyint(1) NOT NULL")
        try:
            with self.assertRaises(IntegrityError), transaction.atomic():
                ClubAPIKey.objects.create(club=club, name="k", prefix="ck_orph1", key_hash="x")
            self._repair()
            self.assertEqual(self._columns(), before)
        finally:
            if "can_add_species" in self._columns():
                self._drop_column("can_add_species")
        ClubAPIKey.objects.create(club=club, name="k", prefix="ck_orph2", key_hash="x")

    def test_an_inert_orphan_is_left_alone(self):
        """Nullable, or carrying a default, means nothing is blocked -- and dropping it would be
        the one version of this that could lose data."""
        self._add_column("leftover_note varchar(20) NULL")
        self._add_column("leftover_flag tinyint(1) NOT NULL DEFAULT 0")
        try:
            self._repair()
            self.assertIn("leftover_note", self._columns())
            self.assertIn("leftover_flag", self._columns())
        finally:
            self._drop_column("leftover_note")
            self._drop_column("leftover_flag")

    def test_it_is_a_no_op_on_a_database_built_from_the_migrations(self):
        before = self._columns()
        self.assertNotIn("can_add_species", before)
        self._repair()
        self.assertEqual(self._columns(), before)


class ClubAPIKeyUITests(TestCase):
    """Permission and basic access tests for API key management UI views."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="apiui_owner", password="testpass", email="apiui_owner@example.com"
        )
        self.club = Club.objects.create(name="API UI Club")
        self.editor = User.objects.create_user(
            username="apiui_editor", password="testpass", email="apiui_editor@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.editor, permission_edit_club=True)
        self.plain = User.objects.create_user(
            username="apiui_plain", password="testpass", email="apiui_plain@example.com"
        )
        ClubMember.objects.create(club=self.club, user=self.plain)
        self.list_url = reverse("club_api_keys", kwargs={"slug": self.club.slug})
        self.create_url = reverse("club_api_key_create", kwargs={"slug": self.club.slug})

    def test_anon_list_redirects_to_login(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_plain_member_list_gets_403(self):
        self.client.login(username="apiui_plain", password="testpass")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_editor_can_access_list(self):
        self.client.login(username="apiui_editor", password="testpass")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)

    def test_editor_can_access_detail_and_raw_key_is_one_time(self):
        self.client.login(username="apiui_editor", password="testpass")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Detail Key", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        session = self.client.session
        session[f"new_api_key_{api_key.pk}"] = raw_key
        session.save()
        detail_url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, raw_key)
        response = self.client.get(detail_url)
        self.assertNotContains(response, raw_key)

    def test_editor_can_create_key(self):
        self.client.login(username="apiui_editor", password="testpass")
        response = self.client.post(self.create_url, {"name": "My Integration"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClubAPIKey.objects.filter(club=self.club, name="My Integration").exists())

    def test_create_key_permission_defaults(self):
        self.client.login(username="apiui_editor", password="testpass")
        self.client.post(self.create_url, {"name": "Default Permissions"})
        api_key = ClubAPIKey.objects.get(club=self.club, name="Default Permissions")
        self.assertTrue(api_key.can_add_club_members)
        self.assertFalse(api_key.can_read_club_member_list)
        self.assertFalse(api_key.can_update_club_members)
        self.assertFalse(api_key.can_add_bap_points)

    def test_create_key_can_enable_all_extended_permissions(self):
        self.client.login(username="apiui_editor", password="testpass")
        self.client.post(
            self.create_url,
            {
                "name": "Everything Key",
                "can_add_club_members_present": "1",
                "can_add_club_members": "on",
                "can_read_club_member_list_present": "1",
                "can_read_club_member_list": "on",
                "can_update_club_members_present": "1",
                "can_update_club_members": "on",
                "can_add_bap_points_present": "1",
                "can_add_bap_points": "on",
            },
        )
        api_key = ClubAPIKey.objects.get(club=self.club, name="Everything Key")
        self.assertTrue(api_key.can_add_club_members)
        self.assertTrue(api_key.can_read_club_member_list)
        self.assertTrue(api_key.can_update_club_members)
        self.assertTrue(api_key.can_add_bap_points)

    def test_create_key_can_enable_the_read_only_auction_permissions(self):
        self.client.login(username="apiui_editor", password="testpass")
        self.client.post(
            self.create_url,
            {
                "name": "Website Key",
                "can_read_auction_info_present": "1",
                "can_read_auction_info": "on",
                "can_read_public_lots_present": "1",
                "can_read_public_lots": "on",
                "can_read_private_lots_present": "1",
            },
        )
        api_key = ClubAPIKey.objects.get(club=self.club, name="Website Key")
        self.assertTrue(api_key.can_read_auction_info)
        self.assertTrue(api_key.can_read_public_lots)
        # Unticked: the privacy flag is never on unless somebody said so.
        self.assertFalse(api_key.can_read_private_lots)

    def test_detail_page_documents_the_auction_endpoints_only_when_they_are_on(self):
        self.client.login(username="apiui_editor", password="testpass")
        _, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Docs Key", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        detail_url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        self.assertNotContains(self.client.get(detail_url), f"/clubs/{self.club.slug}/auctions/")
        api_key.can_read_public_lots = True
        api_key.save(update_fields=["can_read_public_lots"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/api/v1/clubs/{self.club.slug}/auctions/")

    def test_create_redirects_to_detail_with_raw_key_in_session(self):
        self.client.login(username="apiui_editor", password="testpass")
        self.client.post(self.create_url, {"name": "Session Key"})
        api_key = ClubAPIKey.objects.get(club=self.club, name="Session Key")
        session = self.client.session
        self.assertIn(f"new_api_key_{api_key.pk}", session)

    def test_revoke_deactivates_key(self):
        self.client.login(username="apiui_editor", password="testpass")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Revoke Me", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        revoke_url = reverse("club_api_key_revoke", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        self.client.post(revoke_url)
        api_key.refresh_from_db()
        self.assertFalse(api_key.is_active)

    def test_add_mapping(self):
        self.client.login(username="apiui_editor", password="testpass")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Map Me", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        add_url = reverse("club_api_key_mapping_add", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        self.client.post(add_url, {"external_field": "email_address", "internal_field": "email"})
        self.assertTrue(ClubAPIKeyFieldMap.objects.filter(api_key=api_key, external_field="email_address").exists())

    def test_delete_mapping(self):
        self.client.login(username="apiui_editor", password="testpass")
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Del Map", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        mapping = ClubAPIKeyFieldMap.objects.create(
            api_key=api_key, external_field="fullname", internal_field="first_name"
        )
        del_url = reverse(
            "club_api_key_mapping_delete", kwargs={"slug": self.club.slug, "pk": api_key.pk, "map_pk": mapping.pk}
        )
        self.client.post(del_url)
        self.assertFalse(ClubAPIKeyFieldMap.objects.filter(pk=mapping.pk).exists())

    def test_invalid_mapping_is_ignored(self):
        self.client.login(username="apiui_editor", password="testpass")
        _, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Ignore Bad Mapping", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        add_url = reverse("club_api_key_mapping_add", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        self.client.post(add_url, {"external_field": "mystery", "internal_field": "not_a_field"})
        self.assertFalse(ClubAPIKeyFieldMap.objects.filter(api_key=api_key, external_field="mystery").exists())

    def test_plain_member_cannot_access_detail(self):
        self.client.login(username="apiui_plain", password="testpass")
        _, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club, name="Blocked Detail", prefix=prefix, key_hash=key_hash, created_by=self.editor
        )
        detail_url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 403)

    def test_detail_shows_extended_example_endpoints(self):
        self.client.login(username="apiui_editor", password="testpass")
        _, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club,
            name="Examples",
            prefix=prefix,
            key_hash=key_hash,
            created_by=self.editor,
            can_read_club_member_list=True,
            can_update_club_members=True,
            can_add_bap_points=True,
        )
        member = ClubMember.objects.create(club=self.club, name="Endpoint Example")
        detail_url = reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk})
        response = self.client.get(detail_url)
        self.assertContains(response, f"/api/v1/clubs/{self.club.slug}/members/")
        self.assertContains(response, f"/api/v1/clubs/{self.club.slug}/members/{member.pk}/")
        self.assertContains(response, f"/api/v1/clubs/{self.club.slug}/members/{member.pk}/bap-awards/")
        self.assertContains(response, f"/api/v1/clubs/{self.club.slug}/bap-lots/")
        self.assertContains(response, "lot_number_display")
        self.assertContains(response, "lot_id")
        self.assertContains(response, "bap_eligible")
