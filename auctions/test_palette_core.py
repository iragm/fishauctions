"""The command palette itself, and the mobile surfaces that call into it."""

import datetime
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    PRIVACY_POLICY_SLUG,
    Auction,
    AuctionTOS,
    BlogPost,
    Club,
    ClubMember,
    CommandPalettePage,
    CommandPaletteSearch,
    Lot,
    PickupLocation,
)
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class CommandPaletteTests(StandardTestCase):
    """Tests for the command palette: search scoping, default items, search logging, and routing."""

    def _login(self, user):
        self.client.force_login(user)

    def _all_item_titles(self, response):
        titles = []
        for group in response.json()["groups"]:
            for item in group["items"]:
                titles.append(item["title"])
        return titles

    def _group_labels(self, response):
        return [group["label"] for group in response.json()["groups"]]

    def test_endpoints_require_login(self):
        client = Client()
        resp = client.get(reverse("command_palette"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url.lower())  # redirected to authenticate
        resp = client.post(reverse("command_palette_log"), {"search": "x"})
        self.assertEqual(resp.status_code, 302)

    def test_default_items_for_admin(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"))
        self.assertEqual(resp.status_code, 200)
        titles = self._all_item_titles(resp)
        self.assertTrue(any("View lots" in t for t in titles))
        self.assertTrue(any("View users" in t for t in titles))  # admin-only
        self.assertTrue(any("Quick checkout" in t for t in titles))  # admin-only

    def test_default_items_for_non_admin_shows_invoice(self):
        self.invoiceB.status = "UNPAID"
        self.invoiceB.save()
        self.userB.userdata.last_auction_used = self.online_auction
        self.userB.userdata.save()
        self._login(self.userB)
        resp = self.client.get(reverse("command_palette"))
        titles = self._all_item_titles(resp)
        self.assertTrue(any("invoice" in t.lower() for t in titles))
        self.assertFalse(any("View users" in t for t in titles))  # not an admin

    def _make_last_auction_pretty_much_over(self):
        """Push the online auction's pickup + end dates into the past so it's pretty_much_over."""
        self.location.pickup_time = timezone.now() - datetime.timedelta(hours=48)
        self.location.save()
        self.online_auction.date_end = timezone.now() - datetime.timedelta(hours=48)
        self.online_auction.save()
        self.assertTrue(self.online_auction.pretty_much_over)

    def test_default_items_pretty_much_over_shows_only_invoice(self):
        # Once the last auction is pretty_much_over, the palette should surface only its invoice,
        # not View lots / admin actions.
        self.invoice.status = "UNPAID"
        self.invoice.save()
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._make_last_auction_pretty_much_over()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"))
        titles = self._all_item_titles(resp)
        self.assertTrue(any("invoice" in t.lower() for t in titles))
        self.assertFalse(any("View lots" in t for t in titles))
        self.assertFalse(any("View users" in t for t in titles))
        self.assertFalse(any("Quick checkout" in t for t in titles))

    def test_view_lots_shortcut_hidden_when_pretty_much_over(self):
        # The dynamic "view lots" shortcut must not resolve for a pretty_much_over auction.
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._make_last_auction_pretty_much_over()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "view lots"})
        titles = self._all_item_titles(resp)
        self.assertFalse(any("View lots" in t for t in titles))

    def test_stats_shortcut_available_even_when_pretty_much_over(self):
        # Stats stay reachable after the auction is over.
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._make_last_auction_pretty_much_over()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "stats"})
        titles = self._all_item_titles(resp)
        self.assertTrue(any("Auction stats" in t for t in titles))

    def test_create_auction_shortcut(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "create auction"})
        titles = self._all_item_titles(resp)
        self.assertTrue(any("Create an auction" in t for t in titles))

    def test_set_location_shortcut_for_admin(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "set location"})
        titles = self._all_item_titles(resp)
        self.assertTrue(any("Set auction location" in t for t in titles))

    def test_print_search_surfaces_more_label_pages(self):
        # Task 4: "print" and "labels" should surface the auction /print/ hub's label pages, not
        # just the user's own label print.
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        for query in ("print", "labels"):
            titles = self._all_item_titles(self.client.get(reverse("command_palette"), {"q": query}))
            self.assertTrue(
                any("Print labels (whole auction)" in t for t in titles), f"bulk-print page missing for q={query!r}"
            )
            self.assertTrue(
                any("Print unprinted labels" in t for t in titles), f"unprinted-print page missing for q={query!r}"
            )

    def test_search_returns_auctions_and_lots(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "online"})
        self.assertIn("Auctions", self._group_labels(resp))
        resp = self.client.get(reverse("command_palette"), {"q": "test lot"})
        self.assertIn("Lots", self._group_labels(resp))

    def test_search_hides_unlisted_auction_and_lots(self):
        self.online_auction.title = "Visible Scope Auction"
        self.online_auction.save()
        self.lot.lot_name = "Visible Scope Lot"
        self.lot.save()
        hidden_auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Visible Scope Auction Hidden",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=5),
            date_start=timezone.now() + datetime.timedelta(days=1),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            promote_this_auction=False,
        )
        hidden_location = PickupLocation.objects.create(
            name="hidden location",
            auction=hidden_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=6),
        )
        hidden_tos = AuctionTOS.objects.create(
            user=self.admin_user,
            auction=hidden_auction,
            pickup_location=hidden_location,
        )
        Lot.objects.create(
            lot_name="Visible Scope Lot Hidden",
            auction=hidden_auction,
            auctiontos_seller=hidden_tos,
            quantity=1,
        )
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "Visible Scope Auction"})
        auction_titles = [i["title"] for g in resp.json()["groups"] if g["label"] == "Auctions" for i in g["items"]]
        self.assertIn(self.online_auction.title, auction_titles)
        self.assertNotIn(hidden_auction.title, auction_titles)
        resp = self.client.get(reverse("command_palette"), {"q": "Visible Scope Lot"})
        lot_subtitles = [i["subtitle"] for g in resp.json()["groups"] if g["label"] == "Lots" for i in g["items"]]
        self.assertIn(self.online_auction.title, lot_subtitles)
        self.assertNotIn(hidden_auction.title, lot_subtitles)

    def test_lot_search_excludes_deleted_parent_auction(self):
        deleted_auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Deleted Parent Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=5),
            date_start=timezone.now() + datetime.timedelta(days=1),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            is_deleted=True,
        )
        deleted_location = PickupLocation.objects.create(
            name="deleted location",
            auction=deleted_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=6),
        )
        deleted_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=deleted_auction,
            pickup_location=deleted_location,
        )
        Lot.objects.create(
            lot_name="Deleted Parent Lot",
            auction=deleted_auction,
            auctiontos_seller=deleted_tos,
            quantity=1,
        )
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "Deleted Parent Lot"})
        self.assertNotIn("Lots", self._group_labels(resp))

    def test_search_matches_page_shortcuts(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "preferences"})
        self.assertIn("Go to", self._group_labels(resp))

    def test_club_member_search_scoped_to_admins(self):
        club = Club.objects.create(name="Test Aquarium Club")
        ClubMember.objects.create(club=club, name="Secret Member", email="secret@example.com")
        # An admin of the club can find members.
        ClubMember.objects.create(club=club, user=self.user, name="Admin Member", permission_view=True)
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "Secret"})
        self.assertIn("Club members", self._group_labels(resp))
        # A user with no admin rights to the club cannot.
        self._login(self.userB)
        resp = self.client.get(reverse("command_palette"), {"q": "Secret"})
        self.assertNotIn("Club members", self._group_labels(resp))

    def test_log_upsert_keeps_one_row_and_records_click(self):
        self._login(self.user)
        page = CommandPalettePage.objects.first()
        resp = self.client.post(reverse("command_palette_log"), {"search": "pref", "result": "pending"})
        search_id = resp.json()["id"]
        # Refining the query updates the same row rather than creating a new one.
        resp = self.client.post(
            reverse("command_palette_log"), {"id": search_id, "search": "preferences", "result": "pending"}
        )
        self.assertEqual(resp.json()["id"], search_id)
        # Clicking a page result finalizes the row and bumps that page's hit counter.
        self.client.post(
            reverse("command_palette_log"),
            {
                "id": search_id,
                "search": "preferences",
                "result": "clicked",
                "result_type": "page",
                "result_object_id": page.pk,
            },
        )
        self.assertEqual(CommandPaletteSearch.objects.filter(user=self.user).count(), 1)
        row = CommandPaletteSearch.objects.get(pk=search_id)
        self.assertEqual(row.search, "preferences")
        self.assertEqual(row.result, "clicked")
        page.refresh_from_db()
        self.assertEqual(page.hits, 1)

    def test_recent_searches_appear_in_defaults(self):
        CommandPaletteSearch.objects.create(user=self.user, search="angelfish", result="abandoned")
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"))
        self.assertTrue(any("angelfish" in t for t in self._all_item_titles(resp)))

    def test_landing_page_in_person_admin_redirects_to_users(self):
        # A current in-person auction (not pretty_much_over) redirects its admin to the users list.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.in_person_auction.user_admin_link)

    def test_landing_page_pretty_much_over_in_person_admin_does_not_redirect_to_users(self):
        # Once the in-person auction is pretty_much_over, the stale users-list redirect is skipped.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(days=3)
        self.in_person_auction.save()
        self.assertTrue(self.in_person_auction.pretty_much_over)
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("home"))
        # Not redirected to the users list; either falls through to browse or renders home.
        if resp.status_code == 302:
            self.assertNotEqual(resp.url, self.in_person_auction.user_admin_link)

    def test_email_search_is_exact_and_includes_auctiontos(self):
        # online_auction is created by self.user, so they administer it.
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="findme@example.com",
            name="Find Me",
        )
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "findme@example.com"})
        labels = self._group_labels(resp)
        self.assertIn("Auction users", labels)
        # the link pre-populates ?query= so the record surfaces on the destination page
        urls = [i["url"] for g in resp.json()["groups"] if g["label"] == "Auction users" for i in g["items"]]
        self.assertTrue(any("query=" in u for u in urls))
        # exact match only: a partial email should not match
        resp = self.client.get(reverse("command_palette"), {"q": "findme@exa"})
        self.assertNotIn("Auction users", self._group_labels(resp))

    def test_auctiontos_tied_to_club_member_is_excluded(self):
        club = Club.objects.create(name="Linked Club")
        member = ClubMember.objects.create(club=club, email="linked@example.com", name="Linked Person")
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="linked@example.com",
            name="Linked Person",
            clubmember=member,
        )
        self._login(self.user)  # admin of online_auction, not of Linked Club
        resp = self.client.get(reverse("command_palette"), {"q": "linked@example.com"})
        self.assertNotIn("Auction users", self._group_labels(resp))

    def test_multi_club_member_results_include_all_admin_clubs(self):
        for name in ["Club Alpha", "Club Beta"]:
            club = Club.objects.create(name=name)
            ClubMember.objects.create(club=club, user=self.user, name=f"admin {name}", permission_view=True)
            ClubMember.objects.create(club=club, name="Zelda Tester", email=f"zelda-{club.pk}@example.com")
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "Zelda"})
        member_items = [i for g in resp.json()["groups"] if g["label"] == "Club members" for i in g["items"]]
        self.assertEqual(len(member_items), 2)

    def test_synonym_matches_page_shortcut(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "update email"})
        urls = [i["url"] for g in resp.json()["groups"] if g["label"] == "Go to" for i in g["items"]]
        self.assertIn(reverse("account_email"), urls)

    def test_auction_field_name_matches_settings_page(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "tax"})
        urls = [i["url"] for g in resp.json()["groups"] if g["label"] == "Go to" for i in g["items"]]
        self.assertIn(reverse("edit_auction", kwargs={"slug": self.online_auction.slug}), urls)

    def test_auction_field_search_only_includes_editable_form_fields(self):
        # paypal_email_address is a model field that lives on no form, so "paypal" must not be
        # advertised as a configurable auction setting ("configure paypal email address").
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "paypal"})
        edit_url = reverse("edit_auction", kwargs={"slug": self.online_auction.slug})
        settings_items = [
            i for g in resp.json()["groups"] if g["label"] == "Go to" for i in g["items"] if i["url"] == edit_url
        ]
        # The editable "PayPal payments" toggle (enable_online_payments) still surfaces...
        self.assertTrue(settings_items)
        for item in settings_items:
            self.assertIn("PayPal payments", item["subtitle"])
            # ...but the un-editable paypal_email_address field never does.
            self.assertNotIn("email address", item["subtitle"].lower())

    def test_set_winners_excluded_for_online_auction(self):
        # Online auctions pick winners from bids; the set-lot-winners shortcut must not appear.
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "set lot winners"})
        urls = [i["url"] for g in resp.json()["groups"] for i in g["items"]]
        self.assertNotIn(self.online_auction.set_lot_winners_link, urls)

    def test_set_winners_shown_for_open_in_person_auction(self):
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=2)
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "set lot winners"})
        urls = [i["url"] for g in resp.json()["groups"] for i in g["items"]]
        self.assertIn(self.in_person_auction.set_lot_winners_link, urls)

    def test_add_lot_shortcuts_follow_auction_lot_entry_mode(self):
        # Keep the auction current (started recently) so it isn't pretty_much_over, which would
        # otherwise hide the add-lot shortcut. date_end must stay after date_start or the pre_save
        # signal swaps them.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "add lot"})
        urls = [i["url"] for g in resp.json()["groups"] for i in g["items"]]
        self.assertIn(reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug}), urls)
        self.assertNotIn(self.in_person_auction.add_lot_link, urls)

        self.in_person_auction.allow_bulk_adding_lots = False
        self.in_person_auction.save()
        resp = self.client.get(reverse("command_palette"), {"q": "add lot"})
        urls = [i["url"] for g in resp.json()["groups"] for i in g["items"]]
        self.assertIn(self.in_person_auction.add_lot_link, urls)

    def test_command_palette_response_is_not_cached(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "online"})
        self.assertIn("private", resp["Cache-Control"])
        self.assertIn("no-store", resp["Cache-Control"])

    def test_print_and_label_shortcuts(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        print_labels_url = reverse("print_my_labels", kwargs={"slug": self.online_auction.slug})
        for q in ["print", "label"]:
            urls = [
                i["url"]
                for g in self.client.get(reverse("command_palette"), {"q": q}).json()["groups"]
                for i in g["items"]
            ]
            self.assertIn(print_labels_url, urls, f"print labels shortcut missing for '{q}'")
            self.assertIn(reverse("printing"), urls, f"printing preferences shortcut missing for '{q}'")
        # The per-auction label setup is admin-only and keyed off "label", not "print".
        label_urls = [
            i["url"]
            for g in self.client.get(reverse("command_palette"), {"q": "label"}).json()["groups"]
            for i in g["items"]
        ]
        self.assertIn(reverse("auction_label_config", kwargs={"slug": self.online_auction.slug}), label_urls)

    def test_bounce_is_recorded(self):
        self._login(self.user)
        resp = self.client.post(reverse("command_palette_log"), {"search": "zzzznotathing", "result": "bounce"})
        row = CommandPaletteSearch.objects.get(pk=resp.json()["id"])
        self.assertEqual(row.result, "bounce")

    def test_finalize_without_id_records_the_search(self):
        # The client finalizes a search (e.g. a sendBeacon on navigation away) even when the
        # in-progress row's id hasn't come back yet. A finalize with no id must still record the
        # search rather than drop it, which is how searches abandoned by navigating used to vanish.
        self._login(self.user)
        resp = self.client.post(reverse("command_palette_log"), {"search": "guppy", "result": "abandoned"})
        row = CommandPaletteSearch.objects.get(pk=resp.json()["id"])
        self.assertEqual(row.search, "guppy")
        self.assertEqual(row.result, "abandoned")

    def test_ready_invoice_is_top_default_result(self):
        self.invoice.status = "UNPAID"
        self.invoice.save()
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"))
        first = resp.json()["groups"][0]["items"][0]
        self.assertEqual(first["type"], "invoice")

    def _all_urls(self, resp):
        return [i["url"] for g in resp.json()["groups"] for i in g["items"]]

    def _go_to_urls(self, resp):
        return [i["url"] for g in resp.json()["groups"] if g["label"] == "Go to" for i in g["items"]]

    def _make_palette_club(self, user, **permissions):
        """Create a club, make ``user`` a member with the given permissions, and record it as the
        user's last club used so the palette's club shortcuts target it."""
        club = Club.objects.create(name="Palette Club")
        ClubMember.objects.create(club=club, user=user, name="Member", **permissions)
        user.userdata.last_club_used = club
        user.userdata.save()
        return club

    def test_api_search_returns_club_api_keys_page(self):
        club = self._make_palette_club(self.user, permission_edit_club=True)
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "api"})
        self.assertIn(reverse("club_api_keys", kwargs={"slug": club.slug}), self._go_to_urls(resp))

    def test_username_search_returns_preferences_and_change_username(self):
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "username"})
        urls = self._go_to_urls(resp)
        self.assertIn(reverse("change_username"), urls)
        self.assertIn(reverse("preferences"), urls)
        # The preferences hit names the specific field it would change.
        pref_items = [
            i
            for g in resp.json()["groups"]
            if g["label"] == "Go to"
            for i in g["items"]
            if i["url"] == reverse("preferences")
        ]
        self.assertTrue(any("username" in i["subtitle"].lower() for i in pref_items))

    def test_club_settings_field_search_returns_settings_page(self):
        club = self._make_palette_club(self.user, permission_edit_club=True)
        self._login(self.user)
        resp = self.client.get(reverse("command_palette"), {"q": "facebook"})
        self.assertIn(reverse("club_edit", kwargs={"slug": club.slug}), self._go_to_urls(resp))

    def test_club_shortcuts_scoped_to_last_club_used(self):
        club_a = Club.objects.create(name="Club A Palette")
        club_b = Club.objects.create(name="Club B Palette")
        ClubMember.objects.create(club=club_a, user=self.user, name="A", permission_view=True)
        ClubMember.objects.create(club=club_b, user=self.user, name="B", permission_view=True)
        self.user.userdata.last_club_used = club_a
        self.user.userdata.save()
        self._login(self.user)
        urls = self._go_to_urls(self.client.get(reverse("command_palette"), {"q": "members"}))
        self.assertIn(reverse("club_admin", kwargs={"slug": club_a.slug}), urls)
        self.assertNotIn(reverse("club_admin", kwargs={"slug": club_b.slug}), urls)

    def test_lot_search_excludes_promoted_auction_user_has_not_joined(self):
        promoted = Auction.objects.create(
            created_by=self.admin_user,
            title="Promoted Palette Auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=5),
            date_start=timezone.now() + datetime.timedelta(days=1),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            promote_this_auction=True,
        )
        loc = PickupLocation.objects.create(
            name="promoted loc", auction=promoted, pickup_time=timezone.now() + datetime.timedelta(days=6)
        )
        seller = AuctionTOS.objects.create(user=self.admin_user, auction=promoted, pickup_location=loc)
        Lot.objects.create(lot_name="Promoted Palette Lot", auction=promoted, auctiontos_seller=seller, quantity=1)
        self._login(self.user)  # self.user has not joined the promoted auction
        resp = self.client.get(reverse("command_palette"), {"q": "Promoted Palette"})
        # The auction itself is visible (promoted), but its lots are not searchable by a non-participant.
        self.assertIn("Auctions", self._group_labels(resp))
        self.assertNotIn("Lots", self._group_labels(resp))

    def test_checkin_membership_card_shown_for_unchecked_in_member(self):
        club = Club.objects.create(name="Check-in Palette Club")
        member_user = User.objects.create_user(username="cm_palette", password="testpassword", email="cm@example.com")
        member = ClubMember.objects.create(club=club, user=member_user, name="CM Palette")
        auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Check-in Palette Auction",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=2),
            date_end=timezone.now() + datetime.timedelta(days=3),
            club=club,
            manage_users_through_club="checkin",
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.assertTrue(auction.use_check_in_mode)
        member_user.userdata.last_auction_used = auction
        member_user.userdata.save()
        self._login(member_user)
        urls = self._all_urls(self.client.get(reverse("command_palette")))
        self.assertIn(reverse("club_member_by_uuid", kwargs={"slug": club.slug, "uuid": member.uuid}), urls)

    def test_club_default_falls_back_to_home_without_manage_permissions(self):
        club = Club.objects.create(name="Home Fallback Club")
        ClubMember.objects.create(club=club, user=self.userB, name="Plain Member")
        self.userB.userdata.last_club_used = club
        self.userB.userdata.save()
        self._login(self.userB)
        urls = self._all_urls(self.client.get(reverse("command_palette")))
        self.assertIn(reverse("club_detail", kwargs={"slug": club.slug}), urls)
        self.assertNotIn(reverse("club_admin", kwargs={"slug": club.slug}), urls)

    def test_club_default_shows_members_with_manage_permission(self):
        club = self._make_palette_club(self.userB, permission_view=True)
        self._login(self.userB)
        urls = self._all_urls(self.client.get(reverse("command_palette")))
        self.assertIn(reverse("club_admin", kwargs={"slug": club.slug}), urls)

    def test_membership_card_search_terms_return_uuid_card(self):
        club = self._make_palette_club(self.user)
        member = ClubMember.objects.get(club=club, user=self.user)
        card_url = reverse("club_member_by_uuid", kwargs={"slug": club.slug, "uuid": member.uuid})
        self._login(self.user)
        for term in ["card", "membership", "member", club.name]:
            urls = self._all_urls(self.client.get(reverse("command_palette"), {"q": term}))
            self.assertIn(card_url, urls, f"membership card missing for query '{term}'")

    def test_membership_pay_page_not_in_palette(self):
        club = self._make_palette_club(self.user, permission_edit_club=True)
        pay_url = reverse("club_membership_pay", kwargs={"slug": club.slug})
        self._login(self.user)
        for term in ["renew", "membership", "pay dues", "dues"]:
            urls = self._all_urls(self.client.get(reverse("command_palette"), {"q": term}))
            self.assertNotIn(pay_url, urls, f"pay page should not appear for '{term}'")

    def test_analytics_view_is_admin_only(self):
        CommandPaletteSearch.objects.create(user=self.user, search="needle", result="bounce")
        self._login(self.user)
        resp = self.client.get(reverse("command_palette_analytics"))
        self.assertEqual(resp.status_code, 302)  # non-superuser redirected
        superuser = User.objects.create_superuser("cp_super", "cp_super@example.com", "testpassword")
        self.client.force_login(superuser)
        resp = self.client.get(reverse("command_palette_analytics"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "needle")


class MobileCommandPaletteTests(StandardTestCase):
    """The /api/mobile/ command-palette endpoints reuse the shared command_palette module, so this
    only covers what differs from the web: JWT (not session) auth, the JSON contract the app reads,
    and that search-logging is wired through the same log_search upsert."""

    def setUp(self):
        super().setUp()
        from rest_framework_simplejwt.tokens import RefreshToken

        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self.bearer = {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(self.user).access_token}"}
        self.search_url = reverse("mobile-command-palette")
        self.log_url = reverse("mobile-command-palette-log")

    def _labels(self, resp):
        return [g["label"] for g in resp.json()["groups"]]

    def test_requires_jwt_not_session(self):
        # No token at all is rejected (DRF answers 401/403 depending on the auth header).
        self.assertIn(self.client.get(self.search_url).status_code, (401, 403))
        # A web session must NOT grant access to the mobile endpoints (IsMobileAuthenticated only
        # accepts JWT), so a session-authenticated request is still denied.
        self.client.force_login(self.user)
        self.assertIn(self.client.get(self.search_url).status_code, (401, 403))

    def test_search_returns_grouped_results(self):
        resp = self.client.get(self.search_url, {"q": "online"}, **self.bearer)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Auctions", self._labels(resp))
        resp = self.client.get(self.search_url, {"q": "test lot"}, **self.bearer)
        self.assertIn("Lots", self._labels(resp))

    def test_item_shape_and_default_items(self):
        resp = self.client.get(self.search_url, **self.bearer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        items = [i for g in resp.json()["groups"] for i in g["items"]]
        self.assertTrue(items)
        # Admin of the most recent auction sees the "View lots" default; every item carries the
        # full contract the mobile client renders.
        self.assertTrue(any("View lots" in i["title"] for i in items))
        for key in ("type", "title", "subtitle", "url", "icon", "id"):
            self.assertIn(key, items[0])

    def test_log_upsert_and_click_bumps_page_hits(self):
        page = CommandPalettePage.objects.first()
        resp = self.client.post(self.log_url, {"search": "pref", "result": "pending"}, **self.bearer)
        self.assertEqual(resp.status_code, 200)
        search_id = resp.json()["id"]
        # Refining the query updates the same row (one row per session).
        resp = self.client.post(
            self.log_url, {"id": search_id, "search": "preferences", "result": "pending"}, **self.bearer
        )
        self.assertEqual(resp.json()["id"], search_id)
        self.client.post(
            self.log_url,
            {
                "id": search_id,
                "search": "preferences",
                "result": "clicked",
                "result_type": "page",
                "result_object_id": page.pk,
            },
            **self.bearer,
        )
        self.assertEqual(CommandPaletteSearch.objects.filter(user=self.user).count(), 1)
        self.assertEqual(CommandPaletteSearch.objects.get(pk=search_id).result, "clicked")
        page.refresh_from_db()
        self.assertEqual(page.hits, 1)


class MobileMyClubsTests(StandardTestCase):
    """/api/mobile/clubs/mine/ — the clubs the JWT user belongs to, with the is_admin flag.

    Mirrors the web ``user_clubs`` membership scoping (non-deleted ClubMember), so this covers
    what differs on mobile: JWT (not session) auth, name ordering, the is_admin flag, and the
    {name, slug, url, icon_url, is_admin} contract the app reads.
    """

    def setUp(self):
        super().setUp()
        from rest_framework_simplejwt.tokens import RefreshToken

        self.bearer = {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(self.user).access_token}"}
        self.url = reverse("mobile-clubs-mine")
        # "Beta" sorts before "Alpha club" only by name, so ordering is observable regardless of pk.
        self.admin_club = Club.objects.create(name="Alpha club")
        self.member_club = Club.objects.create(name="Beta club")
        # A club the user does NOT belong to must never appear.
        self.other_club = Club.objects.create(name="Zeta club")
        ClubMember.objects.create(club=self.admin_club, user=self.user, name="Me", permission_admin=True)
        ClubMember.objects.create(club=self.member_club, user=self.user, name="Me")

    def test_requires_jwt_not_session(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))
        # A web session must NOT grant access — IsMobileAuthenticated only accepts JWT.
        self.client.force_login(self.user)
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_lists_only_my_clubs_sorted_with_admin_flag(self):
        resp = self.client.get(self.url, **self.bearer)
        self.assertEqual(resp.status_code, 200)
        clubs = resp.json()["clubs"]
        # Only the two clubs the user belongs to, sorted by name.
        self.assertEqual([c["slug"] for c in clubs], [self.admin_club.slug, self.member_club.slug])
        by_slug = {c["slug"]: c for c in clubs}
        # is_admin follows permission_admin on the membership.
        self.assertTrue(by_slug[self.admin_club.slug]["is_admin"])
        self.assertFalse(by_slug[self.member_club.slug]["is_admin"])
        # Full contract, and url is the server-relative web club page.
        for club in clubs:
            self.assertEqual(set(club), {"name", "slug", "url", "icon_url", "is_admin"})
        self.assertEqual(
            by_slug[self.admin_club.slug]["url"],
            reverse("club_detail", kwargs={"slug": self.admin_club.slug}),
        )
        # No club icon set, so icon_url is null.
        self.assertIsNone(by_slug[self.admin_club.slug]["icon_url"])

    def test_deleted_membership_is_excluded(self):
        ClubMember.objects.filter(club=self.member_club, user=self.user).update(is_deleted=True)
        resp = self.client.get(self.url, **self.bearer)
        slugs = [c["slug"] for c in resp.json()["clubs"]]
        self.assertEqual(slugs, [self.admin_club.slug])

    def test_no_memberships_returns_empty_list(self):
        ClubMember.objects.filter(user=self.user).delete()
        resp = self.client.get(self.url, **self.bearer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"clubs": []})


class MobileLabelTests(StandardTestCase):
    """/api/mobile/labels/<pk>/ — authorization (seller or auction admin) and PNG rendering."""

    def setUp(self):
        super().setUp()
        from rest_framework_simplejwt.tokens import RefreshToken

        self._RefreshToken = RefreshToken
        self.url = reverse("mobile-label-lot", kwargs={"pk": self.lot.pk})  # self.lot is sold by self.user

    def _bearer(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._RefreshToken.for_user(user).access_token}"}

    def test_returns_png_for_lot_seller(self):
        resp = self.client.get(self.url, **self._bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content[:8], b"\x89PNG\r\n\x1a\n")

    def test_auction_admin_can_print_other_sellers_lot(self):
        resp = self.client.get(self.url, **self._bearer(self.admin_user))
        self.assertEqual(resp.status_code, 200)

    def test_forbidden_for_non_owner_non_admin(self):
        resp = self.client.get(self.url, **self._bearer(self.userB))
        self.assertEqual(resp.status_code, 403)

    def test_missing_lot_is_404(self):
        url = reverse("mobile-label-lot", kwargs={"pk": 99999999})
        self.assertEqual(self.client.get(url, **self._bearer(self.user)).status_code, 404)

    def test_unsupported_format_is_400(self):
        resp = self.client.get(self.url, {"fmt": "zpl"}, **self._bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_default_resolution_is_600x400(self):
        from io import BytesIO

        from PIL import Image

        resp = self.client.get(self.url, **self._bearer(self.user))
        self.assertEqual(Image.open(BytesIO(resp.content)).size, (600, 400))

    def test_resolution_param_sizes_the_png(self):
        from io import BytesIO

        from PIL import Image

        resp = self.client.get(self.url, {"resolution": "96x64", "dpi": "203"}, **self._bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        img = Image.open(BytesIO(resp.content))
        self.assertEqual(img.size, (96, 64))
        # PIL round-trips DPI through the PNG pixels-per-meter chunk, so it comes back ~203.0.
        dpi_x, dpi_y = img.info.get("dpi")
        self.assertEqual((round(dpi_x), round(dpi_y)), (203, 203))

    def test_malformed_resolution_is_400(self):
        resp = self.client.get(self.url, {"resolution": "not-a-size"}, **self._bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_out_of_range_resolution_is_400(self):
        resp = self.client.get(self.url, {"resolution": "99999x99999"}, **self._bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))


class MobileConfigTests(TestCase):
    """/api/mobile/config/ — public, unauthenticated deployment config the app reads before sign-in."""

    def setUp(self):
        self.url = reverse("mobile-config")
        # privacy_policy_url is only offered when the page exists. The post is seeded by migration,
        # but a TransactionTestCase earlier in the run can truncate it away, so make it explicit.
        BlogPost.objects.get_or_create(slug=PRIVACY_POLICY_SLUG, defaults={"title": "Privacy"})

    @override_settings(
        SQUARE_APPLICATION_ID="sq0idp-test",
        SQUARE_ENVIRONMENT="sandbox",
        GOOGLE_OAUTH_CLIENT_ID="123.apps.googleusercontent.com",
        NAVBAR_BRAND="Test Auctions",
        # Pinned empty so the exact-response assertion doesn't depend on whether the .env of the
        # machine running the tests happens to have the Firebase config files; the firebase block
        # has its own tests below.
        FIREBASE_CLIENT_CONFIG={},
        # Same reason: pin the social providers so this doesn't depend on the running machine's
        # .env. Both keys are always present, whatever their value -- the app hides a provider's
        # button when its key is empty, so the key going missing would be a silent breakage.
        APPLE_ALLOWED_AUDIENCES=["com.fishauctions.app"],
        FACEBOOK_APP_ID="1234567890",
    )
    def test_returns_public_config_without_auth(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        # The drawer is the one per-user block and has its own tests (auctions/test_mobile_menu.py);
        # here it is only checked for being present and signed-out, then dropped so the rest of the
        # response can still be asserted exactly.
        menu = payload.pop("menu")
        self.assertEqual([section["id"] for section in menu["sections"]], ["main", "about"])
        self.assertEqual(
            payload,
            {
                "square_application_id": "sq0idp-test",
                "square_environment": "sandbox",
                "google_server_client_id": "123.apps.googleusercontent.com",
                # Which social sign-in buttons to draw. Apple is a boolean because the native flow's
                # audience is the app's own bundle id and needs nothing at runtime; Facebook's app id
                # is public by construction (it's compiled into the app and registered as an
                # fb<app-id> URL scheme). Neither secret is ever sent -- see test_exposes_no_secrets.
                "apple_sign_in_enabled": True,
                "facebook_app_id": "1234567890",
                "brand_name": "Test Auctions",
                "icon_url": "http://testserver/static/android-chrome-512x512.png",
                # Apple requires both to be linkable from inside the app at sign-up.
                "terms_url": "/tos/",
                "privacy_policy_url": "/privacy/",
            },
        )

    def test_exposes_no_secrets(self):
        # Guard against a secret ever being added to this public endpoint: the response keys are a
        # fixed allowlist of public values, and none of the bytes leak a server-side secret.
        with override_settings(
            SQUARE_CLIENT_SECRET="sq0csp-supersecret",
            SECRET_KEY="django-secret-key-value",
            FIREBASE_CLIENT_CONFIG={},  # same reason as above: keep the key allowlist exact
            # Each social provider has a public half that belongs here and a secret half that never
            # does. Set both so the assertions below prove the line is drawn in the right place.
            FACEBOOK_APP_ID="1234567890",
            FACEBOOK_APP_SECRET="fb-app-secret-value",
            APPLE_ALLOWED_AUDIENCES=["com.fishauctions.app"],
            APPLE_SIGN_IN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----apple-p8-value-----END PRIVATE KEY-----",
        ):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.json().keys()),
            {
                "square_application_id",
                "square_environment",
                "google_server_client_id",
                "apple_sign_in_enabled",
                "facebook_app_id",
                "brand_name",
                "icon_url",
                "terms_url",
                "privacy_policy_url",
                # Titles and paths of navbar links, nothing else -- see auctions/mobile/menu.py.
                "menu",
            },
        )
        self.assertNotIn(b"sq0csp-supersecret", resp.content)
        self.assertNotIn(b"django-secret-key-value", resp.content)
        # The Facebook app *id* is public (compiled into the app, registered as a URL scheme); the
        # app secret and Apple's .p8 signing key are not, and must never travel to a device.
        self.assertIn(b"1234567890", resp.content)
        self.assertNotIn(b"fb-app-secret-value", resp.content)
        self.assertNotIn(b"apple-p8-value", resp.content)

    def test_includes_firebase_block_for_configured_platforms(self):
        android = {
            "package_name": "com.example.auction",
            "api_key": "AIzaAndroid",
            "app_id": "1:111:android:aaa",
            "messaging_sender_id": "111",
            "project_id": "demo-project",
        }
        ios = {
            "bundle_id": "com.example.auction",
            "api_key": "AIzaIos",
            "app_id": "1:111:ios:bbb",
            "messaging_sender_id": "111",
            "project_id": "demo-project",
        }
        with override_settings(FIREBASE_CLIENT_CONFIG={"android": android, "ios": ios}):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["firebase"], {"android": android, "ios": ios})

    def test_omits_firebase_block_when_no_platform_configured(self):
        with override_settings(FIREBASE_CLIENT_CONFIG={}):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("firebase", resp.json())


class FirebaseClientConfigParsingTests(TestCase):
    """Parsing the public Firebase client files (google-services.json / GoogleService-Info.plist)."""

    def _write(self, name, content, *, binary=False):

        path = Path(tempfile.mkdtemp()) / name
        mode = "wb" if binary else "w"
        with path.open(mode) as f:
            f.write(content)
        return str(path)

    def _google_services_json(self):
        import json as _json

        return self._write(
            "google-services.json",
            _json.dumps(
                {
                    "project_info": {
                        "project_number": "111122223333",
                        "project_id": "demo-project",
                        "storage_bucket": "demo-project.appspot.com",
                    },
                    "client": [
                        {
                            "client_info": {
                                "mobilesdk_app_id": "1:111122223333:android:aaaabbbb",
                                "android_client_info": {"package_name": "com.example.auction"},
                            },
                            "api_key": [{"current_key": "AIzaSyAndroidKey"}],
                        }
                    ],
                }
            ),
        )

    def _google_service_info_plist(self):
        import plistlib

        return self._write(
            "GoogleService-Info.plist",
            plistlib.dumps(
                {
                    "BUNDLE_ID": "com.example.auction",
                    "API_KEY": "AIzaSyIosKey",
                    "GOOGLE_APP_ID": "1:111122223333:ios:ccccdddd",
                    "GCM_SENDER_ID": "111122223333",
                    "PROJECT_ID": "demo-project",
                }
            ),
            binary=True,
        )

    def test_parses_both_platforms(self):
        from fishauctions.firebase_config import load_firebase_client_config

        config = load_firebase_client_config(self._google_services_json(), self._google_service_info_plist())
        self.assertEqual(
            config,
            {
                "android": {
                    "package_name": "com.example.auction",
                    "api_key": "AIzaSyAndroidKey",
                    "app_id": "1:111122223333:android:aaaabbbb",
                    "messaging_sender_id": "111122223333",
                    "project_id": "demo-project",
                },
                "ios": {
                    "bundle_id": "com.example.auction",
                    "api_key": "AIzaSyIosKey",
                    "app_id": "1:111122223333:ios:ccccdddd",
                    "messaging_sender_id": "111122223333",
                    "project_id": "demo-project",
                },
            },
        )

    def test_omits_platform_with_unset_path(self):
        from fishauctions.firebase_config import load_firebase_client_config

        config = load_firebase_client_config(self._google_services_json(), "")
        self.assertIn("android", config)
        self.assertNotIn("ios", config)

    def test_empty_when_neither_configured(self):
        from fishauctions.firebase_config import load_firebase_client_config

        self.assertEqual(load_firebase_client_config("", ""), {})

    def test_malformed_or_missing_files_degrade_to_none(self):
        from fishauctions.firebase_config import load_android_config, load_ios_config

        self.assertIsNone(load_android_config("/no/such/google-services.json"))
        self.assertIsNone(load_ios_config("/no/such/GoogleService-Info.plist"))
        # A JSON file missing the expected keys must not raise.
        self.assertIsNone(load_android_config(self._write("bad.json", '{"unexpected": true}')))
        # A plist that isn't a valid plist must not raise.
        self.assertIsNone(load_ios_config(self._write("bad.plist", "not a plist", binary=False)))


class SingleLotLabelPngTests(StandardTestCase):
    """The web single-lot label endpoint can also emit a PNG (?format=png) via the shared renderer,
    with the same ?resolution / ?dpi controls as the mobile endpoint; default stays the PDF sheet."""

    def setUp(self):
        super().setUp()
        self.url = reverse("single_lot_label", kwargs={"pk": self.lot.pk})  # self.lot's seller is self.user
        self.client.login(username="my_lot", password="testpassword")

    def test_default_is_pdf(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp["Content-Type"], "image/png")

    def test_format_png_returns_png(self):
        resp = self.client.get(self.url, {"format": "png"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content[:8], b"\x89PNG\r\n\x1a\n")

    def test_png_honors_resolution(self):
        from io import BytesIO

        from PIL import Image

        resp = self.client.get(self.url, {"format": "png", "resolution": "96x64"})
        self.assertEqual(Image.open(BytesIO(resp.content)).size, (96, 64))

    def test_png_malformed_resolution_is_400(self):
        resp = self.client.get(self.url, {"format": "png", "resolution": "garbage"})
        self.assertEqual(resp.status_code, 400)


class MobileEmailLoginTests(TestCase):
    """MobileAuthService email fallback must work even when multiple users share an email, and it
    must honour allauth's mandatory email-verification policy (no weaker side door than the web)."""

    def setUp(self):
        from allauth.account.models import EmailAddress

        self.alice = User.objects.create_user("alice", "dup@example.com", "pw-alice")
        self.bob = User.objects.create_user("bob", "dup@example.com", "pw-bob")
        # ACCOUNT_EMAIL_VERIFICATION is mandatory, so these must have a verified email to log in.
        for user in (self.alice, self.bob):
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)

    def test_login_by_username_still_works(self):
        from auctions.mobile.services.auth import MobileAuthService

        self.assertEqual(MobileAuthService.authenticate("alice", "pw-alice"), self.alice)

    def test_duplicate_email_resolves_to_password_owner(self):
        from auctions.mobile.services.auth import MobileAuthService

        self.assertEqual(MobileAuthService.authenticate("dup@example.com", "pw-bob"), self.bob)
        self.assertEqual(MobileAuthService.authenticate("dup@example.com", "pw-alice"), self.alice)

    def test_wrong_password_returns_none(self):
        from auctions.mobile.services.auth import MobileAuthService

        self.assertIsNone(MobileAuthService.authenticate("dup@example.com", "nope"))

    def test_unverified_email_blocked_when_verification_mandatory(self):
        """A correct password is not enough when the email is unverified — matches web login."""
        from auctions.mobile.services.auth import MobileAuthService

        # No verified EmailAddress for carol.
        User.objects.create_user("carol", "carol@example.com", "pw-carol")
        self.assertIsNone(MobileAuthService.authenticate("carol", "pw-carol"))
        self.assertIsNone(MobileAuthService.authenticate("carol@example.com", "pw-carol"))

    def test_inactive_user_blocked(self):
        from allauth.account.models import EmailAddress

        from auctions.mobile.services.auth import MobileAuthService

        dave = User.objects.create_user("dave", "dave@example.com", "pw-dave", is_active=False)
        EmailAddress.objects.create(user=dave, email=dave.email, verified=True, primary=True)
        self.assertIsNone(MobileAuthService.authenticate("dave", "pw-dave"))


@isolated_cache("mobile-web-session")
class MobileWebSessionTests(TestCase):
    """The WebView pre-auth handoff: a Bearer-authenticated POST mints a one-time token, and the
    WebView-loaded consume GET turns it into a real, server-set Django session cookie. The cookie
    must never be established by the mint call and must carry HttpOnly/Secure flags from the consume
    redirect; the token must be single-use and fail closed (redirect to login, no session)."""

    SESSION_COOKIE = "sessionid"

    def setUp(self):
        from django.conf import settings
        from rest_framework_simplejwt.tokens import RefreshToken

        # Random-token TTL keys can't collide between tests, but clear to keep the cache deterministic.
        cache.clear()
        self.user = User.objects.create_user("websession", "ws@example.com", "pw")
        self.access = str(RefreshToken.for_user(self.user).access_token)
        self.mint_url = reverse("mobile-auth-web-session")
        self.consume_url = reverse("mobile-auth-web-session-consume")
        self.login_url = reverse("account_login")
        self.home_url = settings.LOGIN_REDIRECT_URL

    def _bearer(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.access}"}

    def _mint_token(self):
        from auctions.mobile.services.web_session import WebSessionService

        return WebSessionService.create_handoff_token(self.user)

    def _logged_in_user_id(self):
        from django.contrib.auth import SESSION_KEY

        return self.client.session.get(SESSION_KEY)

    def test_mint_requires_jwt(self):
        self.assertIn(self.client.post(self.mint_url).status_code, (401, 403))

    def test_mint_returns_consume_url_without_establishing_a_session(self):
        resp = self.client.post(self.mint_url, **self._bearer())
        self.assertEqual(resp.status_code, 200)
        handoff_url = resp.json()["handoff_url"]
        self.assertIn(self.consume_url, handoff_url)
        self.assertIn("t=", handoff_url)
        # The mint call must NOT log anyone in: no session cookie, the token is the only credential.
        self.assertNotIn(self.SESSION_COOKIE, resp.cookies)
        self.assertIsNone(self._logged_in_user_id())

    def test_consume_logs_in_and_sets_session_cookie(self):
        resp = self.client.get(self.consume_url, {"t": self._mint_token()})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.home_url)
        self.assertIn(self.SESSION_COOKIE, resp.cookies)
        # The follow-up request carries the cookie, so the WebView is now authenticated as the user.
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_session_cookie_carries_httponly_and_secure(self):
        resp = self.client.get(self.consume_url, {"t": self._mint_token()})
        morsel = resp.cookies[self.SESSION_COOKIE]
        self.assertTrue(morsel["httponly"])
        self.assertTrue(morsel["secure"])

    def test_token_is_single_use(self):
        token = self._mint_token()
        first = self.client.get(self.consume_url, {"t": token})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, self.home_url)

        # Replaying the same token must not mint a second session.
        self.client.logout()
        second = self.client.get(self.consume_url, {"t": token})
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, self.login_url)
        self.assertNotIn(self.SESSION_COOKIE, second.cookies)
        self.assertIsNone(self._logged_in_user_id())

    def test_missing_token_redirects_to_login_without_session(self):
        resp = self.client.get(self.consume_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.login_url)
        self.assertIsNone(self._logged_in_user_id())

    def test_invalid_token_redirects_to_login_without_session(self):
        resp = self.client.get(self.consume_url, {"t": "not-a-real-token"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.login_url)
        self.assertIsNone(self._logged_in_user_id())

    def test_inactive_user_cannot_consume(self):
        token = self._mint_token()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        resp = self.client.get(self.consume_url, {"t": token})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.login_url)
        self.assertIsNone(self._logged_in_user_id())

    def test_consume_honours_safe_next(self):
        resp = self.client.get(self.consume_url, {"t": self._mint_token(), "next": "/lots/"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/lots/")

    def test_consume_rejects_offsite_next(self):
        resp = self.client.get(self.consume_url, {"t": self._mint_token(), "next": "https://evil.example.com/"})
        self.assertEqual(resp.status_code, 302)
        # Open-redirect attempt falls back to the safe default rather than the attacker's host.
        self.assertEqual(resp.url, self.home_url)
