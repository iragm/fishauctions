"""Gaps in the bulk-import and export views: the per-row lot save (closed submission, bad input), the
classic and auto bulk-add pages' gates, the lot CSV importer's duplicate and scoping rules, the Google
Drive sync (with ``requests.get`` mocked), BulkAddUsers' edge cases, adding auction participants to a
club, and the personal and marketing CSV exports. The happy paths already covered in
test_bulk_add_lots.py and test_csv_import.py are not repeated here.
"""

import csv
import datetime
import io
import json
from unittest.mock import MagicMock, patch

import requests
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubHistory,
    ClubMember,
    Invoice,
    Lot,
    PickupLocation,
)
from auctions.tests import StandardTestCase


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8"))))


def _someone_elses_auction(owner, participant_name, participant_email):
    auction = Auction.objects.create(
        created_by=owner,
        title="Someone else's auction",
        is_online=True,
        date_start=timezone.now() - datetime.timedelta(days=1),
        date_end=timezone.now() + datetime.timedelta(days=1),
    )
    location = PickupLocation.objects.create(name="elsewhere", auction=auction, pickup_time=timezone.now())
    AuctionTOS.objects.create(
        auction=auction, pickup_location=location, name=participant_name, email=participant_email, bidder_number="777"
    )
    return auction


class _OpenInPersonAuction(StandardTestCase):
    """The in-person auction with lot submission open and bulk adding allowed."""

    def setUp(self):
        super().setUp()
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=7)
        self.in_person_auction.save()

    def close_submission(self):
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.save()


class SaveLotAjaxGapTests(_OpenInPersonAuction):
    def _post(self, payload, raw=None):
        return self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=raw if raw is not None else json.dumps(payload),
            content_type="application/json",
        )

    def test_new_lot_is_owned_by_the_seller_and_gets_an_invoice_and_history(self):
        self.client.login(username="no_lots", password="testpassword")
        data = self._post({"lot_name": "Ajax guppies", "reserve_price": 5}).json()
        self.assertTrue(data["success"], data)
        lot = Lot.objects.get(pk=data["lot_pk"])
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)
        self.assertEqual(lot.user, self.user_with_no_lots)
        self.assertEqual(lot.added_by, self.user_with_no_lots)
        self.assertTrue(
            Invoice.objects.filter(auctiontos_user=self.in_person_buyer, auction=self.in_person_auction).exists()
        )
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.in_person_auction, action__contains="Ajax guppies").exists()
        )

    def test_non_admin_cannot_add_once_lot_submission_has_ended(self):
        self.close_submission()
        self.client.login(username="no_lots", password="testpassword")
        data = self._post({"lot_name": "Too late", "reserve_price": 5}).json()
        self.assertFalse(data["success"])
        self.assertIn("ended", data["error"])
        self.assertFalse(Lot.objects.filter(lot_name="Too late").exists())

    def test_admin_can_still_add_for_a_seller_after_submission_ends(self):
        self.close_submission()
        self.client.login(username="admin_user", password="testpassword")
        data = self._post({"lot_name": "Admin late add", "reserve_price": 5, "bidder_number": "555"}).json()
        self.assertTrue(data["success"], data)
        self.assertEqual(Lot.objects.get(pk=data["lot_pk"]).auctiontos_seller, self.in_person_buyer)

    def test_someone_who_has_not_joined_is_refused(self):
        self.client.login(username="no_tos", password="testpassword")
        data = self._post({"lot_name": "No join", "reserve_price": 5}).json()
        self.assertFalse(data["success"])
        self.assertIn("join", data["error"])
        self.assertFalse(Lot.objects.filter(lot_name="No join").exists())

    def test_own_lot_in_another_auction_cannot_be_edited_through_this_one(self):
        other = Lot.objects.create(
            lot_name="Online original", auction=self.online_auction, auctiontos_seller=self.tosC, reserve_price=5
        )
        self.client.login(username="no_lots", password="testpassword")
        data = self._post({"lot_id": other.lot_number, "lot_name": "Hijacked", "reserve_price": 5}).json()
        self.assertFalse(data["success"])
        other.refresh_from_db()
        self.assertEqual(other.lot_name, "Online original")

    def test_malformed_json_is_an_error_not_a_500(self):
        self.client.login(username="no_lots", password="testpassword")
        response = self._post(None, raw="{not json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertIn("Invalid JSON", response.json()["error"])

    def test_bad_field_values_are_reported_per_field_and_nothing_is_saved(self):
        self.in_person_auction.use_quantity_field = True
        self.in_person_auction.save()
        self.client.login(username="no_lots", password="testpassword")
        before = Lot.objects.count()
        data = self._post({"lot_name": "x" * 41, "reserve_price": "abc", "quantity": 0, "buy_now_price": "-1"}).json()
        self.assertFalse(data["success"])
        self.assertEqual(set(data["errors"]), {"lot_name", "reserve_price", "quantity", "buy_now_price"})
        self.assertEqual(Lot.objects.count(), before)


class BulkAddLotsFormsetTests(_OpenInPersonAuction):
    """The classic (non-auto) bulk add formset."""

    def _formset_data(self, lot_name):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-lot_name": lot_name,
            "form-0-quantity": "1",
            "form-0-reserve_price": "5",
        }

    def test_seller_adds_lots_for_themselves(self):
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.post(url, self._formset_data("Formset lot"))
        self.assertRedirects(response, reverse("selling"), fetch_redirect_response=False)
        lot = Lot.objects.get(lot_name="Formset lot")
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)
        self.assertEqual(lot.auction, self.in_person_auction)
        self.assertEqual(lot.user, self.user_with_no_lots)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.in_person_auction, action__startswith="Bulk added 1").exists()
        )

    def test_non_admin_cannot_add_lots_under_another_bidder_number(self):
        # bulk_add_lots.py dispatch uses the URL's bidder_number without an admin check (BulkAddLotsAuto has one).
        self.client.login(username="no_lots", password="testpassword")
        url = reverse(
            "bulk_add_lots",
            kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.admin_in_person_tos.bidder_number},
        )
        self.client.post(url, self._formset_data("Planted lot"))
        self.assertFalse(
            Lot.objects.filter(lot_name="Planted lot", auctiontos_seller=self.admin_in_person_tos).exists()
        )

    def test_closed_submission_redirects_to_the_auction(self):
        self.close_submission()
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.post(url, self._formset_data("Closed lot"))
        self.assertRedirects(
            response,
            reverse("auction_main", kwargs={"slug": self.in_person_auction.slug}),
            fetch_redirect_response=False,
        )
        self.assertFalse(Lot.objects.filter(lot_name="Closed lot").exists())

    def test_bulk_adding_disabled_sends_the_seller_to_the_single_lot_form(self):
        self.in_person_auction.allow_bulk_adding_lots = False
        self.in_person_auction.save()
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug}))
        self.assertRedirects(response, self.in_person_auction.add_lot_link, fetch_redirect_response=False)


class BulkAddLotsAutoGapTests(_OpenInPersonAuction):
    def test_someone_who_has_not_joined_is_sent_to_join_then_back(self):
        self.client.login(username="no_tos", password="testpassword")
        url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(reverse("auction_main", kwargs={"slug": self.in_person_auction.slug}))
        )
        self.assertIn(f"next={url}", response["Location"])

    def test_admin_with_unknown_bidder_number_goes_to_the_user_list(self):
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("bulk_add_lots_auto", kwargs={"slug": self.in_person_auction.slug, "bidder_number": "99999"})
        response = self.client.get(url)
        self.assertRedirects(
            response,
            reverse("auction_tos_list", kwargs={"slug": self.in_person_auction.slug}),
            fetch_redirect_response=False,
        )

    def test_closed_submission_blocks_sellers_but_not_admins(self):
        self.close_submission()
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.login(username="admin_user", password="testpassword")
        admin_url = reverse("bulk_add_lots_auto", kwargs={"slug": self.in_person_auction.slug, "bidder_number": "555"})
        self.assertEqual(self.client.get(admin_url).status_code, 200)

    def test_a_seller_at_the_lot_limit_still_gets_one_row(self):
        self.in_person_auction.max_lots_per_user = 1
        self.in_person_auction.save()
        Lot.objects.create(
            lot_name="Only lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_buyer, reserve_price=5
        )
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )
        self.assertEqual(response.context["initial_rows"], 1)
        self.assertEqual(response.context["current_lot_count"], 1)


class ImportLotsFromCSVGapTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username=self.admin_user.username, password="testpassword")
        self.url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

    def _file(self, content, name="lots.csv"):
        return SimpleUploadedFile(name, content if isinstance(content, bytes) else content.encode(), "text/csv")

    def _name_only_match(self):
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(name="Wanda Maximoff", email="wanda@example.com")
        return self._file("Name,Email,Lot Name\nWanda Maximoff,scarlet@example.com,Name match lot\n")

    def test_name_only_match_merged_attaches_the_lot_to_the_existing_seller(self):
        self.run_csv_import(self.url, self._name_only_match(), decisions={0: "merge"})
        lot = Lot.objects.get(lot_name="Name match lot")
        self.assertEqual(lot.auctiontos_seller, self.tosC)
        self.assertFalse(AuctionTOS.objects.filter(email="scarlet@example.com").exists())

    def test_name_only_match_create_makes_a_new_seller(self):
        self.run_csv_import(self.url, self._name_only_match(), decisions={0: "create"})
        lot = Lot.objects.get(lot_name="Name match lot")
        self.assertEqual(lot.auctiontos_seller.email, "scarlet@example.com")
        self.assertNotEqual(lot.auctiontos_seller, self.tosC)

    def test_row_with_an_invalid_email_is_skipped(self):
        self.run_csv_import(self.url, self._file("Name,Email,Lot Name\nTypo Tim,tim@nowhere,Typo lot\n"))
        self.assertFalse(Lot.objects.filter(lot_name="Typo lot").exists())
        self.assertFalse(AuctionTOS.objects.filter(name="Typo Tim").exists())

    def test_lot_number_from_another_auction_is_not_updated(self):
        self.run_csv_import(self.url, self._file("Lot Number,Lot Name\n101-1,Renamed across auctions\n"))
        self.in_person_lot.refresh_from_db()
        self.assertEqual(self.in_person_lot.lot_name, "another test lot")

    def test_update_ignores_winner_and_price_columns(self):
        content = f"Lot Number,Lot Name,Winner,Winning Price\n{self.lot.lot_number_int},Renamed,{self.tosC.bidder_number},999\n"
        self.run_csv_import(self.url, self._file(content))
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.lot_name, "Renamed")
        self.assertEqual(self.lot.auctiontos_winner, self.tosB)
        self.assertEqual(self.lot.winning_price, 10)

    def test_unknown_preview_token_redirects_to_the_lot_list(self):
        response = self.client.get(self.url + "?preview=doesnotexist")
        self.assertRedirects(
            response,
            reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug}),
            fetch_redirect_response=False,
        )

    def test_non_utf8_file_is_an_error_message_not_a_500(self):
        # handle_csv_upload (bulk_add.py) returns None on UnicodeDecodeError and post() returns that as the response.
        response = self.client.post(self.url, {"csv_file": self._file(b"Name,Lot Name\n\xff\xfe Caf\xe9,Lot\n")})
        self.assertEqual(response.status_code, 302)


class ImportFromGoogleDriveTests(StandardTestCase):
    SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=77"

    def setUp(self):
        super().setUp()
        self.url = reverse("sync_google_drive", kwargs={"slug": self.online_auction.slug})
        self.bulk_add_url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})

    def _sheet_response(self, text):
        response = MagicMock()
        response.text = text
        response.raise_for_status.return_value = None
        return response

    def test_non_admin_is_refused(self):
        self.client.login(username="no_lots", password="testpassword")
        with patch("auctions.views.bulk_add.requests.get") as get:
            response = self.client.post(self.url, {"google_drive_link": self.SHEET})
        self.assertEqual(response.status_code, 403)
        get.assert_not_called()
        self.online_auction.refresh_from_db()
        self.assertFalse(self.online_auction.google_drive_link)

    def test_sync_saves_the_link_and_previews_before_writing(self):
        self.client.login(username="admin_user", password="testpassword")
        with patch(
            "auctions.views.bulk_add.requests.get",
            return_value=self._sheet_response("Name,Email\nSheet Person,sheet@example.com\n"),
        ) as get:
            response = self.client.post(self.url, {"google_drive_link": self.SHEET})
        get.assert_called_once()
        self.assertEqual(
            get.call_args.args[0], "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=77"
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.bulk_add_url + "?preview="))
        self.online_auction.refresh_from_db()
        self.assertEqual(self.online_auction.google_drive_link, self.SHEET)
        self.assertIsNotNone(self.online_auction.last_sync_time)
        self.assertFalse(AuctionTOS.objects.filter(email="sheet@example.com").exists())

        token = response["Location"].split("preview=")[1]
        self.client.post(self.bulk_add_url, {"_confirm": token})
        self.assertTrue(
            AuctionTOS.objects.filter(
                auction=self.online_auction, email="sheet@example.com", name="Sheet Person"
            ).exists()
        )

    def test_a_link_that_is_not_a_sheet_is_refused_without_fetching(self):
        self.client.login(username="admin_user", password="testpassword")
        with patch("auctions.views.bulk_add.requests.get") as get:
            response = self.client.post(self.url, {"google_drive_link": "https://example.com/not-a-sheet"})
        get.assert_not_called()
        self.assertRedirects(response, self.bulk_add_url, fetch_redirect_response=False)
        self.assertTrue(any("Invalid Google Drive link" in m for m in _messages(response)))

    def test_sync_with_no_link_configured(self):
        self.client.login(username="admin_user", password="testpassword")
        with patch("auctions.views.bulk_add.requests.get") as get:
            response = self.client.post(self.url)
        get.assert_not_called()
        self.assertRedirects(response, self.bulk_add_url, fetch_redirect_response=False)
        self.assertTrue(any("No Google Drive link" in m for m in _messages(response)))

    def test_unshared_sheet_says_to_share_it(self):
        self.client.login(username="admin_user", password="testpassword")
        failing = self._sheet_response("")
        failing.raise_for_status.side_effect = requests.HTTPError("401 Client Error: Unauthorized")
        with patch("auctions.views.bulk_add.requests.get", return_value=failing):
            response = self.client.post(self.url, {"google_drive_link": self.SHEET})
        self.assertRedirects(response, self.bulk_add_url, fetch_redirect_response=False)
        self.assertTrue(any("anyone with the link" in m for m in _messages(response)))
        self.online_auction.refresh_from_db()
        self.assertIsNone(self.online_auction.last_sync_time)


class BulkAddUsersEdgeTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="admin_user", password="testpassword")
        self.url = reverse("bulk_add_users", kwargs={"slug": self.in_person_auction.slug})

    def test_csv_without_a_name_or_email_column_is_rejected(self):
        before = AuctionTOS.objects.count()
        response = self.client.post(
            self.url, {"csv_file": SimpleUploadedFile("u.csv", b"colour,size\nred,big\n", "text/csv")}
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertTrue(any("Unable to read information" in m for m in _messages(response)))
        self.assertEqual(AuctionTOS.objects.count(), before)

    def test_import_from_another_auction_lists_only_people_not_already_here(self):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(name="Import Me", email="importme@example.com")
        response = self.client.get(self.url + f"?import={self.online_auction.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "importme@example.com")
        self.assertTrue(any("already in this auction" in m for m in _messages(response)))

    def test_import_from_an_auction_you_do_not_run_is_refused(self):
        stranger_auction = _someone_elses_auction(self.userB, "Private Person", "private@example.com")
        response = self.client.get(self.url + f"?import={stranger_auction.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "private@example.com")
        self.assertTrue(any("permission" in m for m in _messages(response)))

    def test_import_from_a_missing_auction_is_not_a_500(self):
        # bulk_add.py BulkAddUsers.get: other_auction is None for an unknown slug, then .permission_check() raises.
        response = self.client.get(self.url + "?import=no-such-auction")
        self.assertIn(response.status_code, (200, 302, 404))

    def test_manual_formset_adds_people_as_manually_added(self):
        data = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-bidder_number": "4242",
            "form-0-name": "Walk In",
            "form-0-email": "walkin@example.com",
            "form-0-pickup_location": str(self.in_person_location.pk),
        }
        response = self.client.post(self.url, data)
        self.assertRedirects(
            response,
            reverse("auction_tos_list", kwargs={"slug": self.in_person_auction.slug}),
            fetch_redirect_response=False,
        )
        tos = AuctionTOS.objects.get(auction=self.in_person_auction, email="walkin@example.com")
        self.assertTrue(tos.manually_added)
        self.assertEqual(tos.bidder_number, "4242")

    def test_club_managed_auction_sends_admins_to_the_club(self):
        club = Club.objects.create(name="Managing Club")
        self.in_person_auction.club = club
        self.in_person_auction.manage_users_through_club = "all"
        self.in_person_auction.save()
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("club_admin", kwargs={"slug": club.slug}), fetch_redirect_response=False)


class AddAuctionUsersToClubTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Receiving Club")
        self.online_auction.club = self.club
        self.online_auction.save()
        self.url = reverse("auction_add_users_to_club", kwargs={"slug": self.online_auction.slug})
        self.tos_list_url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})

    def _grant_admin_club_permission(self):
        ClubMember.objects.create(
            club=self.club, user=self.admin_user, name="Admin", email="clubadmin@example.com", permission_add_edit=True
        )

    def test_non_auction_admin_is_refused(self):
        self.client.login(username="no_lots", password="testpassword")
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_auction_without_a_club(self):
        self.online_auction.club = None
        self.online_auction.save()
        self.client.login(username="admin_user", password="testpassword")
        before = ClubMember.objects.count()
        response = self.client.post(self.url)
        self.assertRedirects(response, self.tos_list_url, fetch_redirect_response=False)
        self.assertEqual(ClubMember.objects.count(), before)

    def test_auction_admin_without_club_permission_adds_nobody(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(self.url)
        self.assertRedirects(response, self.tos_list_url, fetch_redirect_response=False)
        self.assertFalse(ClubMember.objects.filter(club=self.club).exists())

    def test_adds_new_people_once_and_skips_existing_members(self):
        self._grant_admin_club_permission()
        ClubMember.objects.create(club=self.club, name="Already", email="seller@example.com")
        AuctionTOS.objects.filter(pk=self.admin_online_tos.pk).update(email="admin-tos@example.com")
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(email="Seller@Example.com")
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(email="")
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(email="newbie@example.com", name="Newbie")
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Newbie again",
            email="newbie@example.com",
            bidder_number="900",
        )
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(self.url)
        self.assertRedirects(response, self.tos_list_url, fetch_redirect_response=False)
        added = ClubMember.objects.filter(club=self.club, source=self.online_auction.title)
        self.assertEqual(list(added.values_list("email", flat=True)), ["newbie@example.com"])
        self.assertEqual(added.get().added_by, self.admin_user)
        self.assertEqual(ClubMember.objects.filter(club=self.club, email__iexact="seller@example.com").count(), 1)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__startswith="Added 1 participant").exists())


class AddSingleAuctionTOSToClubTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Single Club")
        self.online_auction.club = self.club
        self.online_auction.save()
        ClubMember.objects.create(club=self.club, user=self.admin_user, name="Admin", permission_add_edit=True)
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(name="Single Person", email="single@example.com")
        self.url = reverse("add_single_auctiontos_to_club", kwargs={"pk": self.tosC.pk})

    def test_auction_without_a_club_is_a_400(self):
        self.online_auction.club = None
        self.online_auction.save()
        self.client.login(username="admin_user", password="testpassword")
        self.assertEqual(self.client.post(self.url).status_code, 400)

    def test_someone_without_club_permission_is_refused(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertFalse(ClubMember.objects.filter(email="single@example.com").exists())

    def test_adds_the_person_once(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self.url)
        response = self.client.post(self.url, HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Refresh"], "true")
        member = ClubMember.objects.get(club=self.club, email="single@example.com")
        self.assertEqual(member.name, "Single Person")
        self.assertEqual(member.user, self.user_with_no_lots)

    def test_a_removed_member_can_be_added_back(self):
        ClubMember.objects.create(club=self.club, name="Gone", email="single@example.com", is_deleted=True)
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(self.url)
        self.assertTrue(
            ClubMember.objects.filter(club=self.club, email="single@example.com", is_deleted=False).exists()
        )


class PersonalCSVExportTests(StandardTestCase):
    def test_won_lots_csv_lists_only_my_live_wins(self):
        won = Lot.objects.create(
            lot_name="My win",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=self.tosC,
            winning_price=12,
            active=False,
        )
        Lot.objects.create(
            lot_name="Deleted win",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=self.tosC,
            winning_price=12,
            active=False,
            is_deleted=True,
        )
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(email=self.user_with_no_lots.email)
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("my_won_lot_csv"))
        self.assertEqual(response["Content-Type"], "text/csv")
        names = [row[1] for row in _csv_rows(response)[1:]]
        self.assertEqual(names, [won.lot_name])

    def test_won_lots_csv_for_an_account_without_email_is_not_everyone_s_wins(self):
        self.user_with_no_lots.email = ""
        self.user_with_no_lots.save()
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(email="")
        self.client.login(username="no_lots", password="testpassword")
        rows = _csv_rows(self.client.get(reverse("my_won_lot_csv")))
        self.assertEqual(len(rows), 1)

    def test_my_lot_report_status_column(self):
        Lot.objects.create(
            lot_name="Removed lot", auction=self.online_auction, auctiontos_seller=self.online_tos, banned=True
        )
        Lot.objects.filter(auctiontos_seller=self.online_tos).update(user=self.user)
        self.client.login(username="my_lot", password="testpassword")
        rows = _csv_rows(self.client.get(reverse("my_lot_report")))
        status = {row[1]: row[4] for row in rows[1:]}
        self.assertEqual(status["A test lot"], "Sold")
        self.assertEqual(status["Unsold lot"], "Unsold")
        self.assertEqual(status["Removed lot"], "Removed")


class MarketingListTests(StandardTestCase):
    def test_lists_each_address_once_from_auctions_i_run_and_skips_bad_ones(self):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(
            name="Bouncer", email="bounce@example.com", email_address_status="BAD"
        )
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(name="Good One", email="good@example.com")
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(name="Good Again", email="good@example.com")
        _someone_elses_auction(self.userB, "Hidden", "hidden@example.com")
        self.client.login(username="my_lot", password="testpassword")
        rows = _csv_rows(self.client.get(reverse("all_my_users")))
        emails = [row[1] for row in rows[1:]]
        self.assertEqual(emails.count("good@example.com"), 1)
        self.assertNotIn("bounce@example.com", emails)
        self.assertNotIn("hidden@example.com", emails)
        self.assertTrue(
            AuctionHistory.objects.filter(
                auction=self.in_person_auction, action__startswith="Exported marketing"
            ).exists()
        )

    def test_someone_who_runs_no_auctions_gets_only_the_header(self):
        self.client.login(username="no_tos", password="testpassword")
        rows = _csv_rows(self.client.get(reverse("all_my_users")))
        self.assertEqual(rows, [["Name", "Email", "Phone"]])


class PayPalCSVPermissionTests(StandardTestCase):
    def test_non_admin_is_refused(self):
        self.client.login(username="no_lots", password="testpassword")
        response = self.client.get(reverse("paypal_csv", kwargs={"slug": self.online_auction.slug, "chunk": 1}))
        self.assertEqual(response.status_code, 403)

    def test_a_chunk_past_the_end_is_just_the_header(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("paypal_csv", kwargs={"slug": self.online_auction.slug, "chunk": 50}))
        self.assertEqual(len(_csv_rows(response)), 1)
