"""Readers flagging a lot's species (``auctions/species_flags.py``), and the batch of feature requests built
with it: the admin connector's ``species_flags``, ``list_proposals`` and request lookups, make_club_admin by
email, ``go_to_page`` taking a lot's address, the two club banners, and the invoice totals following
adjustments as they are typed."""

import datetime
import json

from django.contrib.auth.models import User
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from auctions import palette_actions, palette_routes, services, species_flags
from auctions.models import (
    AgentProposal,
    AssistantSkillRequest,
    Auction,
    Club,
    ClubMember,
    Lot,
    SpeciesFlag,
    UserData,
)
from auctions.test_mcp_admin import AdminEndpointCase
from auctions.test_species import make_species
from auctions.tests import StandardTestCase


class SpeciesFlagTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.yellow_lab = make_species("Labidochromis", "caeruleus", "Yellow lab")
        cls.cherry = make_species("Neocaridina", "davidi", "Cherry shrimp")
        cls.flagged = Lot.objects.create(
            lot_name="Yellow labs",
            auction=cls.online_auction,
            user=cls.userB,
            auctiontos_seller=cls.tosB,
            quantity=1,
            reserve_price=5,
            species=cls.cherry,
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        cls.reader = User.objects.create_user(username="reader", password="testpassword", email="r@example.com")
        cls.reader2 = User.objects.create_user(username="reader2", password="testpassword", email="r2@example.com")

    def url(self):
        return reverse("lot_species_flag", kwargs={"pk": self.flagged.pk})

    def page(self, user):
        if user:
            self.client.force_login(user)
        else:
            self.client.logout()
        return self.client.get(self.flagged.lot_link).content.decode()

    def report(self, user):
        self.client.force_login(user)
        return self.client.post(self.url(), {"do": "report"}, HTTP_HX_REQUEST="true")

    def test_a_reader_reports_once_and_nobody_is_emailed(self):
        self.assertIn("Report incorrect species", self.page(self.reader))
        self.assertNotIn("Report incorrect species", self.page(None))
        mail.outbox = []
        response = self.report(self.reader)
        self.assertContains(response, "Reported")
        self.report(self.reader)
        self.assertEqual(SpeciesFlag.objects.filter(lot=self.flagged).count(), 1)
        self.assertEqual(mail.outbox, [])
        self.assertIn("Reported", self.page(self.reader))
        self.assertNotIn("Other people have flagged", self.page(self.reader))

    def test_the_seller_cannot_flag_their_own_lot_and_a_lot_without_species_has_no_button(self):
        self.assertNotIn("Report incorrect species", self.page(self.userB))
        self.report(self.userB)
        self.assertFalse(SpeciesFlag.objects.exists())
        Lot.objects.filter(pk=self.flagged.pk).update(species=None)
        self.assertNotIn("Report incorrect species", self.page(self.reader))
        self.report(self.reader)
        self.assertFalse(SpeciesFlag.objects.exists())

    def test_the_seller_sees_the_note_and_label_correct_settles_it(self):
        self.report(self.reader)
        self.assertIn("Other people have flagged this species as incorrect", self.page(self.userB))
        # Another reader never sees the note, and can't settle it.
        self.client.force_login(self.reader2)
        self.client.post(self.url(), {"do": "correct"}, HTTP_HX_REQUEST="true")
        self.assertTrue(species_flags.open_flags(self.flagged).exists())
        self.client.force_login(self.userB)
        response = self.client.post(self.url(), {"do": "correct"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "Other people have flagged")
        self.assertFalse(species_flags.open_flags(self.flagged).exists())
        self.assertNotIn("Other people have flagged", self.page(self.userB))
        # A later reader flags it again: open again.
        self.report(self.reader2)
        self.assertIn("Other people have flagged", self.page(self.userB))

    def test_fix_changes_the_species_through_set_lot_species_and_settles_the_flags(self):
        self.report(self.reader)
        self.client.force_login(self.userB)
        response = self.client.post(
            self.url(), {"do": "fix", "species": "Labidochromis caeruleus"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response["HX-Refresh"], "true")
        self.flagged.refresh_from_db()
        self.assertEqual(self.flagged.species, self.yellow_lab)
        self.assertFalse(species_flags.open_flags(self.flagged).exists())
        flag = SpeciesFlag.objects.get()
        self.assertEqual(species_flags.status(flag, self.flagged.species_id), "changed")

    def test_fix_says_why_when_nothing_matches_and_is_refused_once_sold(self):
        self.report(self.reader)
        self.client.force_login(self.userB)
        response = self.client.post(self.url(), {"do": "fix", "species": "Nothing madeupii"}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Nothing on the species list matches")
        Lot.objects.filter(pk=self.flagged.pk).update(auctiontos_winner=self.tosC, winning_price=10)
        self.assertNotIn(">Fix<", self.page(self.userB))
        self.client.post(self.url(), {"do": "fix", "species": "Labidochromis caeruleus"}, HTTP_HX_REQUEST="true")
        self.flagged.refresh_from_db()
        self.assertEqual(self.flagged.species, self.cherry)
        # The auction's admin still can.
        self.assertIn(">Fix<", self.page(self.user))
        self.client.force_login(self.user)
        self.client.post(self.url(), {"do": "fix", "species": "Labidochromis caeruleus"}, HTTP_HX_REQUEST="true")
        self.flagged.refresh_from_db()
        self.assertEqual(self.flagged.species, self.yellow_lab)

    def test_an_unknown_action_is_refused(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(self.url(), {"do": "delete"}).status_code, 400)


class SpeciesFlagsOnTheAdminEndpointTests(AdminEndpointCase):
    def test_flags_are_listed_by_status_without_who_flagged(self):
        species = make_species("Neocaridina", "davidi", "Cherry shrimp")
        Lot.objects.filter(pk=self.lot.pk).update(species=species)
        SpeciesFlag.objects.create(lot=self.lot, user=self.userB, species=species)
        SpeciesFlag.objects.create(lot=self.lot, user=self.user_with_no_lots, species=species)
        result = self.call("species_flags", {})
        self.assertFalse(result["isError"], result)
        rows = result["structuredContent"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flags"], 2)
        self.assertEqual(rows[0]["status"], "open")
        self.assertNotIn(self.userB.username, json.dumps(result))
        Lot.objects.filter(pk=self.lot.pk).update(species=None)
        self.assertEqual(self.call("species_flags", {})["structuredContent"]["total"], 0)
        changed = self.call("species_flags", {"status": "changed"})["structuredContent"]["rows"]
        self.assertEqual(changed[0]["status"], "changed")
        self.assertTrue(self.call("species_flags", {"status": "bogus"})["isError"])


class RequestAndProposalStatusTests(AdminEndpointCase):
    def test_one_request_by_number_on_both_endpoints(self):
        mine = AssistantSkillRequest.objects.create(user=self.user, skill="refund an invoice", reason="r")
        theirs = AssistantSkillRequest.objects.create(user=self.userB, skill="other", reason="r", status="done")
        request = RequestFactory().get("/")
        request.user = self.user
        result = palette_actions.run_action(request, "my_requests", {"request": mine.pk})
        self.assertEqual([row["request"] for row in result["requests"]], [mine.pk])
        self.assertIn("received", result["summary"])
        refused = palette_actions.run_action(request, "my_requests", {"request": theirs.pk})
        self.assertIn("isn't one of yours", refused["error"])
        rows = self.call("list_feature_requests", {"request": theirs.pk})["structuredContent"]["requests"]
        self.assertEqual([(row["request"], row["status"]) for row in rows], [(theirs.pk, "done")])
        self.assertTrue(self.call("list_feature_requests", {"request": 999999})["isError"])

    def test_list_proposals_reads_status_and_results(self):
        proposal = AgentProposal.objects.create(
            summary="Fix a lot",
            steps=[{"tool": "set_lot_species", "arguments": {}}],
            status=AgentProposal.STATUS_FAILED,
            results=[{"tool": "set_lot_species", "ok": False, "said": "Which lot?"}],
            proposed_by=self.owner,
        )
        AgentProposal.objects.create(summary="Another", steps=[], proposed_by=self.owner)
        data = self.call("list_proposals", {"proposal": proposal.pk})["structuredContent"]
        self.assertEqual(data["total"], 1)
        row = data["proposals"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("Which lot?", row["results"][0]["said"])
        self.assertEqual(self.call("list_proposals", {"status": "pending"})["structuredContent"]["total"], 1)
        self.assertEqual(self.call("list_proposals", {})["structuredContent"]["total"], 2)
        self.assertTrue(self.call("list_proposals", {"proposal": 999999})["isError"])
        # Not a tool for members, nor on /mcp/.
        self.assertNotIn("list_proposals", self.listed(url="/mcp/", key=self.member_key))


class MakeClubAdminByEmailTests(AdminEndpointCase):
    def approve(self):
        proposal = AgentProposal.objects.get(status=AgentProposal.STATUS_PENDING)
        self.client.force_login(self.owner)
        self.client.post(reverse("agent_proposals"), {"pk": proposal.pk, "decision": "approve"})
        proposal.refresh_from_db()
        return proposal

    def propose(self, arguments):
        return self.call(
            "propose_change", {"summary": "Admins", "steps": [{"tool": "make_club_admin", "arguments": arguments}]}
        )

    def test_an_existing_account_by_email_and_a_future_one(self):
        club = Club.objects.create(name="Harbor Aquarium Society")
        self.userB.email = "bee@example.com"
        self.userB.save()
        self.assertFalse(self.propose({"club": club.pk, "user": "Bee@Example.com"})["isError"])
        self.assertEqual(self.approve().status, AgentProposal.STATUS_APPLIED)
        self.assertTrue(ClubMember.objects.get(club=club, user=self.userB).permission_admin)

        self.assertFalse(self.propose({"club": club.pk, "user": "newcomer@example.com"})["isError"])
        self.assertEqual(self.approve().status, AgentProposal.STATUS_APPLIED)
        waiting = ClubMember.objects.get(club=club, email="newcomer@example.com")
        self.assertTrue(waiting.permission_admin)
        self.assertIsNone(waiting.user)
        newcomer = User.objects.create_user(username="newcomer", password="x", email="newcomer@example.com")
        waiting.save()
        waiting.refresh_from_db()
        self.assertEqual(waiting.user, newcomer)

        self.propose({"club": club.pk, "user": "nobody-yet@example.com", "admin": False})
        self.assertEqual(self.approve().status, AgentProposal.STATUS_APPLIED)
        self.assertFalse(ClubMember.objects.filter(email="nobody-yet@example.com").exists())
        self.assertTrue(self.propose({"club": club.pk, "user": "not@an@address"})["isError"])


class GoToPageLotAddressTests(StandardTestCase):
    def test_a_lot_page_address_names_the_lot(self):
        resolve = palette_routes._lot_from_hint
        superuser = User.objects.create_superuser("su", "su@example.com", "x")
        self.assertEqual(resolve(superuser, f"https://example.com/lots/{self.lot.pk}/a-test-lot/"), self.lot)
        self.assertEqual(resolve(superuser, f"/lots/{self.lot.pk}/"), self.lot)
        self.assertEqual(resolve(superuser, self.lot.lot_link), self.lot)
        self.assertEqual(resolve(superuser, f"http://example.com{self.lot.lot_link}"), self.lot)
        self.assertEqual(
            resolve(superuser, f"/auctions/{self.online_auction.slug}/lots/{self.lot.lot_number_int}"), self.lot
        )
        self.assertIsNone(resolve(superuser, "/lots/99999999/"))
        # Someone who can't see the lot doesn't get it by address either.
        self.assertIsNone(resolve(self.user_who_does_not_join, f"/lots/{self.lot.pk}/"))
        # An ordinary name still works.
        self.assertEqual(resolve(superuser, "A test lot"), self.lot)


class LinkClubBannerTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Harbor Aquarium Society")
        ClubMember.objects.create(club=self.club, user=self.user, name="me", permission_manage_auctions=True)
        Auction.objects.filter(pk=self.online_auction.pk).update(club=None)
        self.online_auction.refresh_from_db()

    def test_the_creator_is_offered_their_clubs_and_can_link_or_dismiss(self):
        self.assertEqual(services.clubs_to_link(self.online_auction, self.user), [self.club])
        self.assertEqual(services.clubs_to_link(self.online_auction, self.admin_user), [])
        superuser = User.objects.create_superuser("su", "su@example.com", "x")
        self.assertEqual(services.clubs_to_link(self.online_auction, superuser), [])
        self.client.force_login(self.user)
        url = reverse("auction_page_action", kwargs={"slug": self.online_auction.slug})
        other = Club.objects.create(name="Someone else's club")
        self.assertEqual(self.client.post(url, {"action": "link_club", "club": other.pk}).status_code, 403)
        self.client.post(url, {"action": "link_club", "club": self.club.pk})
        self.online_auction.refresh_from_db()
        self.assertEqual(self.online_auction.club, self.club)
        self.assertEqual(services.clubs_to_link(self.online_auction, self.user), [])

    def test_dont_show_again_is_for_this_auction(self):
        self.client.force_login(self.user)
        url = reverse("auction_page_action", kwargs={"slug": self.online_auction.slug})
        self.client.post(url, {"action": "dismiss_link_club_banner"})
        self.online_auction.refresh_from_db()
        self.assertTrue(self.online_auction.dismissed_link_club_banner)
        self.assertEqual(services.clubs_to_link(self.online_auction, self.user), [])
        self.assertEqual(services.clubs_to_link(self.in_person_auction, self.user), [self.club])


class ClubAuctionBannerTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Harbor Aquarium Society")
        ClubMember.objects.create(club=self.club, user=self.user, name="me", permission_manage_auctions=True)
        UserData.objects.filter(user=self.user).update(can_create_club_auctions=True)
        self.user.refresh_from_db()

    def test_shown_only_while_the_club_has_no_current_auction(self):
        self.assertTrue(services.can_start_club_auction(self.user, self.club))
        self.assertFalse(services.can_start_club_auction(self.userB, self.club))
        self.assertTrue(services.club_needs_an_auction(self.club))
        Auction.objects.filter(pk=self.online_auction.pk).update(club=self.club)
        # online_auction ended two days ago.
        self.assertTrue(services.club_needs_an_auction(self.club))
        Auction.objects.filter(pk=self.online_auction.pk).update(date_end=timezone.now() - datetime.timedelta(hours=2))
        self.assertFalse(services.club_needs_an_auction(self.club))

    def test_the_club_page_offers_it_and_the_new_auction_is_the_clubs(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug})).content.decode()
        self.assertIn("Create a new club auction", page)
        self.client.force_login(self.userB)
        self.assertNotIn(
            "Create a new club auction",
            self.client.get(reverse("club_detail", kwargs={"slug": self.club.slug})).content.decode(),
        )

    def test_a_slug_naming_online_or_clone_doesnt_change_the_auction_type(self):
        from django.test import RequestFactory as Factory

        request = Factory().get("/auctions/new/?type=offline&club=online-clone-club")
        self.assertNotEqual(request.GET.get("type"), "online")
        self.assertNotIn("online", request.GET)
        self.assertNotIn("clone", request.GET)


class InvoiceTotalsFollowTypingTests(StandardTestCase):
    def test_rows_save_as_typed_and_the_totals_redraw_queues(self):
        from django.template.loader import get_template

        row = get_template("auctions/partials/invoice_adjustment_row.html").template.source
        self.assertIn('hx-trigger="input delay:600ms, change, submit"', row)
        page = get_template("invoice.html").template.source
        self.assertIn('hx-trigger="adjustmentSaved from:body" hx-sync="this:queue last"', page)
        self.assertIn('hx-swap="innerHTML"', page)


class ReviewFixTests(SpeciesFlagTests):
    """What the adversarial review found."""

    def close_submission(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(
            lot_submission_end_date=timezone.now() - datetime.timedelta(days=1)
        )

    def test_after_submission_closes_the_seller_fixes_only_a_flagged_species_on_both_surfaces(self):
        self.close_submission()
        self.client.force_login(self.userB)
        self.client.post(self.url(), {"do": "fix", "species": "Labidochromis caeruleus"}, HTTP_HX_REQUEST="true")
        self.flagged.refresh_from_db()
        self.assertEqual(self.flagged.species, self.cherry)
        request = RequestFactory().get("/")
        request.user = self.userB
        request.palette_page = {}
        params = {"lot_id": self.flagged.pk, "species": "Labidochromis caeruleus"}
        self.assertIn("error", palette_actions.run_action(request, "set_lot_species", params))
        self.report(self.reader)
        result = palette_actions.run_action(request, "set_lot_species", params)
        self.assertTrue(result.get("ok"), result)

    def test_status_all_counts_a_reopened_flag_as_open(self):
        self.report(self.reader)
        self.client.force_login(self.userB)
        self.client.post(self.url(), {"do": "correct"})
        self.report(self.reader2)
        superuser = User.objects.create_superuser("su", "su@example.com", "x")
        request = RequestFactory().get("/")
        request.user = superuser
        from auctions.mcp import admin_species

        rows = admin_species.species_flags(request, {"status": "all"})["rows"]
        self.assertEqual((rows[0]["flags"], rows[0]["unanswered"], rows[0]["status"]), (2, 1, "open"))


class MakeClubAdminUsernameWithAtTests(MakeClubAdminByEmailTests):
    def test_a_username_that_looks_like_an_address_finds_its_account(self):
        club = Club.objects.create(name="Harbor Aquarium Society")
        odd = User.objects.create_user(username="bob@shop.example", password="x", email="bob@elsewhere.example")
        self.propose({"club": club.pk, "user": "bob@shop.example"})
        self.assertEqual(self.approve().status, AgentProposal.STATUS_APPLIED)
        self.assertTrue(ClubMember.objects.get(club=club, user=odd).permission_admin)
        self.assertFalse(ClubMember.objects.filter(club=club, user__isnull=True).exists())
