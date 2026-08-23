"""Tests for what the command palette assistant can *do*.

The first test is the important one, and like the route audit it isn't really a test of behaviour:
it is the rule that every capability on this site is either a palette skill or something somebody
has written down a reason for skipping.

``auctions/test_palette_routes.py`` already guarantees the assistant can reach every *page*. That
turned out to be half a guarantee: a URL that adds a club member is not a page, so the route audit
excused it as a "JSON/HTMX endpoint" and everyone moved on -- and the assistant went on not knowing
how to add a club member. This is the other half.
"""

import datetime
import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import palette_actions
from auctions.models import (
    AssistantSkillRequest,
    Auction,
    AuctionTOS,
    Club,
    ClubMember,
    Invoice,
    Lot,
    LotImage,
    PickupLocation,
    Species,
    SpeciesCommonName,
    SpeciesSearchCache,
    Watch,
)
from auctions.test_species import make_species
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class SkillAuditTests(SimpleTestCase):
    """Every POST-able view is a skill, or has a written reason. No third state."""

    def test_every_capability_is_either_a_skill_or_has_a_written_reason(self):
        audit = palette_actions.audit_skills()
        self.assertEqual(
            audit["uncovered"],
            [],
            "These views accept a POST, so they are things the UI can do. They are in neither "
            "palette_actions.SKILLS nor NOT_A_SKILL, so the assistant can't do them and nobody has "
            "said why. Register an action and map it in SKILLS, or add a reason to NOT_A_SKILL.",
        )

    def test_no_table_entry_names_a_view_that_no_longer_exists(self):
        audit = palette_actions.audit_skills()
        self.assertEqual(audit["stale"], [], "These views are in the skill tables but not in the URLconf any more.")

    def test_every_named_skill_is_a_registered_action(self):
        audit = palette_actions.audit_skills()
        self.assertEqual(
            audit["unregistered"], [], "SKILLS claims these actions cover a view, but they aren't registered."
        )

    def test_the_audit_actually_sees_the_site(self):
        """A guard against the walk silently matching nothing and the audit passing on air."""
        audit = palette_actions.audit_skills()
        self.assertGreater(len(audit["covered"]) + len(audit["excused"]), 100)
        self.assertGreater(len(audit["covered"]), 10)

    def test_every_reason_is_a_real_sentence(self):
        for view, reason in palette_actions.NOT_A_SKILL.items():
            self.assertGreater(len(reason), 40, f"{view} needs a real reason, not '{reason}'")

    def test_the_write_surface_is_found_by_class_not_by_url_name(self):
        """Several capabilities have no URL name at all; they must still be audited."""
        views = palette_actions.postable_views()
        self.assertIn("WatchOrUnwatch", views)
        self.assertEqual(views["WatchOrUnwatch"], [])
        self.assertIn("InvoicePaid", views)


@override_settings(SINGLE_CLUB_MODE=False)
class SkillTestCase(StandardTestCase):
    """Runs skills directly. None of these need a model in the loop -- ``run_action`` is the gate."""

    def _run(self, name, params, user=None, page=None):
        request = self.client.request().wsgi_request
        request.user = user or self.user
        request.palette_page = page or {}
        return palette_actions.run_action(request, name, params)


class WatchLotTests(SkillTestCase):
    """'watch this lot'."""

    def test_watching_a_lot_by_name(self):
        result = self._run("watch_lot", {"lot": self.lot.lot_name})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(Watch.objects.filter(lot_number=self.lot, user=self.user).exists())

    def test_watching_the_lot_on_screen(self):
        result = self._run("watch_lot", {}, page={"lot_id": self.lot.pk})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(Watch.objects.filter(lot_number=self.lot, user=self.user).exists())

    def test_watching_twice_does_not_duplicate_the_row(self):
        self._run("watch_lot", {"lot": self.lot.lot_name})
        self._run("watch_lot", {"lot": self.lot.lot_name})
        self.assertEqual(Watch.objects.filter(lot_number=self.lot, user=self.user).count(), 1)

    def test_unwatching(self):
        Watch.objects.create(lot_number=self.lot, user=self.user)
        result = self._run("watch_lot", {"lot": self.lot.lot_name, "watching": False})
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(Watch.objects.filter(lot_number=self.lot, user=self.user).exists())

    def test_a_lot_nobody_can_name_asks_which_one(self):
        result = self._run("watch_lot", {})
        self.assertIn("more_info_needed", result)


class EditLotTests(SkillTestCase):
    """'make lot 14 twenty dollars'."""

    def setUp(self):
        super().setUp()
        self.in_person_auction.lot_submission_end_date = None
        self.in_person_auction.save()
        self.my_lot = Lot.objects.create(
            lot_name="Editable Shrimp",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            user=self.user,
            quantity=1,
            reserve_price=5,
        )

    def test_the_seller_can_change_the_price(self):
        result = self._run("edit_lot", {"lot": "Editable Shrimp", "reserve_price": 12})
        self.assertTrue(result.get("ok"), result)
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.reserve_price, 12)

    def test_the_quantity_changes_too(self):
        result = self._run("edit_lot", {"lot": "Editable Shrimp", "quantity": 4})
        self.assertTrue(result.get("ok"), result)
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.quantity, 4)

    def test_naming_the_lot_does_not_rename_it(self):
        """'change the editable shrimp quantity' must not rename the lot to 'Editable Shrimp'."""
        self._run("edit_lot", {"name": "editable shrimp", "quantity": 2})
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.lot_name, "Editable Shrimp")
        self.assertEqual(self.my_lot.quantity, 2)

    def test_renaming_takes_new_name(self):
        self._run("edit_lot", {"lot": "Editable Shrimp", "new_name": "cherry shrimp"})
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.lot_name, "Cherry Shrimp")

    def test_somebody_elses_lot_is_refused(self):
        result = self._run("edit_lot", {"lot": "Editable Shrimp", "reserve_price": 99}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.my_lot.refresh_from_db()
        self.assertEqual(self.my_lot.reserve_price, 5)

    def test_changing_nothing_asks_what_to_change(self):
        result = self._run("edit_lot", {"lot": "Editable Shrimp"})
        self.assertIn("more_info_needed", result)


class InvoiceStatusTests(SkillTestCase):
    """The checkout desk: 'bidder 555 paid'."""

    def setUp(self):
        super().setUp()
        self.buyer_invoice, _created = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        Invoice.objects.filter(pk=self.buyer_invoice.pk).update(status="UNPAID")
        self.buyer_invoice.refresh_from_db()

    def test_an_admin_can_mark_an_invoice_paid(self):
        result = self._run(
            "set_invoice_status",
            {"person": "555", "auction": self.in_person_auction.title, "status": "paid"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.buyer_invoice.refresh_from_db()
        self.assertEqual(self.buyer_invoice.status, "PAID")

    def test_reopening_an_invoice(self):
        Invoice.objects.filter(pk=self.buyer_invoice.pk).update(status="PAID")
        result = self._run(
            "set_invoice_status",
            {"person": "555", "auction": self.in_person_auction.title, "status": "open"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.buyer_invoice.refresh_from_db()
        self.assertEqual(self.buyer_invoice.status, "DRAFT")

    def test_an_invoice_that_is_already_paid_is_not_paid_twice(self):
        """Marking PAID books club ledger entries, so doing it again is not a harmless no-op."""
        Invoice.objects.filter(pk=self.buyer_invoice.pk).update(status="PAID")
        result = self._run(
            "set_invoice_status",
            {"person": "555", "auction": self.in_person_auction.title, "status": "paid"},
            user=self.admin_user,
        )
        self.assertIn("already", result["summary"])

    def test_a_participant_cannot_change_invoices(self):
        result = self._run(
            "set_invoice_status",
            {"person": "555", "auction": self.in_person_auction.title},
            user=self.user_with_no_lots,
        )
        self.assertIn("error", result)
        self.assertIn("admin", result["error"].lower())

    def test_an_unknown_status_is_a_question_not_a_guess(self):
        result = self._run(
            "set_invoice_status",
            {"person": "555", "auction": self.in_person_auction.title, "status": "banana"},
            user=self.admin_user,
        )
        self.assertIn("more_info_needed", result)


class ClubSkillTestCase(SkillTestCase):
    """A club the caller administers, and a member in it."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Palette Aquarium Society", active=True, points_per_lot=5)
        self.club_admin = ClubMember.objects.create(
            club=self.club,
            user=self.admin_user,
            name="Club Admin",
            email="clubadmin@example.com",
            permission_admin=True,
        )
        self.club_member = ClubMember.objects.create(club=self.club, name="Renewable Rita", email="rita@example.com")


class ClubMemberSkillTests(ClubSkillTestCase):
    """Adding, editing and renewing club members."""

    def test_an_admin_can_add_a_member(self):
        result = self._run(
            "add_club_member",
            {"name": "New Nancy", "club": self.club.name, "email": "nancy@example.com"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(ClubMember.objects.filter(club=self.club, name="New Nancy", is_deleted=False).exists())

    def test_a_stranger_cannot_add_a_member(self):
        result = self._run("add_club_member", {"name": "New Nancy", "club": self.club.name}, user=self.userB)
        self.assertIn("error", result)
        self.assertFalse(ClubMember.objects.filter(name="New Nancy").exists())

    def test_adding_the_same_member_twice_is_refused(self):
        self._run("add_club_member", {"name": "Renewable Rita", "club": self.club.name}, user=self.admin_user)
        self.assertEqual(ClubMember.objects.filter(club=self.club, name="Renewable Rita").count(), 1)

    def test_an_admin_can_change_a_members_email(self):
        result = self._run(
            "update_club_member",
            {"person": "Renewable Rita", "club": self.club.name, "email": "newrita@example.com"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.club_member.refresh_from_db()
        self.assertEqual(self.club_member.email, "newrita@example.com")

    def test_renewing_extends_the_membership(self):
        result = self._run("renew_member", {"person": "Renewable Rita", "club": self.club.name}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.club_member.refresh_from_db()
        self.assertIsNotNone(self.club_member.membership_expiration_date)

    def test_a_stranger_cannot_renew_anybody(self):
        result = self._run("renew_member", {"person": "Renewable Rita", "club": self.club.name}, user=self.userB)
        self.assertIn("error", result)
        self.club_member.refresh_from_db()
        self.assertIsNone(self.club_member.membership_expiration_date)

    def test_an_ambiguous_name_asks_which_one(self):
        ClubMember.objects.create(club=self.club, name="Renewable Robert", email="robert@example.com")
        result = self._run("renew_member", {"person": "Renewable", "club": self.club.name}, user=self.admin_user)
        self.assertIn("more_info_needed", result)
        self.assertEqual(len(result["options"]), 2)


class AwardPointsTests(ClubSkillTestCase):
    """'give bob 10 points for the corydoras'."""

    def test_a_bap_admin_can_award_points(self):
        from auctions.models import BapAward

        result = self._run(
            "award_points",
            {"person": "Renewable Rita", "club": self.club.name, "points": 10, "notes": "Corydoras"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        award = BapAward.objects.filter(club_member=self.club_member).first()
        self.assertIsNotNone(award)
        self.assertEqual(award.points, 10)
        self.assertEqual(award.awarded_by, self.admin_user)

    def test_points_with_no_number_asks_for_one(self):
        result = self._run("award_points", {"person": "Renewable Rita", "club": self.club.name}, user=self.admin_user)
        self.assertIn("more_info_needed", result)

    def test_a_stranger_cannot_award_points(self):
        result = self._run(
            "award_points", {"person": "Renewable Rita", "club": self.club.name, "points": 10}, user=self.userB
        )
        self.assertIn("error", result)


class ParticipantAdminFieldTests(SkillTestCase):
    """update_person now covers the rest of the participant edit modal."""

    def test_bidding_can_be_turned_off(self):
        result = self._run(
            "update_person",
            {"person": "555", "auction": self.in_person_auction.title, "bidding_allowed": False},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.in_person_buyer.refresh_from_db()
        self.assertFalse(self.in_person_buyer.bidding_allowed)

    def test_the_summary_reads_like_a_sentence(self):
        result = self._run(
            "update_person",
            {"person": "555", "auction": self.in_person_auction.title, "selling_allowed": False},
            user=self.admin_user,
        )
        self.assertIn("selling off", result["summary"])

    def test_a_note_can_be_left_on_somebody(self):
        result = self._run(
            "update_person",
            {"person": "555", "auction": self.in_person_auction.title, "memo": "Paid cash"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.in_person_buyer.refresh_from_db()
        self.assertEqual(self.in_person_buyer.memo, "Paid cash")

    def test_a_bidder_number_can_be_changed(self):
        result = self._run(
            "update_person",
            {"person": "555", "auction": self.in_person_auction.title, "bidder_number": "556"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.in_person_buyer.refresh_from_db()
        self.assertEqual(self.in_person_buyer.bidder_number, "556")

    def test_a_participant_still_cannot_change_anybody(self):
        result = self._run(
            "update_person",
            {"person": "555", "auction": self.in_person_auction.title, "bidding_allowed": False},
            user=self.user_with_no_lots,
        )
        self.assertIn("error", result)


@override_settings(SINGLE_CLUB_MODE=False)
class SkillPromptTests(StandardTestCase):
    """What the model is told it can do."""

    def test_every_registered_action_reaches_a_superusers_tool_list(self):
        """Nothing may be registered and then never offered to anybody.

        The skills moved out of the system prompt and into tool definitions, generated by the same
        code that serves ``/mcp/`` — so this is now the same guarantee for both surfaces at once.
        """
        from auctions import palette_assist

        self.admin_user.is_superuser = True
        self.admin_user.save()
        offered = {tool["name"] for tool in palette_assist.tools_for(self.admin_user)}
        for name in palette_actions.ACTIONS:
            self.assertIn(name, offered)

    def test_the_tool_list_is_filtered_but_the_server_is_not(self):
        """A skill the tool list didn't mention is still accepted -- filtering is not a permission."""
        from auctions import palette_assist

        offered = {tool["name"] for tool in palette_assist.tools_for(self.user)}
        self.assertNotIn("award_points", offered)
        self.assertIsNotNone(palette_actions.get_action("award_points"))

    def test_the_prompt_is_filtered_but_the_server_is_not(self):
        """A skill the prompt didn't mention is still accepted -- filtering is not a permission."""
        offered = {action.name for action in palette_actions.actions_for(self.user)}
        self.assertNotIn("award_points", offered)
        self.assertIsNotNone(palette_actions.get_action("award_points"))

    def test_writes_are_never_advertised_as_safe(self):
        """A ``safe`` action runs during assist with no countdown, so it must not change anything."""
        for action in palette_actions.ACTIONS.values():
            if action.danger == palette_actions.DANGER_SAFE:
                self.assertTrue(
                    action.lookup,
                    f"{action.name} is danger=safe but isn't a read-only lookup",
                )


class CreateAuctionTests(SkillTestCase):
    """'set up next year's spring auction'. It only ever copies -- see the resolver's docstring."""

    def setUp(self):
        super().setUp()
        userdata = self.user.userdata
        userdata.can_create_club_auctions = True
        userdata.save()
        self.when = (timezone.now() + datetime.timedelta(days=200)).strftime("%Y-%m-%dT%H:%M")

    def test_copying_carries_the_fees_and_leaves_the_dates_alone(self):
        self.in_person_auction.lot_entry_fee = 7
        self.in_person_auction.winning_bid_percent_to_club = 33
        self.in_person_auction.save()
        result = self._run(
            "create_auction",
            {"title": "Next Spring", "date_start": self.when, "copy_from": self.in_person_auction.slug},
        )
        self.assertTrue(result.get("ok"), result)
        made = Auction.objects.get(title="Next Spring")
        self.assertEqual(made.lot_entry_fee, 7)
        self.assertEqual(made.winning_bid_percent_to_club, 33)
        # The date it was given, not the source's -- to the day, because ``date_start`` is parsed
        # in the user's own timezone and the exact instant depends on which side of midnight UTC
        # the test runs on.
        self.assertGreater(made.date_start, timezone.now() + datetime.timedelta(days=199))
        self.assertLess(made.date_start, timezone.now() + datetime.timedelta(days=201))
        # Where it came from is in the history, which is the only place it is recorded.
        self.assertTrue(
            made.auctionhistory_set.filter(action__contains=f"copying {self.in_person_auction}").exists(),
            "the copy does not say what it was copied from",
        )

    def test_a_copy_is_never_listed_publicly(self):
        """The source is promoted; copying it is not a second decision to advertise it."""
        self._run(
            "create_auction",
            {"title": "Quiet Copy", "date_start": self.when, "copy_from": self.in_person_auction.slug},
        )
        self.assertFalse(Auction.objects.get(title="Quiet Copy").promote_this_auction)

    def test_the_pickup_locations_come_across(self):
        result = self._run(
            "create_auction",
            {"title": "With Locations", "date_start": self.when, "copy_from": self.in_person_auction.slug},
        )
        self.assertTrue(result.get("ok"), result)
        made = Auction.objects.get(title="With Locations")
        self.assertEqual(PickupLocation.objects.filter(auction=made).count(), 1)

    def test_with_no_source_named_it_copies_their_most_recent(self):
        result = self._run("create_auction", {"title": "Implicit", "date_start": self.when})
        self.assertTrue(result.get("ok"), result)
        # The most recent by date_start, which for a club that only runs in-person auctions is the
        # thing the old -date_end ordering got wrong. Both fixtures share a date_start, so this
        # asserts only that it copied one of theirs rather than starting from nothing.
        self.assertIn(result["copied_from"], {self.online_auction.slug, self.in_person_auction.slug})

    def test_somebody_with_nothing_to_copy_is_sent_to_the_page(self):
        """A first auction is twenty decisions and stays a form. The answer has to say where."""
        userdata = self.user_with_no_lots.userdata
        userdata.can_create_club_auctions = True
        userdata.save()
        result = self._run(
            "create_auction", {"title": "My First", "date_start": self.when}, user=self.user_with_no_lots
        )
        self.assertIn("error", result)
        self.assertEqual(result["followups"][0]["url"], reverse("create_auction"))
        self.assertFalse(Auction.objects.filter(title="My First").exists())

    def test_an_account_that_may_not_create_auctions_is_refused(self):
        result = self._run("create_auction", {"title": "Nope", "date_start": self.when}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.assertFalse(Auction.objects.filter(title="Nope").exists())

    def test_it_will_not_copy_somebody_elses_auction(self):
        theirs = Auction.objects.create(
            created_by=self.admin_user,
            title="Not Yours",
            date_start=timezone.now(),
            promote_this_auction=True,
        )
        result = self._run(
            "create_auction", {"title": "Stolen Rules", "date_start": self.when, "copy_from": theirs.slug}
        )
        self.assertIn("error", result)
        self.assertFalse(Auction.objects.filter(title="Stolen Rules").exists())

    def test_a_date_nobody_can_read_is_refused_rather_than_guessed(self):
        result = self._run("create_auction", {"title": "Whenever", "date_start": "sometime in the spring"})
        self.assertIn("error", result)
        self.assertFalse(Auction.objects.filter(title="Whenever").exists())

    def test_no_date_at_all_is_a_question(self):
        result = self._run("create_auction", {"title": "Undated"})
        self.assertIn("more_info_needed", result)


class LotImageTests(SkillTestCase):
    """'find a picture for this lot'. A URL, because LotImage has stored one all along."""

    photo = "https://example.com/blue-dream.jpg"

    def setUp(self):
        super().setUp()
        self.in_person_auction.lot_submission_end_date = None
        self.in_person_auction.save()
        self.my_lot = Lot.objects.create(
            lot_name="Photogenic Shrimp",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            user=self.user,
            quantity=1,
        )

    def test_adding_a_picture_from_the_internet(self):
        result = self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        self.assertTrue(result.get("ok"), result)
        image = LotImage.objects.get(lot_number=self.my_lot)
        self.assertEqual(image.url, self.photo)
        # Labelled as what it is. A bidder reads this next to the photo.
        self.assertEqual(image.image_source, "RANDOM")
        self.assertTrue(image.is_primary, "the first picture on a lot is its thumbnail")

    def test_the_answer_names_the_lot_it_landed_on(self):
        result = self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        # Fenced, because a lot name is somebody else's text arriving in an agent's context.
        self.assertIn(self.my_lot.lot_name, result["lot_name"])
        self.assertEqual(result["url"], self.my_lot.lot_link)
        self.assertEqual(result["image"]["url"], self.photo)

    def test_a_url_that_is_not_an_image_is_refused(self):
        result = self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": "https://example.com/a-blog-post"})
        self.assertIn("error", result)
        self.assertFalse(LotImage.objects.filter(lot_number=self.my_lot).exists())

    def test_the_sellers_own_photo_has_to_be_asked_for(self):
        result = self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo, "image_source": "actual"})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(LotImage.objects.get(lot_number=self.my_lot).image_source, "ACTUAL")

    def test_a_kind_of_picture_nobody_recognises_is_refused(self):
        result = self._run(
            "add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo, "image_source": "screenshot"}
        )
        self.assertIn("error", result)

    def test_somebody_elses_lot_is_refused(self):
        result = self._run(
            "add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo}, user=self.user_with_no_lots
        )
        self.assertIn("error", result)
        self.assertFalse(LotImage.objects.filter(lot_number=self.my_lot).exists())

    def test_removing_the_only_picture(self):
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        result = self._run("remove_lot_image", {"lot": "Photogenic Shrimp"})
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(LotImage.objects.filter(lot_number=self.my_lot).exists())

    def test_removing_one_of_several_asks_which(self):
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": "https://example.com/other.png"})
        result = self._run("remove_lot_image", {"lot": "Photogenic Shrimp"})
        self.assertIn("more_info_needed", result)
        self.assertEqual(LotImage.objects.filter(lot_number=self.my_lot).count(), 2)

    def test_removing_the_thumbnail_promotes_another(self):
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": "https://example.com/other.png"})
        primary = LotImage.objects.get(lot_number=self.my_lot, is_primary=True)
        self._run("remove_lot_image", {"lot": "Photogenic Shrimp", "image_id": primary.pk})
        remaining = LotImage.objects.get(lot_number=self.my_lot)
        self.assertTrue(remaining.is_primary, "the lot would show a placeholder with no primary")

    def test_lots_with_no_picture_can_be_listed(self):
        """'for any of my lots that don't have images' — the question the whole skill is for."""
        result = self._run(
            "list_lots", {"status": "mine", "without_images": True, "auction": self.in_person_auction.slug}
        )
        self.assertIn("Photogenic Shrimp", json.dumps(result))
        self._run("add_lot_image", {"lot": "Photogenic Shrimp", "url": self.photo})
        result = self._run(
            "list_lots", {"status": "mine", "without_images": True, "auction": self.in_person_auction.slug}
        )
        self.assertNotIn("Photogenic Shrimp", json.dumps(result))


class AuctionWideLotImageTests(SkillTestCase):
    """ "find all the lots in my auction with no picture and add one" -- the admin's version.

    The seller's half of this was already there. The admin's half needed ``list_lots`` to answer
    about somebody else's lots (it does, and adds the seller's name for an admin) and
    ``Lot.image_permission_check`` to let an auction admin write to them -- which it did by asking
    half of ``Auction.permission_check`` by hand, and so missed the club half of it entirely.
    """

    photo = "https://example.com/found-online.jpg"

    def setUp(self):
        super().setUp()
        self.in_person_auction.lot_submission_end_date = None
        self.in_person_auction.save()
        self.someone_elses_lot = Lot.objects.create(
            lot_name="Bare Betta",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            user=self.user_with_no_lots,
            quantity=1,
        )

    def test_an_admin_sees_every_lot_with_no_picture_and_who_brought_it(self):
        result = self._run(
            "list_lots", {"without_images": True, "auction": self.in_person_auction.slug}, user=self.admin_user
        )
        rows = {row["name"] for row in result["lots"]}
        self.assertTrue(any("Bare Betta" in name for name in rows), result)
        row = next(row for row in result["lots"] if "Bare Betta" in row["name"])
        self.assertFalse(row["has_picture"])
        self.assertIn("seller", row, "an admin needs to know whose lot it is")

    def test_an_admin_can_put_a_picture_on_somebody_elses_lot(self):
        result = self._run("add_lot_image", {"lot": "Bare Betta", "url": self.photo}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(LotImage.objects.filter(lot_number=self.someone_elses_lot).exists())

    def test_a_club_officer_running_a_club_auction_can_too(self):
        """The clause that was missing: in a club-managed auction admin comes from the club."""
        club = Club.objects.create(name="Photo Club", active=True)
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        officer = ClubMember.objects.create(
            club=club, user=self.userB, name="Officer", email="officer@example.com", permission_admin=True
        )
        self.assertTrue(officer.permission_admin)
        result = self._run("add_lot_image", {"lot": "Bare Betta", "url": self.photo}, user=self.userB)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(LotImage.objects.filter(lot_number=self.someone_elses_lot).exists())

    def test_a_stranger_still_cannot(self):
        result = self._run("add_lot_image", {"lot": "Bare Betta", "url": self.photo}, user=self.user_who_does_not_join)
        self.assertIn("error", result)
        self.assertFalse(LotImage.objects.filter(lot_number=self.someone_elses_lot).exists())


class ClubManagedParticipantTests(ClubSkillTestCase):
    """update_person in a club-managed auction, where the fields live on the ClubMember.

    The bug this covers is not a crash: ``update_person`` answered "ok" and named another tool.
    From the other end of an assistant that reads as a refusal of the one sentence a check-in desk
    says most -- "bob is bidder 12 now" -- so the test is as much about the answer as the write.
    """

    def setUp(self):
        super().setUp()
        from auctions.views import _upsert_clubmember_shadow_tos

        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        self.club_admin.permission_add_edit = True
        self.club_admin.permission_manage_auctions = True
        self.club_admin.save()
        self.member = ClubMember.objects.create(
            club=self.club, name="Managed Mike", email="mike@example.com", bidder_number="61"
        )
        self.tos = _upsert_clubmember_shadow_tos(self.in_person_auction, self.member)

    def test_a_bidder_number_lands_on_the_club_member(self):
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "bidder_number": "62"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.member.refresh_from_db()
        self.tos.refresh_from_db()
        self.assertEqual(self.member.bidder_number, "62")
        self.assertEqual(self.tos.bidder_number, "62")
        self.assertEqual(result["bidder_number"], "62")

    def test_the_summary_never_reads_back_the_error_placeholder(self):
        """The reported bug: 'Set Jane Seller's bidder number to ERROR'."""
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "bidder_number": "63"},
            user=self.admin_user,
        )
        self.assertNotIn("ERROR", result.get("summary", ""))
        self.assertIn("63", result["summary"])

    def test_an_email_reaches_both_rows(self):
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "email": "mike2@example.com"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.member.refresh_from_db()
        self.tos.refresh_from_db()
        self.assertEqual(self.member.email, "mike2@example.com")
        self.assertEqual(self.tos.email, "mike2@example.com")

    def test_running_the_auction_is_enough_to_fix_somebody_in_it(self):
        """Deliberately wider than the web page. See _update_through_the_club.

        Requiring the club's permission_add_edit here refused the auction's own creator whenever
        they held no club role — somebody correcting a typo in the email of a person standing at
        their check-in desk. What that permission protects is the membership roll; this is already
        narrowed to a participant in an auction the caller administers.
        """
        self.club_admin.permission_add_edit = False
        self.club_admin.permission_admin = False
        self.club_admin.save()
        self.assertTrue(self.club_admin.permission_manage_auctions)
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "bidder_number": "64"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bidder_number, "64")

    def test_somebody_with_no_relationship_to_the_auction_still_cannot(self):
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "bidder_number": "64"},
            user=self.user_who_does_not_join,
        )
        self.assertIn("error", result)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bidder_number, "61")

    def test_a_duplicate_bidder_number_is_the_clubs_own_refusal(self):
        ClubMember.objects.create(club=self.club, name="Taken Tina", email="tina@example.com", bidder_number="70")
        result = self._run(
            "update_person",
            {"person": "Managed Mike", "auction": self.in_person_auction.slug, "bidder_number": "70"},
            user=self.admin_user,
        )
        self.assertIn("error", result)
        self.member.refresh_from_db()
        self.assertEqual(self.member.bidder_number, "61")


class UndoCheckInTests(SkillTestCase):
    """One person per call. The bulk version was written and then deliberately taken out again."""

    def setUp(self):
        super().setUp()
        club = Club.objects.create(name="Check In Club", active=True)
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "checkin"
        self.in_person_auction.save()
        for tos in AuctionTOS.objects.filter(auction=self.in_person_auction):
            tos.checked_in = timezone.now()
            tos.save(update_fields=["checked_in"])

    def test_naming_one_person_only_touches_that_person(self):
        result = self._run(
            "undo_check_in", {"auction": self.in_person_auction.slug, "person": "555"}, user=self.admin_user
        )
        self.assertTrue(result.get("ok"), result)
        self.in_person_buyer.refresh_from_db()
        self.assertIsNone(self.in_person_buyer.checked_in)
        self.assertTrue(AuctionTOS.objects.filter(auction=self.in_person_auction, checked_in__isnull=False).exists())

    def test_there_is_no_way_to_clear_the_whole_auction_in_one_call(self):
        """The bound that makes prompt injection expensive is 'no tool changes more than one row'."""
        action = palette_actions.ACTIONS["undo_check_in"]
        self.assertNotIn("everyone", action.params)
        before = AuctionTOS.objects.filter(auction=self.in_person_auction, checked_in__isnull=False).count()
        result = self._run(
            "undo_check_in", {"auction": self.in_person_auction.slug, "everyone": True}, user=self.admin_user
        )
        self.assertIn("error", result)
        self.assertEqual(
            AuctionTOS.objects.filter(auction=self.in_person_auction, checked_in__isnull=False).count(), before
        )

    def test_the_list_an_agent_clears_from_is_a_read(self):
        """ "uncheck everybody" is list_people then one call each, and this is the list."""
        result = self._run(
            "list_people", {"auction": self.in_person_auction.slug, "status": "checked_in"}, user=self.admin_user
        )
        self.assertTrue(result["found"], result)
        self.assertGreater(result["count"], 0)

    def test_a_participant_cannot_uncheck_anybody(self):
        result = self._run(
            "undo_check_in",
            {"auction": self.in_person_auction.slug, "person": "555"},
            user=self.user_with_no_lots,
        )
        self.assertIn("error", result)
        self.in_person_buyer.refresh_from_db()
        self.assertIsNotNone(self.in_person_buyer.checked_in)


class MembershipCardTests(ClubSkillTestCase):
    """'show me my membership card'. Read-only and always the caller's own."""

    def setUp(self):
        super().setUp()
        self.club.show_member_barcode = True
        self.club.membership_annual_fee = 20
        self.club.save()
        self.mine = ClubMember.objects.create(
            club=self.club,
            user=self.user,
            name="Card Carrier",
            email="carrier@example.com",
            membership_number=4242,
        )

    def test_it_answers_with_the_card(self):
        result = self._run("my_membership", {}, user=self.user)
        self.assertTrue(result.get("found"), result)
        card = result["membership"]
        self.assertEqual(card["club"], self.club.name)
        self.assertEqual(card["membership_number"], 4242)
        self.assertIn("4242", card["barcode_url"])

    def _let_the_club_take_money(self):
        """The club has to be able to accept a payment before a Renew link means anything."""
        self.club.enable_club_page = True
        self.club.allow_non_oauth_paypal = True
        self.club.paypal_client_id = "client"
        self.club.paypal_secret = "secret"
        self.club.save()

    def test_an_expired_membership_says_so_and_offers_a_way_back(self):
        self._let_the_club_take_money()
        self.mine.membership_expiration_date = timezone.now().date() - datetime.timedelta(days=10)
        self.mine.save()
        result = self._run("my_membership", {}, user=self.user)
        card = result["membership"]
        self.assertTrue(card["is_expired"])
        self.assertIn("expired", result["summary"].lower())
        # The renew link is only there when the club can actually take the money.
        self.assertIn("renew_url", card)

    def test_a_club_that_takes_no_money_gets_no_renew_button(self):
        self.club.membership_annual_fee = 0
        self.club.save()
        card = self._run("my_membership", {}, user=self.user)["membership"]
        self.assertNotIn("renew_url", card)

    def test_somebody_with_no_membership_is_told_so(self):
        result = self._run("my_membership", {}, user=self.userB)
        self.assertIn("error", result)

    def test_it_cannot_be_asked_about_anybody_else(self):
        """There is no parameter for a person, and the club filter cannot reach another member."""
        action = palette_actions.ACTIONS["my_membership"]
        self.assertEqual(set(action.params), {"club"})
        result = self._run("my_membership", {"club": self.club.name}, user=self.user)
        self.assertEqual(result["membership"]["name"], "Card Carrier")


class CopyingAnAuctionTests(SkillTestCase):
    """What ``copy_users_when_copying_this_auction`` should and should not carry across.

    The people are the point of the setting -- a club that runs the same auction every year does
    not want to retype two hundred names. What is *not* the point is what happened at last year's:
    copying was done with ``tos.pk = None`` on a loaded row, so every column came with it, and an
    auction in check-in mode opened with everybody already through the door.
    """

    def setUp(self):
        super().setUp()
        userdata = self.user.userdata
        userdata.can_create_club_auctions = True
        userdata.save()
        self.when = (timezone.now() + datetime.timedelta(days=300)).strftime("%Y-%m-%dT%H:%M")
        self.in_person_auction.copy_users_when_copying_this_auction = True
        self.in_person_auction.save()
        self.in_person_buyer.checked_in = timezone.now()
        self.in_person_buyer.door_prize_called = timezone.now()
        self.in_person_buyer.confirm_email_sent = True
        self.in_person_buyer.time_spent_reading_rules = 90
        self.in_person_buyer.memo = "Always pays cash"
        self.in_person_buyer.save()

    def _copy(self, title):
        result = self._run(
            "create_auction",
            {"title": title, "date_start": self.when, "copy_from": self.in_person_auction.slug},
        )
        self.assertTrue(result.get("ok"), result)
        return Auction.objects.get(title=title)

    def test_nobody_arrives_at_the_copy_already_checked_in(self):
        made = self._copy("Next Year In Person")
        self.assertTrue(AuctionTOS.objects.filter(auction=made).exists(), "the people did not come across at all")
        self.assertFalse(
            AuctionTOS.objects.filter(auction=made, checked_in__isnull=False).exists(),
            "the copy opened with people already checked in",
        )

    def test_last_years_door_prize_and_emails_do_not_come_with_them(self):
        made = self._copy("Next Year Prizes")
        copied = AuctionTOS.objects.get(auction=made, bidder_number=self.in_person_buyer.bidder_number)
        self.assertIsNone(copied.door_prize_called)
        self.assertFalse(copied.confirm_email_sent)
        self.assertEqual(copied.time_spent_reading_rules, 0)

    def test_a_club_managed_copy_never_takes_the_people(self):
        """In that mode the participants are the club's members, and check-in mode adds them at the
        door — pre-filling from last year is the thing check-in mode exists to stop."""
        club = Club.objects.create(name="Copy Club", active=True)
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "checkin"
        self.in_person_auction.save()
        made = self._copy("Next Year Club Managed")
        self.assertTrue(made.is_club_managed)
        self.assertFalse(
            AuctionTOS.objects.filter(auction=made).exists(),
            "a club-managed copy pre-filled its door table from last year",
        )

    def test_what_is_true_of_the_person_still_comes_across(self):
        made = self._copy("Next Year People")
        copied = AuctionTOS.objects.get(auction=made, bidder_number=self.in_person_buyer.bidder_number)
        self.assertEqual(copied.name, self.in_person_buyer.name)
        self.assertEqual(copied.memo, "Always pays cash")


@isolated_cache("palette-species-skills")
class LotSpeciesTests(SkillTestCase):
    """'Fix the scientific name on lot 10' — three jobs, three verbs.

    Which one it is depends on what the site already knows, and the tests are grouped that way:
    the species is on the list under a name nobody typed; the species is on the list and the name
    is missing; the species genuinely isn't there.
    """

    def setUp(self):
        super().setUp()
        self.in_person_auction.use_scientific_name = True
        self.in_person_auction.lot_submission_end_date = None
        self.in_person_auction.save()
        self.yellow_lab = make_species("Labidochromis", "caeruleus", "Blue streak hap")
        self.cherry = make_species("Neocaridina", "davidi", "Cherry shrimp")
        self.lot = Lot.objects.create(
            lot_name="Yellow lab",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            user=self.user,
            quantity=1,
        )
        userdata = self.admin_user.userdata
        userdata.can_create_club_auctions = True
        userdata.save()

    # --- set_lot_species ---

    def test_an_exact_scientific_name_lands_on_the_lot(self):
        result = self._run("set_lot_species", {"lot": "Yellow lab", "species": "Neocaridina davidi"})
        self.assertTrue(result.get("ok"), result)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.cherry)
        self.assertEqual(result["species"]["scientific_name"], self.cherry.full_scientific_name)

    def test_with_no_name_given_it_reads_the_lots_own_name(self):
        """The fix for a lot added by a route that filled nothing in."""
        SpeciesCommonName.objects.create(species=self.yellow_lab, name="yellow lab", source="admin")
        result = self._run("set_lot_species", {"lot": "Yellow lab"})
        self.assertTrue(result.get("ok"), result)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.yellow_lab)

    def test_two_candidates_is_a_question_not_a_pick(self):
        """A wrong species reaches a printed label and breeder points, so no match beats a guess."""
        make_species("Neocaridina", "palmata", "Cherry shrimp")
        result = self._run("set_lot_species", {"lot": "Yellow lab", "species": "cherry shrimp"})
        self.assertIn("more_info_needed", result)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_a_name_on_nothing_says_which_of_the_two_fixes_to_use(self):
        result = self._run("set_lot_species", {"lot": "Yellow lab", "species": "Nonexistent madeupii"})
        self.assertIn("error", result)
        self.assertIn("name_a_species", result["error"])
        self.assertIn("add_species", result["error"])

    def test_it_can_be_taken_off_again(self):
        self._run("set_lot_species", {"lot": "Yellow lab", "species": "Neocaridina davidi"})
        result = self._run("set_lot_species", {"lot": "Yellow lab", "clear": True})
        self.assertTrue(result.get("ok"), result)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_somebody_elses_lot_is_refused(self):
        result = self._run(
            "set_lot_species",
            {"lot": "Yellow lab", "species": "Neocaridina davidi"},
            user=self.user_with_no_lots,
        )
        self.assertIn("error", result)
        self.lot.refresh_from_db()
        self.assertIsNone(self.lot.species)

    def test_an_admin_teaches_the_site_the_lot_name_and_a_seller_does_not(self):
        """SpeciesSearchCache is global and read ahead of the token search — same rule as LotAdmin.

        ``self.user`` created this auction, so they are an admin of it; the plain seller is the
        one who owns ``sellers_lot``.
        """
        # A seller may only edit a lot while lot submission is still open -- the same rule edit_lot
        # applies -- and the fixture auction closed two days ago. Move the whole thing forward for
        # this one test. ``lot_submission_end_date`` is set last because Auction.save() derives it.
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=5)
        self.in_person_auction.save()
        Auction.objects.filter(pk=self.in_person_auction.pk).update(
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=4)
        )
        self.in_person_auction.refresh_from_db()
        sellers_lot = Lot.objects.create(
            lot_name="Seller's shrimp",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            user=self.user_with_no_lots,
            quantity=1,
        )
        result = self._run(
            "set_lot_species",
            {"lot": sellers_lot.lot_name, "species": "Neocaridina davidi"},
            user=self.user_with_no_lots,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(result["remembered_the_lot_name"])
        self.assertFalse(SpeciesSearchCache.objects.filter(search_text__contains="shrimp").exists())
        result = self._run(
            "set_lot_species", {"lot": "Yellow lab", "species": "Neocaridina davidi"}, user=self.admin_user
        )
        self.assertTrue(result["remembered_the_lot_name"], result)
        self.assertTrue(SpeciesSearchCache.objects.filter(search_text__contains="yellow lab").exists())

    # --- name_a_species ---

    def test_naming_a_species_makes_the_matcher_find_it(self):
        result = self._run(
            "name_a_species",
            {"species": "Labidochromis caeruleus", "names": "yellow lab"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn("yellow lab", [name.lower() for name in result["names_added"]])
        # And now the lot matches by its own name, which is the point of doing it.
        follow_up = self._run("set_lot_species", {"lot": "Yellow lab"}, user=self.admin_user)
        self.assertTrue(follow_up.get("ok"), follow_up)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.yellow_lab)

    def test_naming_a_species_can_set_the_lot_in_the_same_call(self):
        result = self._run(
            "name_a_species",
            {"species": "Labidochromis caeruleus", "lot": "Yellow lab"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, self.yellow_lab)

    def test_a_name_that_belongs_to_another_species_is_refused(self):
        """One name on two species is the loss of a name, not the gain of one."""
        result = self._run(
            "name_a_species",
            {"species": "Labidochromis caeruleus", "names": "Cherry shrimp"},
            user=self.admin_user,
        )
        self.assertIn("error", result)

    def test_somebody_who_runs_no_auction_cannot_name_anything(self):
        result = self._run(
            "name_a_species",
            {"species": "Labidochromis caeruleus", "names": "yellow lab"},
            user=self.user_who_does_not_join,
        )
        self.assertIn("error", result)

    # --- add_species ---

    def test_adding_a_species_and_putting_it_on_the_lot(self):
        result = self._run(
            "add_species",
            {"scientific_name": "Ancistrus cirrhosus", "common_name": "Bristlenose pleco", "lot": "Yellow lab"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        added = Species.objects.get(genus="Ancistrus", species="cirrhosus")
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.species, added)

    def test_what_an_auction_admin_adds_is_not_everybody_s(self):
        self._run("add_species", {"scientific_name": "Ancistrus cirrhosus"}, user=self.admin_user)
        self.assertFalse(Species.objects.get(genus="Ancistrus", species="cirrhosus").approved)

    def test_a_hybrid_has_no_scientific_name_at_all(self):
        result = self._run("add_species", {"hybrid": True, "variety": "Tibee"}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        hybrid = Species.objects.get(variety="Tibee")
        self.assertTrue(hybrid.is_hybrid)
        self.assertEqual(hybrid.genus, "")
        self.assertIsNone(hybrid.parent)

    def test_a_strain_keeps_its_parents_genus(self):
        result = self._run(
            "add_species",
            {"variety": "Blue Dream", "strain_of": "Neocaridina davidi"},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        strain = Species.objects.get(variety="Blue Dream")
        self.assertEqual(strain.genus, "Neocaridina")
        self.assertEqual(strain.parent, self.cherry)

    def test_a_species_already_on_the_list_is_refused(self):
        result = self._run("add_species", {"scientific_name": "Neocaridina davidi"}, user=self.admin_user)
        self.assertIn("error", result)
        self.assertEqual(Species.objects.filter(genus="Neocaridina", species="davidi").count(), 1)

    def test_somebody_who_runs_no_auction_cannot_add_one(self):
        result = self._run("add_species", {"scientific_name": "Ancistrus cirrhosus"}, user=self.user_who_does_not_join)
        self.assertIn("error", result)
        self.assertFalse(Species.objects.filter(genus="Ancistrus").exists())


class RequestASkillTests(SkillTestCase):
    """The one write with no subject: an agent writing down what it could not do."""

    def test_a_request_is_written_down(self):
        result = self._run(
            "request_a_skill",
            {"skill": "refund an invoice", "reason": "They wanted to refund bidder 12 and nothing here does that."},
        )
        self.assertTrue(result.get("ok"), result)
        row = AssistantSkillRequest.objects.get(pk=result["request_id"])
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.skill, "refund an invoice")
        self.assertIn("bidder 12", row.reason)

    def test_a_name_with_no_reason_is_a_question(self):
        """The name on its own does not say what the tool is for, and that sentence is the value."""
        result = self._run("request_a_skill", {"skill": "refund an invoice"})
        self.assertIn("more_info_needed", result)
        self.assertFalse(AssistantSkillRequest.objects.exists())

    def test_the_same_caller_asking_twice_updates_their_own_row(self):
        self._run("request_a_skill", {"skill": "refund an invoice", "reason": "First try."})
        self._run("request_a_skill", {"skill": "refund an invoice", "reason": "Second, better description."})
        rows = AssistantSkillRequest.objects.filter(user=self.user, skill="refund an invoice")
        self.assertEqual(rows.count(), 1)
        self.assertIn("better", rows.first().reason)

    def test_two_people_asking_is_two_rows_and_it_says_so(self):
        """A duplicate is the evidence, which is why they are counted and not merged."""
        self._run("request_a_skill", {"skill": "refund an invoice", "reason": "Mine."})
        result = self._run("request_a_skill", {"skill": "refund an invoice", "reason": "Theirs."}, user=self.admin_user)
        self.assertEqual(AssistantSkillRequest.objects.filter(skill="refund an invoice").count(), 2)
        self.assertEqual(result["others_asking"], 1)
        self.assertIn("1 other person has asked", result["summary"])


class AssistantSkillRequestsPageTests(StandardTestCase):
    """The queue a superuser reads, and the four buttons that move a row through it."""

    def setUp(self):
        super().setUp()
        self.url = reverse("assistant_skill_requests")
        self.row = AssistantSkillRequest.objects.create(
            user=self.user, skill="refund an invoice", reason="Nothing here refunds anything."
        )

    def test_only_a_superuser_can_read_it(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_a_superuser_sees_the_request(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "refund an invoice")
        self.assertContains(response, "Nothing here refunds anything.")

    def test_the_ones_most_people_asked_for_come_first(self):
        AssistantSkillRequest.objects.create(user=self.admin_user, skill="refund an invoice", reason="Me too.")
        AssistantSkillRequest.objects.create(user=self.userB, skill="something else", reason="Only me.")
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        groups = self.client.get(self.url).context["groups"]
        self.assertEqual(groups[0]["skill"], "refund an invoice")
        self.assertEqual(groups[0]["people_count"], 2)

    def test_a_status_button_moves_it(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self.url, {"pk": self.row.pk, "status": "planned", "notes": "Next release"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "planned")
        self.assertEqual(self.row.notes, "Next release")

    def test_a_status_nobody_offers_is_ignored(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self.url, {"pk": self.row.pk, "status": "banana"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "new")

    def test_a_member_cannot_move_anything(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(self.url, {"pk": self.row.pk, "status": "done"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "new")

    def test_it_comes_back_to_the_tab_it_was_posted_from(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(self.url, {"pk": self.row.pk, "status": "planned", "filter": "planned"})
        self.assertEqual(response["Location"], f"{self.url}?status=planned")

    def test_the_referrer_is_never_where_it_goes(self):
        """Built out of reverse() and one validated word. A referrer is an open redirect."""
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            self.url,
            {"pk": self.row.pk, "status": "done", "filter": "https://evil.example/"},
            HTTP_REFERER="https://evil.example/",
        )
        self.assertEqual(response["Location"], self.url)


class PointsDeskTests(ClubSkillTestCase):
    """The breeder award review desk: what's waiting, deciding it, and the seller's own side.

    The fixture is the shape a club's points backlog actually has: one lot waiting for a decision,
    one already denied, and one whose seller never ticked "I bred this" -- which is the third
    question ("show me lots that should have been marked bap but weren't") and the one that has no
    other way of being asked.
    """

    def setUp(self):
        super().setUp()
        from auctions.models import Category

        self.club.enable_breeder_award_program = True
        # Off, or every lot would be awarded the moment it sold and there would be no queue. This
        # is the setting a club that reviews its own points is running with by definition.
        self.club.auto_add_points = False
        self.club.min_quantity = 1
        self.club.save()
        self.seller_member = ClubMember.objects.create(
            club=self.club, user=self.user, name="Selling Sam", email="sam@example.com"
        )
        self.category = Category.objects.create(name="Points Cichlids", bap_points=4)
        self.club_auction = Auction.objects.create(
            created_by=self.admin_user,
            club=self.club,
            title="Points Spring Auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=1),
            winning_bid_percent_to_club=25,
            lot_entry_fee=0,
            unsold_lot_fee=0,
            tax=0,
            promote_this_auction=False,
        )
        location = PickupLocation.objects.create(
            name="Points Hall",
            auction=self.club_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=1),
        )
        self.seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.club_auction, pickup_location=location, bidder_number="601"
        )
        self.buyer_tos = AuctionTOS.objects.create(
            user=self.userB, auction=self.club_auction, pickup_location=location, bidder_number="602"
        )
        self.pending_lot = self._lot("Waiting Wagtails", bred=True)
        self.denied_lot = self._lot("Denied Danios", bred=True, manually_approved=True)
        self.missed_lot = self._lot("Forgot The Box Barbs", bred=False)

    def _lot(self, name, *, bred, manually_approved=False, sold=True):
        return Lot.objects.create(
            lot_name=name,
            auction=self.club_auction,
            auctiontos_seller=self.seller_tos,
            auctiontos_winner=self.buyer_tos if sold else None,
            winning_price=12 if sold else None,
            active=False,
            quantity=6,
            i_bred_this_fish=bred,
            manually_approved=manually_approved,
            species_category=self.category,
            date_end=timezone.now() - datetime.timedelta(days=1),
        )

    def _names(self, result):
        return {row["name"].strip("«»") for row in result.get("lots", [])}

    # --- the queue ----------------------------------------------------------

    def test_pending_is_what_the_desk_has_to_decide(self):
        result = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        self.assertTrue(result.get("found"), result)
        self.assertEqual(self._names(result), {"Waiting Wagtails"})

    def test_denied_lots_from_one_auction(self):
        result = self._run(
            "points_queue",
            {"club": self.club.name, "status": "denied", "auction": "Points Spring"},
            user=self.admin_user,
        )
        self.assertEqual(self._names(result), {"Denied Danios"})

    def test_the_word_last_means_the_clubs_most_recent_auction(self):
        result = self._run(
            "points_queue", {"club": self.club.name, "status": "denied", "auction": "last"}, user=self.admin_user
        )
        self.assertEqual(self._names(result), {"Denied Danios"})

    def test_lots_the_seller_never_marked_as_bred(self):
        result = self._run("points_queue", {"club": self.club.name, "status": "missed"}, user=self.admin_user)
        self.assertEqual(self._names(result), {"Forgot The Box Barbs"})

    def test_a_pending_row_says_what_approving_it_would_give(self):
        result = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        row = result["lots"][0]
        self.assertEqual(row["points_if_approved"], 5)  # the club's points_per_lot, not the category's 4
        self.assertEqual(row["the_site_says"], "eligible")

    def test_a_lot_name_is_fenced(self):
        """Forty characters somebody else typed, read by an agent holding the write scope."""
        result = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        self.assertTrue(result["lots"][0]["name"].startswith("«"))

    def test_a_status_nobody_recognises_is_refused_rather_than_defaulted(self):
        """Quietly answering "pending" hands back a real list that is not the one asked for."""
        result = self._run("points_queue", {"club": self.club.name, "status": "unsold"}, user=self.admin_user)
        self.assertIn("isn't a status I know", result.get("error", ""))

    def test_a_stranger_sees_nothing(self):
        result = self._run("points_queue", {"club": self.club.name}, user=self.userB)
        self.assertIn("error", result)

    def test_a_club_with_no_program_says_so_rather_than_refusing_a_permission(self):
        self.club.enable_breeder_award_program = False
        self.club.save()
        result = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        self.assertIn("doesn't run a breeder award program", result.get("error", ""))

    # --- deciding one -------------------------------------------------------

    def test_approving_with_no_number_uses_the_clubs_own_rules(self):
        from auctions.models import BapAward

        result = self._run("review_points", {"lot_id": self.pending_lot.pk}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        award = BapAward.objects.get(lot=self.pending_lot)
        self.assertEqual(award.points, 5)
        self.assertEqual(award.club_member, self.seller_member)
        self.assertEqual(award.awarded_by, self.admin_user)
        self.pending_lot.refresh_from_db()
        self.assertEqual(self.pending_lot.bap_points_awarded, 5)
        self.assertTrue(self.pending_lot.manually_approved)

    def test_a_number_overrides_the_default(self):
        from auctions.models import BapAward

        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "points": 20}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(BapAward.objects.get(lot=self.pending_lot).points, 20)

    def test_finding_the_lot_by_its_number(self):
        result = self._run(
            "review_points",
            {"lot": str(self.pending_lot.lot_number_int), "club": self.club.name},
            user=self.admin_user,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["lot_number"], self.pending_lot.lot_number_display)

    def test_denying_leaves_no_award_and_takes_it_off_the_queue(self):
        from auctions.models import BapAward

        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": "deny"}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(BapAward.objects.filter(lot=self.pending_lot).exists())
        self.pending_lot.refresh_from_db()
        self.assertTrue(self.pending_lot.manually_approved)
        queue = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        self.assertEqual(self._names(queue), set())

    def test_undo_puts_it_back_on_the_queue(self):
        self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": "deny"}, user=self.admin_user)
        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": "undo"}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        queue = self._run("points_queue", {"club": self.club.name}, user=self.admin_user)
        self.assertEqual(self._names(queue), {"Waiting Wagtails"})

    def test_undoing_a_lot_nobody_has_decided_is_a_quiet_no_op(self):
        """It declares itself idempotent, so a retried call must not come back an error."""
        from auctions.models import ClubHistory

        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": "undo"}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(ClubHistory.objects.filter(club=self.club, applies_to="BAP").exists())

    def test_every_decision_lands_in_the_clubs_history(self):
        """Including undo, which used to roll back the other two and leave no trace at all."""
        from auctions.models import ClubHistory

        for decision in ("approve", "deny", "undo"):
            self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": decision}, user=self.admin_user)
        lines = ClubHistory.objects.filter(club=self.club, applies_to="BAP").count()
        self.assertEqual(lines, 3)

    def test_a_stranger_cannot_decide_anything(self):
        from auctions.models import BapAward

        result = self._run("review_points", {"lot_id": self.pending_lot.pk}, user=self.userB)
        self.assertIn("error", result)
        self.assertFalse(BapAward.objects.filter(lot=self.pending_lot).exists())

    def test_a_seller_who_is_not_a_member_has_nobody_to_credit(self):
        from auctions.models import BapAward

        self.seller_member.is_deleted = True
        self.seller_member.save()
        result = self._run("review_points", {"lot_id": self.pending_lot.pk}, user=self.admin_user)
        self.assertIn("error", result)
        self.assertFalse(BapAward.objects.filter(lot=self.pending_lot).exists())

    def test_hap_points_at_a_club_with_no_separate_hap_are_refused_by_name(self):
        """A refusal a click cannot produce: the page only ever shows the one column."""
        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "hap_points": 5}, user=self.admin_user)
        self.assertIn("separate HAP", result.get("error", ""))

    def test_an_unknown_decision_is_refused_rather_than_guessed(self):
        result = self._run("review_points", {"lot_id": self.pending_lot.pk, "decision": "maybe"}, user=self.admin_user)
        self.assertIn("error", result)

    # --- the seller's own side ----------------------------------------------

    def test_my_points_says_what_they_have(self):
        from auctions.models import BapAward

        BapAward.objects.create(club_member=self.seller_member, date=timezone.now().date(), points=17)
        result = self._run("my_points", {"auction": "Points Spring"}, user=self.user)
        self.assertTrue(result.get("found"), result)
        self.assertEqual(result["points"]["clubs"][0]["points"]["bap"], 17)

    def test_the_summary_names_every_track_the_club_runs(self):
        """A plant club's whole answer is in the HAP column."""
        from auctions.models import BapAward

        self.club.separate_hap = True
        self.club.save()
        BapAward.objects.create(club_member=self.seller_member, date=timezone.now().date(), hap_points=9)
        result = self._run("my_points", {"club": "palette-aquarium-society"}, user=self.user)
        self.assertIn("9 HAP", result["summary"])

    def test_my_points_forecasts_the_auction(self):
        result = self._run("my_points", {"auction": "Points Spring"}, user=self.user)
        forecast = result["points"]["this_auction"]
        self.assertEqual(forecast["already_awarded"], 0)
        # Only the one lot that is eligible and undecided: the denied one is decided, and the one
        # whose box was never ticked is not in the program at all.
        self.assertEqual(forecast["still_to_come"], 5)

    def test_an_awarded_lot_is_reported_as_awarded_rather_than_forecast_twice(self):
        self._run("review_points", {"lot_id": self.pending_lot.pk}, user=self.admin_user)
        result = self._run("my_points", {"auction": "Points Spring"}, user=self.user)
        forecast = result["points"]["this_auction"]
        self.assertEqual(forecast["already_awarded"], 5)
        self.assertEqual(forecast["still_to_come"], 0)

    def test_an_unsold_lot_still_counts_towards_if_they_all_sell(self):
        self._lot("Unsold Uarus", bred=True, sold=False)
        result = self._run("my_points", {"auction": "Points Spring"}, user=self.user)
        self.assertEqual(result["points"]["this_auction"]["still_to_come"], 10)

    def test_somebody_in_no_points_club_is_told_so_rather_than_given_a_zero(self):
        result = self._run("my_points", {}, user=self.user_with_no_lots)
        self.assertFalse(result.get("found"))
        self.assertIn("breeder award program", result["summary"])


class PlaceBidTests(SkillTestCase):
    """'bid $20 on lot 14' — the one write on this list with no way back."""

    def setUp(self):
        super().setUp()
        # An open online lot belonging to somebody else, so there is something legitimate to bid on.
        self.online_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.online_auction.save()
        self.for_sale = Lot.objects.create(
            lot_name="A lot worth bidding on",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            user=self.user,
            quantity=1,
            reserve_price=5,
            active=True,
            date_end=self.online_auction.date_end,
        )
        # ``Lot.bidding_allowed_on`` holds bidding off a lot for its first 20 minutes, and
        # ``date_posted`` is auto_now_add, so it has to be pushed back after the insert.
        Lot.objects.filter(pk=self.for_sale.pk).update(date_posted=timezone.now() - datetime.timedelta(hours=1))
        self.for_sale.refresh_from_db()

    def test_a_bid_is_placed_through_the_lot_page_own_code(self):
        result = self._run("place_bid", {"lot_id": self.for_sale.pk, "amount": 10}, user=self.user_with_no_lots)
        self.assertNotIn("error", result)
        self.for_sale.refresh_from_db()
        self.assertEqual(self.for_sale.high_bidder.pk, self.user_with_no_lots.pk)

    def test_the_answer_says_it_cannot_be_taken_back(self):
        result = self._run("place_bid", {"lot_id": self.for_sale.pk, "amount": 10}, user=self.user_with_no_lots)
        self.assertIn("cannot_be_undone", result)
        # No undo block at all: there is nothing that reverses a bid, and offering one would be a
        # promise this site has never been able to keep.
        self.assertNotIn("undo", result)

    def test_bidding_on_your_own_lot_is_refused_in_the_lot_page_own_words(self):
        result = self._run("place_bid", {"lot_id": self.for_sale.pk, "amount": 10}, user=self.user)
        self.assertIn("error", result)
        self.assertIn("your own lot", result["error"])

    def test_under_the_reserve_is_refused_rather_than_rounded_up(self):
        result = self._run("place_bid", {"lot_id": self.for_sale.pk, "amount": 1}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.assertEqual(self.for_sale.bids.count(), 0)

    def test_no_amount_is_a_question(self):
        result = self._run("place_bid", {"lot_id": self.for_sale.pk}, user=self.user_with_no_lots)
        self.assertIn("more_info_needed", result)

    def test_a_host_is_told_to_ask_first(self):
        """``destructive`` here means "cannot be taken back", not "overwrites a row"."""
        from auctions.mcp import tools

        action = palette_actions.get_action("place_bid")
        self.assertTrue(action.destructive)
        self.assertTrue(action.asks_first)
        self.assertFalse(tools.idempotent(action), "two calls are two bids")


class InvoiceLineTests(SkillTestCase):
    """Looking at an invoice, and putting a line on it that isn't a lot."""

    def setUp(self):
        super().setUp()
        self.buyer_invoice, _created = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        Invoice.objects.filter(pk=self.buyer_invoice.pk).update(status="DRAFT", auction=self.in_person_auction)
        self.buyer_invoice.refresh_from_db()

    def _add(self, params, user=None):
        payload = {"auction": self.in_person_auction.slug}
        payload.update(params)
        return self._run("add_invoice_adjustment", payload, user=user or self.admin_user)

    def test_a_charge_is_added_as_a_line(self):
        result = self._add({"person": "555", "label": "raffle", "amount": 5})
        self.assertTrue(result.get("ok"), result)
        adjustment = self.buyer_invoice.invoiceadjustment_set.get()
        self.assertEqual(adjustment.adjustment_type, "ADD")
        self.assertEqual(adjustment.amount, 5)
        self.assertEqual(adjustment.notes, "raffle")
        self.assertEqual(adjustment.user, self.admin_user)

    def test_a_negative_amount_is_a_discount(self):
        result = self._add({"person": "555", "label": "helped pack up", "amount": -10})
        self.assertTrue(result.get("ok"), result)
        adjustment = self.buyer_invoice.invoiceadjustment_set.get()
        self.assertEqual(adjustment.adjustment_type, "DISCOUNT")
        self.assertEqual(adjustment.amount, 10)

    def test_a_settled_invoice_refuses(self):
        Invoice.objects.filter(pk=self.buyer_invoice.pk).update(status="PAID")
        result = self._add({"person": "555", "label": "raffle", "amount": 5})
        self.assertIn("error", result)
        self.assertEqual(self.buyer_invoice.invoiceadjustment_set.count(), 0)

    def test_nothing_is_not_an_adjustment(self):
        result = self._add({"person": "555", "label": "raffle", "amount": 0})
        self.assertIn("error", result)

    def test_a_line_with_no_label_is_a_question(self):
        result = self._add({"person": "555", "amount": 5})
        self.assertIn("more_info_needed", result)

    def test_a_participant_cannot_adjust_anybody(self):
        result = self._add({"person": "555", "label": "raffle", "amount": 5}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.assertEqual(self.buyer_invoice.invoiceadjustment_set.count(), 0)

    def test_the_change_is_in_the_auction_history(self):
        from auctions.models import AuctionHistory

        self._add({"person": "555", "label": "raffle", "amount": 5})
        self.assertTrue(
            AuctionHistory.objects.filter(
                auction=self.in_person_auction, applies_to="INVOICES", action__contains="raffle"
            ).exists()
        )

    def test_an_admin_can_read_somebody_elses_invoice(self):
        result = self._run(
            "find_invoice", {"person": "555", "auction": self.in_person_auction.slug}, user=self.admin_user
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["bidder_number"], "555")
        self.assertIn("url", result["invoice"])

    def test_a_participant_cannot_read_somebody_elses(self):
        result = self._run(
            "find_invoice", {"person": "502", "auction": self.in_person_auction.slug}, user=self.user_with_no_lots
        )
        self.assertIn("error", result)
        self.assertIn("admin", result["error"].lower())

    def test_a_participant_can_read_their_own(self):
        result = self._run("find_invoice", {"auction": self.in_person_auction.slug}, user=self.user_with_no_lots)
        self.assertTrue(result["found"])
        self.assertIsNone(result["person"])

    def test_the_lines_on_it_come_back_with_it(self):
        self._add({"person": "555", "label": "raffle", "amount": 5})
        result = self._run(
            "find_invoice", {"person": "555", "auction": self.in_person_auction.slug}, user=self.admin_user
        )
        self.assertEqual([line["amount"] for line in result["adjustments"]], ["+$5.00"])

    def test_an_invoice_answers_at_its_own_address(self):
        """``invoice://{auction}/{person}`` — the first resource about a pair of things."""
        from auctions.mcp import resources

        matched = resources.match(f"invoice://{self.in_person_auction.slug}/555")
        self.assertIsNotNone(matched)
        template, arguments = matched
        self.assertEqual(template.action, "find_invoice")
        self.assertEqual(arguments, {"auction": self.in_person_auction.slug, "person": "555"})

    def test_a_write_points_at_the_invoice_it_changed(self):
        from auctions.mcp import resources

        result = self._add({"person": "555", "label": "raffle", "amount": 5})
        links = resources.links_for("add_invoice_adjustment", result[palette_actions.KEY_ABOUT])
        self.assertIn(f"invoice://{self.in_person_auction.slug}/555", [link["uri"] for link in links])


class SendingAMembershipCardTests(ClubSkillTestCase):
    """Emailing a card: the caller's own, and — new — another member's."""

    def setUp(self):
        super().setUp()
        self.club.show_member_barcode = True
        self.club.save()
        self.club_member.membership_number = 4100
        self.club_member.save()
        # The caller's own membership, so the "no person" half has something to send.
        self.club_admin.membership_number = 4001
        self.club_admin.save()

    def _send(self, params, user=None):
        return self._run("send_membership_card", params, user=user or self.admin_user)

    def test_an_admin_can_send_another_members_card(self):
        result = self._send({"person": "Renewable Rita", "club": self.club.name})
        self.assertTrue(result.get("ok"), result)
        self.assertIn("rita@example.com", result["summary"])
        # Sent, not shown. See MembershipCardPrivacyTests for the whole of that line.
        self.assertNotIn("membership", result)

    def test_sending_somebody_elses_card_is_in_the_club_history(self):
        from auctions.models import ClubHistory

        self._send({"person": "Renewable Rita", "club": self.club.name})
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="rita@example.com").exists())

    def test_the_address_is_the_one_on_the_membership_and_not_one_in_the_request(self):
        """There is no parameter for an address, which is what makes widening this safe."""
        action = palette_actions.get_action("send_membership_card")
        self.assertEqual(set(action.params), {"person", "club"})

    def test_the_persons_name_is_never_read_as_the_club(self):
        """``_club_or_problem(also=…)`` takes a *club* hint; ``person`` is not one."""
        result = self._send({"person": "Renewable Rita"})
        self.assertTrue(result.get("ok"), result)

    def test_a_member_with_no_email_is_refused_rather_than_silently_dropped(self):
        ClubMember.objects.filter(pk=self.club_member.pk).update(email="")
        result = self._send({"person": "Renewable Rita", "club": self.club.name})
        self.assertIn("error", result)

    def test_a_do_not_contact_member_is_refused_exactly_as_the_page_refuses_them(self):
        ClubMember.objects.filter(pk=self.club_member.pk).update(contact_status="do_not_contact")
        result = self._send({"person": "Renewable Rita", "club": self.club.name})
        self.assertIn("error", result)
        self.assertIn("do-not-contact", result["error"])

    def test_somebody_with_no_club_permission_cannot_send_anybody_a_card(self):
        result = self._send({"person": "Renewable Rita", "club": self.club.name}, user=self.user_with_no_lots)
        self.assertIn("error", result)

    def test_the_card_is_no_longer_drawn_in_the_senders_chat_window(self):
        """It can send another member's card now, and their barcode is the wrong receipt for that."""
        from auctions.mcp import widgets

        self.assertNotIn("send_membership_card", widgets.TOOL_WIDGETS)


class MembershipCardPrivacyTests(ClubSkillTestCase):
    """A membership number and its barcode are a credential, not a club record.

    Running a club is permission to *send* a member their card, to the address on their membership.
    It is not permission to be handed the card — and an agent that has been handed one has put a
    scannable way through the door into a transcript. Every route to ``_membership_card`` is
    checked here, because a leak would be one keyword argument.
    """

    def setUp(self):
        super().setUp()
        self.club.show_member_barcode = True
        self.club.save()
        self.club_admin.membership_number = 4001
        self.club_admin.save()
        self.club_member.membership_number = 4100
        self.club_member.save()

    def _cards_in(self, result):
        """Every membership card object anywhere in one result."""
        found = []
        if isinstance(result, dict):
            for key, value in result.items():
                if isinstance(value, dict) and "barcode_url" in value:
                    found.append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "barcode_url" in item:
                            found.append(item)
                found.extend(self._cards_in(value) if isinstance(value, dict) else [])
        return found

    def test_an_admin_sending_another_members_card_is_not_handed_it(self):
        result = self._run(
            "send_membership_card", {"person": "Renewable Rita", "club": self.club.name}, user=self.admin_user
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self._cards_in(result), [], "an admin was handed another member's barcode")
        self.assertNotIn("4100", json.dumps(result), "an admin was handed another member's number")

    def test_sending_your_own_card_still_shows_it_to_you(self):
        result = self._run("send_membership_card", {"club": self.club.name}, user=self.admin_user)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["membership"]["membership_number"], 4001)

    def test_my_membership_has_no_way_to_name_anybody(self):
        action = palette_actions.get_action("my_membership")
        self.assertEqual(set(action.params), {"club"})
        self.assertEqual(action.aliases, set(), "an alias would be a second way to name a person")

    def test_a_club_admin_asking_for_a_card_gets_their_own(self):
        """``_my_memberships`` matches on ``ClubMember.user``; the club filter cannot reach past it."""
        result = self._run("my_membership", {"club": self.club.name}, user=self.admin_user)
        self.assertEqual(result["membership"]["membership_number"], 4001)
        self.assertNotIn("4100", json.dumps(result))

    def test_a_non_member_of_the_club_gets_nothing_at_all(self):
        result = self._run("my_membership", {"club": self.club.name}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.assertNotIn("4100", json.dumps(result))

    def test_no_other_read_hands_out_a_barcode(self):
        """The club-side reads an admin has: neither carries the scannable half."""
        for name, params in (
            ("list_club_members", {"club": self.club.name}),
            ("describe_club", {"club": self.club.name}),
        ):
            result = self._run(name, params, user=self.admin_user)
            self.assertNotIn("barcode", json.dumps(result).lower(), name)

    def test_only_the_callers_own_membership_can_build_a_card(self):
        """A guard on the shape rather than on one call site: every card comes from one helper."""
        card = palette_actions._membership_card(self.club_admin)
        self.assertIn("barcode_url", card)
        self.assertIn("membership_number", card)


class LotQueueTests(SkillTestCase):
    """ "What lot are we on?" — and, unlike the web page, not admins only."""

    def setUp(self):
        super().setUp()
        from auctions.models import LotQueueEntry

        self.queued = []
        for order, name in enumerate(["Ancistrus L144", "Java fern", "Ancistrus sp. 3", "A heater"]):
            lot = Lot.objects.create(
                lot_name=name,
                auction=self.in_person_auction,
                auctiontos_seller=self.admin_in_person_tos,
                quantity=1,
                custom_lot_number=f"200-{order}",
            )
            LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot, order=order)
            self.queued.append(lot)

    def _queue(self, params=None, user=None):
        payload = {"auction": self.in_person_auction.slug}
        payload.update(params or {})
        return self._run("lot_queue", payload, user=user or self.user_with_no_lots)

    def test_a_plain_bidder_can_read_the_queue(self):
        """The Lot queue *page* is admin-only. This is the one place the two differ on purpose."""
        result = self._queue()
        self.assertTrue(result["found"], result)
        self.assertEqual(result["current_lot"]["lot_number"], self.queued[0].lot_number_display)

    def test_it_is_a_read_and_asks_nobody(self):
        action = palette_actions.get_action("lot_queue")
        self.assertEqual(action.danger, palette_actions.DANGER_SAFE)

    def test_a_query_answers_are_there_any_ancistrus_selling_soon(self):
        result = self._queue({"query": "ancistrus"})
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [row["lot_number"] for row in result["queue"]],
            [self.queued[0].lot_number_display, self.queued[2].lot_number_display],
        )

    def test_a_match_reports_where_it_really_is_in_the_running_order(self):
        """Third in the queue has to read as third, not as "second in the two things I matched"."""
        result = self._queue({"query": "ancistrus"})
        self.assertEqual([row["position"] for row in result["queue"]], ["now", 3])

    def test_nothing_matching_says_so_rather_than_answering_with_the_whole_queue(self):
        result = self._queue({"query": "discus"})
        self.assertFalse(result["found"])
        self.assertEqual(result["queue"], [])
        self.assertIn("discus", result["summary"])

    def test_a_long_queue_pages(self):
        result = self._queue({"limit": 2})
        self.assertEqual(result["showing"], 2)
        self.assertIn("offset=2", result["summary"])

    def test_an_online_auction_has_no_queue_and_says_why(self):
        result = self._run("lot_queue", {"auction": self.online_auction.slug}, user=self.user_with_no_lots)
        self.assertIn("error", result)
        self.assertIn("online auction", result["error"])

    def test_the_selling_console_widget_is_gone(self):
        from auctions.mcp import widgets

        self.assertNotIn("lot_queue", widgets.TOOL_WIDGETS)
        self.assertNotIn("ui://auction.fish/winners", widgets.WIDGETS)
        for name in ("set_lot_winner", "no_sale", "undo_sale"):
            self.assertNotIn(name, widgets.TOOL_WIDGETS, f"{name} still draws a form")


class MemberListPrivacyTests(ClubSkillTestCase):
    """What a member row says, and what it deliberately no longer says."""

    def test_a_row_does_not_report_whether_they_have_signed_up_to_this_website(self):
        result = self._run("list_club_members", {"club": self.club.name}, user=self.admin_user)
        self.assertTrue(result["members"])
        for row in result["members"]:
            self.assertNotIn("has_an_account", row)

    def test_asking_who_has_no_account_still_answers(self):
        """The filter is a question somebody asked on purpose; the column was on every row."""
        result = self._run("list_club_members", {"club": self.club.name, "status": "no_account"}, user=self.admin_user)
        # Fenced, like every other name somebody else typed that reaches a model.
        self.assertEqual([row["name"] for row in result["members"]], ["«Renewable Rita»"])

    def test_listing_members_is_a_read(self):
        from auctions.mcp import tools

        self.assertTrue(tools.read_only(palette_actions.get_action("list_club_members")))
