"""Tests for what the command palette assistant can *do*.

The first test is the important one, and like the route audit it isn't really a test of behaviour:
it is the rule that every capability on this site is either a palette skill or something somebody
has written down a reason for skipping.

``auctions/test_palette_routes.py`` already guarantees the assistant can reach every *page*. That
turned out to be half a guarantee: a URL that adds a club member is not a page, so the route audit
excused it as a "JSON/HTMX endpoint" and everyone moved on -- and the assistant went on not knowing
how to add a club member. This is the other half.
"""

from django.test import SimpleTestCase, override_settings

from auctions import palette_actions
from auctions.models import Club, ClubMember, Invoice, Lot, Watch
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

    def test_every_registered_action_reaches_a_superusers_prompt(self):
        """Nothing may be registered and then never described to anybody."""
        from auctions import palette_assist

        self.admin_user.is_superuser = True
        self.admin_user.save()
        prompt = palette_assist.build_system_prompt(self.admin_user)
        for name in palette_actions.ACTIONS:
            self.assertIn(name, prompt)

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
