"""Regression tests for auction-admin and lot-management views with thin coverage: the add-to-calendar
link, the no-show actions dialog, bulk label PDFs, the custom-dropdown options API, the users table, new
pickup locations, deleting and merging AuctionTOS rows, the lot admin modal, the lot page, lot creation,
and the small AJAX endpoints for images, deactivating lots and user bans. Each view gets its permission
gate, its main effect on the database, and the edge cases that looked fragile when reading the code.
"""

import datetime
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionHistory,
    AuctionTOS,
    Bid,
    Invoice,
    Lot,
    LotImage,
    PickupLocation,
    UserBan,
)
from auctions.tests import StandardTestCase, WritableMediaRoot, give_contact_info


def _future(days=3):
    return timezone.now() + datetime.timedelta(days=days)


def _open_auction(creator, title="Open auction"):
    """A running online auction taking lots, with one pickup location."""
    auction = Auction.objects.create(
        created_by=creator,
        title=title,
        is_online=True,
        date_start=timezone.now() - datetime.timedelta(days=1),
        date_end=_future(5),
        lot_submission_end_date=_future(4),
        winning_bid_percent_to_club=25,
    )
    location = PickupLocation.objects.create(name="Open location", auction=auction, pickup_time=_future(6))
    return auction, location


class AddToCalendarViewTests(StandardTestCase):
    def _url(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{reverse('add_to_calendar')}?{query}"

    def test_ics_download_for_a_participant_records_the_choice(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="ics", location=self.location.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar")
        self.assertIn("BEGIN:VEVENT", response.content.decode())
        self.online_tos.refresh_from_db()
        self.assertEqual(self.online_tos.add_to_calendar, "ics")

    def test_google_redirects_to_google_calendar(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="google", location=self.location.pk))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://calendar.google.com/"))

    def test_native_returns_the_event_as_json(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="native", location=self.location.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], self.online_auction.title)

    def test_someone_who_has_not_joined_is_sent_to_the_auction(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(self._url(type="ics", location=self.location.pk))
        self.assertRedirects(response, self.online_auction.get_absolute_url(), fetch_redirect_response=False)
        self.assertFalse(AuctionTOS.objects.filter(user=self.user_who_does_not_join).exists())

    def test_an_unknown_calendar_type_changes_nothing(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="yahoo", location=self.location.pk))
        self.assertEqual(response.status_code, 302)
        self.online_tos.refresh_from_db()
        self.assertNotEqual(self.online_tos.add_to_calendar, "yahoo")

    def test_second_pickup_when_there_is_none_redirects(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="ics", location=self.location.pk, second="1"))
        self.assertRedirects(response, self.online_auction.get_absolute_url(), fetch_redirect_response=False)

    def test_a_missing_location_is_a_404(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="ics"))
        self.assertEqual(response.status_code, 404)

    # auction_extras.py: ?location= goes straight into get_object_or_404(pk=...), which raises ValueError.
    def test_a_non_numeric_location_is_a_404(self):
        self.client.force_login(self.user)
        response = self.client.get(self._url(type="ics", location="abc"))
        self.assertEqual(response.status_code, 404)


class AuctionNoShowActionTests(StandardTestCase):
    def _url(self, tos, auction=None):
        auction = auction or self.online_auction
        return reverse("auction_no_show_dialog", kwargs={"slug": auction.slug, "tos": tos.bidder_number})

    def test_a_non_admin_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self._url(self.tosB), {"refund_bought_lots": "on"})
        self.assertEqual(response.status_code, 403)
        self.lot.refresh_from_db()
        self.assertNotEqual(self.lot.partial_refund_percent, 100)

    def test_an_unknown_bidder_number_is_a_404(self):
        self.client.force_login(self.admin_user)
        url = reverse("auction_no_show_dialog", kwargs={"slug": self.online_auction.slug, "tos": "nobody"})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_refund_bought_lots_refunds_every_lot_the_no_show_won(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self._url(self.tosB), {"refund_bought_lots": "on"})
        self.assertEqual(response.status_code, 200)
        for lot in (self.lot, self.lotB, self.lotC):
            lot.refresh_from_db()
            self.assertEqual(lot.partial_refund_percent, 100)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.online_auction, action__contains="refunded bought lots").exists()
        )

    def test_refund_sold_lots_refunds_sold_and_removes_unsold(self):
        self.client.force_login(self.admin_user)
        self.client.post(self._url(self.online_tos), {"refund_sold_lots": "on"})
        self.lot.refresh_from_db()
        self.unsoldLot.refresh_from_db()
        self.assertEqual(self.lot.partial_refund_percent, 100)
        self.assertTrue(self.unsoldLot.banned)

    def test_negative_feedback_on_lots_the_no_show_won(self):
        self.client.force_login(self.admin_user)
        self.client.post(self._url(self.tosB), {"leave_negative_feedback": "on"})
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.winner_feedback_rating, -1)

    # auction_extras.py: `lot.feedback_rating - 1` is an expression, not an assignment, so sold lots keep 0.
    def test_negative_feedback_on_lots_the_no_show_sold(self):
        self.client.force_login(self.admin_user)
        self.client.post(self._url(self.online_tos), {"leave_negative_feedback": "on"})
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.feedback_text, "Did not provide lot")
        self.assertEqual(self.lot.feedback_rating, -1)

    def test_ban_bans_the_account_behind_the_email(self):
        no_show = User.objects.create_user(username="no_show", password="testpassword", email="noshow@example.com")
        tos = AuctionTOS.objects.create(
            user=no_show,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="880",
            email="noshow@example.com",
        )
        self.client.force_login(self.admin_user)
        self.client.post(self._url(tos), {"ban_this_user": "on"})
        self.assertTrue(UserBan.objects.filter(user=self.admin_user, banned_user=no_show).exists())

    # auction_extras.py: the ban looks the account up by tos.email only, ignoring tos.user; a linked row with no email bans nobody.
    def test_ban_bans_a_linked_account_with_no_email(self):
        self.client.force_login(self.admin_user)
        self.client.post(self._url(self.tosB), {"ban_this_user": "on"})
        self.assertTrue(UserBan.objects.filter(user=self.admin_user, banned_user=self.userB).exists())


class AuctionBulkPrintingPDFTests(StandardTestCase):
    def _url(self, auction=None, **params):
        auction = auction or self.in_person_auction
        url = reverse("auction_printing_pdf", kwargs={"slug": auction.slug})
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return url

    def test_a_non_admin_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_selected_users_labels_are_printed(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(
            self._url(selected_tos=f"[{self.admin_in_person_tos.pk}]", print_only_unprinted="False")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;filename=", response["Content-Disposition"])

    def test_malformed_selection_redirects_back_to_the_form(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(selected_tos="[1, 'x'", print_only_unprinted="False"))
        self.assertRedirects(
            response,
            reverse("auction_printing", kwargs={"slug": self.in_person_auction.slug}),
            fetch_redirect_response=False,
        )

    def test_a_person_from_another_auction_prints_nothing(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(selected_tos=f"[{self.online_tos.pk}]", print_only_unprinted="False"))
        self.assertEqual(response.status_code, 302)

    # auction_extras.py: dispatch uses .first() instead of a 404, so require_auction_admin raises ImproperlyConfigured.
    def test_an_unknown_auction_is_a_404(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("auction_printing_pdf", kwargs={"slug": "no-such-auction"}))
        self.assertEqual(response.status_code, 404)


class AuctionDropdownOptionsAPITests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("auction_custom_dropdown_options", kwargs={"slug": self.online_auction.slug})
        self.option = AuctionDropdown.objects.create(auction=self.online_auction, user=self.user, value="River")

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_get_lists_the_auctions_options(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(self.url)
        self.assertEqual([o["value"] for o in response.json()["options"]], ["River"])

    def test_admin_updates_an_option(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "update", "option_id": self.option.pk, "value": "Lake"})
        self.assertTrue(response.json()["success"])
        self.option.refresh_from_db()
        self.assertEqual(self.option.value, "Lake")

    def test_a_duplicate_is_refused_regardless_of_case(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "create", "value": "river"})
        self.assertFalse(response.json()["success"])
        self.assertEqual(AuctionDropdown.objects.filter(auction=self.online_auction).count(), 1)

    def test_admin_deletes_an_option(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "delete", "option_id": self.option.pk})
        self.assertTrue(response.json()["success"])
        self.assertFalse(AuctionDropdown.objects.filter(pk=self.option.pk).exists())

    def test_a_non_admin_cannot_delete(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self.url, {"action": "delete", "option_id": self.option.pk})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(AuctionDropdown.objects.filter(pk=self.option.pk).exists())

    def test_another_auctions_option_is_not_found(self):
        """An admin of this auction can't reach an option on another auction through its own slug."""
        other = AuctionDropdown.objects.create(auction=self.in_person_auction, user=self.user, value="Pond")
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "delete", "option_id": other.pk})
        self.assertFalse(response.json()["success"])
        self.assertTrue(AuctionDropdown.objects.filter(pk=other.pk).exists())

    # auction_admin.py: option_id goes straight into filter(pk=...), so a non-numeric id raises ValueError.
    def test_a_non_numeric_option_id_is_an_error_not_a_500(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "delete", "option_id": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])


class AuctionUsersTests(StandardTestCase):
    def test_an_unknown_auction_is_a_404(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": "no-such-auction"}))
        self.assertEqual(response.status_code, 404)

    def test_a_participant_who_is_not_an_admin_gets_a_403(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 403)

    def test_a_search_with_no_match_offers_to_create_that_user(self):
        self.client.force_login(self.admin_user)
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "nobody@nowhere.example"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("email=nobody%40nowhere.example", response.context["no_results"])

    def test_failed_bidder_numbers_are_flagged(self):
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(bidder_number="ERROR")
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any(m.startswith("Automatic bidder number generation failed") for m in messages))


class PickupLocationsCreateTests(StandardTestCase):
    def _url(self, slug=None):
        return reverse("create_auction_pickup_location", kwargs={"slug": slug or self.online_auction.slug})

    def _mail_location(self):
        return {"mail_or_not": "True", "name": "By mail", "auction": self.online_auction.pk, "description": ""}

    def test_an_unknown_auction_is_a_404(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self._url("no-such-auction")).status_code, 404)

    def test_a_non_admin_cannot_post_one(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self._url(), self._mail_location())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PickupLocation.objects.filter(auction=self.online_auction, pickup_by_mail=True).exists())

    def test_admin_adds_a_mail_location(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self._url(), self._mail_location())
        self.assertEqual(response.status_code, 302)
        location = PickupLocation.objects.get(auction=self.online_auction, pickup_by_mail=True)
        self.assertEqual(location.user, self.admin_user)
        self.assertTrue(location.users_must_coordinate_pickup)
        self.assertTrue(AuctionHistory.objects.filter(auction=self.online_auction, action__startswith="Added").exists())

    def test_a_second_mail_location_is_refused(self):
        PickupLocation.objects.create(name="Mail", auction=self.online_auction, pickup_by_mail=True)
        self.client.force_login(self.admin_user)
        response = self.client.post(self._url(), self._mail_location())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PickupLocation.objects.filter(auction=self.online_auction, pickup_by_mail=True).count(), 1)

    def test_an_in_person_location_needs_a_map_pin(self):
        self.client.force_login(self.admin_user)
        data = {**self._mail_location(), "mail_or_not": "False", "name": "Hall"}
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PickupLocation.objects.filter(auction=self.online_auction, name="Hall").exists())


class AuctionTOSDeleteTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.spare = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            name="Spare person",
            bidder_number="777",
            manually_added=True,
        )
        self.client.force_login(self.admin_user)

    def _url(self, tos=None):
        return reverse("auctiontosdelete", kwargs={"pk": (tos or self.spare).pk})

    def _data(self, **overrides):
        data = {"auction": self.online_auction.pk, "exclude_auctiontos": self.spare.pk, "merge_with": ""}
        data.update(overrides)
        return data

    def test_a_non_admin_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self._url(), self._data())
        self.assertEqual(response.status_code, 403)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_deleting_someone_with_no_lots(self):
        response = self.client.post(self._url(), self._data())
        self.assertRedirects(
            response,
            reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}),
            fetch_redirect_response=False,
        )
        self.assertFalse(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_someone_with_lots_needs_a_merge_or_delete_lots(self):
        Lot.objects.create(lot_name="Spare's lot", auction=self.online_auction, auctiontos_seller=self.spare)
        response = self.client.post(self._url(), self._data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_delete_lots_removes_sold_lots_and_reopens_won_ones(self):
        sold = Lot.objects.create(lot_name="Spare's lot", auction=self.online_auction, auctiontos_seller=self.spare)
        won = Lot.objects.create(
            lot_name="Won by spare",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            auctiontos_winner=self.spare,
            winning_price=5,
            active=False,
        )
        Invoice.objects.filter(auctiontos_user=self.spare).delete()
        response = self.client.post(self._url(), self._data(delete_lots="on"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuctionTOS.objects.filter(pk=self.spare.pk).exists())
        sold.refresh_from_db()
        won.refresh_from_db()
        self.assertTrue(sold.is_deleted)
        self.assertIsNone(won.auctiontos_winner)
        self.assertIsNone(won.winning_price)
        self.assertTrue(won.active)

    def test_merge_with_someone_in_the_same_auction_moves_their_lots(self):
        lot = Lot.objects.create(lot_name="Spare's lot", auction=self.online_auction, auctiontos_seller=self.spare)
        response = self.client.post(self._url(), self._data(merge_with=self.tosC.pk))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuctionTOS.objects.filter(pk=self.spare.pk).exists())
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_seller, self.tosC)

    def test_merge_with_someone_in_another_auction_is_refused(self):
        lot = Lot.objects.create(lot_name="Spare's lot", auction=self.online_auction, auctiontos_seller=self.spare)
        response = self.client.post(self._url(), self._data(merge_with=self.in_person_buyer.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())
        self.assertTrue(AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).exists())
        lot.refresh_from_db()
        self.assertEqual(lot.auctiontos_seller, self.spare)

    def test_merge_with_garbage_is_a_form_error(self):
        response = self.client.post(self._url(), self._data(merge_with="'; drop table"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_merge_with_themselves_is_refused(self):
        response = self.client.post(self._url(), self._data(merge_with=self.spare.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_someone_with_an_invoice_cannot_be_plainly_deleted(self):
        Invoice.objects.get_or_create(auctiontos_user=self.spare, auction=self.online_auction)
        response = self.client.post(self._url(), self._data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_an_unknown_pk_is_a_404(self):
        response = self.client.post(reverse("auctiontosdelete", kwargs={"pk": 999999999}), self._data())
        self.assertEqual(response.status_code, 404)

    # auction_pages.py: the pk is a <str:> URL segment passed straight to filter(pk=...), which raises ValueError.
    def test_a_non_numeric_pk_is_a_404(self):
        response = self.client.post(reverse("auctiontosdelete", kwargs={"pk": "abc"}), self._data())
        self.assertEqual(response.status_code, 404)

    # auction_pages.py: _get_merge_target passes the posted target to get_object_or_404, which raises ValueError.
    def test_merge_review_with_a_non_numeric_target_is_a_404(self):
        url = self._url() + "?action=merge"
        response = self.client.post(url, {"action": "merge", "step": "review", "target": "abc"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())

    def test_merge_review_with_a_target_in_another_auction_is_a_404(self):
        url = self._url() + "?action=merge"
        response = self.client.post(url, {"action": "merge", "step": "review", "target": self.in_person_buyer.pk})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AuctionTOS.objects.filter(pk=self.spare.pk).exists())


class LotAdminTests(StandardTestCase):
    def _url(self, lot=None):
        return reverse("auctionlotadmin", kwargs={"pk": (lot or self.in_person_lot).pk})

    def _data(self, **overrides):
        lot = self.in_person_lot
        data = {
            "lot_name": lot.lot_name,
            "auction": lot.auction.pk,
            "species": "",
            "species_category": "",
            "summernote_description": "",
            "quantity": 1,
            "donation": "",
            "i_bred_this_fish": "",
            "buy_now_price": "",
            "reserve_price": 5,
            "banned": "",
            "auctiontos_winner": "",
            "winning_price": "",
            "custom_checkbox": "",
            "custom_field_1": "",
            "custom_dropdown": "",
        }
        data.update(overrides)
        return data

    def test_a_non_admin_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(self._url(), self._data(lot_name="Hijacked"))
        self.assertEqual(response.status_code, 403)
        self.in_person_lot.refresh_from_db()
        self.assertNotEqual(self.in_person_lot.lot_name, "Hijacked")

    def test_an_unknown_lot_is_a_404(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(reverse("auctionlotadmin", kwargs={"pk": 999999999})).status_code, 404)

    def test_a_lot_outside_any_auction_is_a_404(self):
        standalone = Lot.objects.create(lot_name="Standalone", user=self.admin_user, quantity=1)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self._url(standalone)).status_code, 404)

    def test_admin_sets_the_winner(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(
            self._url(), self._data(auctiontos_winner=self.in_person_buyer.pk, winning_price=15)
        )
        self.assertEqual(response.status_code, 200)
        self.in_person_lot.refresh_from_db()
        self.assertEqual(self.in_person_lot.auctiontos_winner, self.in_person_buyer)
        self.assertEqual(self.in_person_lot.winning_price, 15)
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.in_person_auction, action__startswith="Edited lot").exists()
        )

    def test_a_winner_from_another_auction_is_refused(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self._url(), self._data(auctiontos_winner=self.tosB.pk, winning_price=15))
        self.assertEqual(response.status_code, 200)
        self.in_person_lot.refresh_from_db()
        self.assertIsNone(self.in_person_lot.auctiontos_winner)


class ViewLotTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.open_auction, self.open_location = _open_auction(self.user)
        self.seller_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.open_location, bidder_number="700"
        )
        self.bidder_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.open_auction,
            pickup_location=self.open_location,
            bidder_number="701",
        )
        AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.open_auction,
            pickup_location=self.open_location,
            bidder_number="702",
            is_admin=True,
        )
        self.open_lot = Lot.objects.create(
            lot_name="Open lot",
            auction=self.open_auction,
            auctiontos_seller=self.seller_tos,
            user=self.user,
            quantity=1,
            reserve_price=5,
        )
        self.bid = Bid.objects.create(user=self.user_with_no_lots, lot_number=self.open_lot, amount=6)
        self.url = reverse("lot_by_pk", kwargs={"pk": self.open_lot.pk})

    def test_anonymous_is_asked_to_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["bidding_error_is_sign_in"])
        self.assertEqual(response.context["bids"], [])

    def test_someone_who_has_not_joined_is_asked_to_join(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(self.url)
        self.assertIn("join the auction", str(response.context["user_specific_bidding_error"]))

    def test_the_seller_cannot_bid_on_their_own_lot(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.context["user_specific_bidding_error"], "You can't bid on your own lot")
        self.assertTrue(response.context["is_lot_creator"])

    def test_a_participant_sees_their_own_bid_but_not_the_bid_list(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(self.url)
        self.assertEqual(response.context["viewer_bid"], 6)
        self.assertEqual(response.context["bids"], [])
        self.assertFalse(response.context["is_lot_creator"])

    def test_an_admin_sees_the_bids(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertTrue(response.context["is_auction_admin"])
        self.assertIn(self.bid.pk, [b.pk for b in response.context["bids"]])

    def test_a_deleted_lot_is_a_404(self):
        Lot.objects.filter(pk=self.open_lot.pk).update(is_deleted=True)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_custom_lot_number_in_its_auction(self):
        url = reverse("lot_in_auction", kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": "101-1"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["object"].pk, self.in_person_lot.pk)

    def test_the_same_lot_number_in_another_auction_is_a_404(self):
        url = reverse("lot_in_auction", kwargs={"slug": self.online_auction.slug, "custom_lot_number": "101-1"})
        self.assertEqual(self.client.get(url).status_code, 404)

    # lot_pages.py: a non-numeric latitude cookie reaches distance_to(), which raises TypeError.
    def test_a_garbage_location_cookie_does_not_break_the_page(self):
        self.client.cookies["latitude"] = "abc"
        self.client.cookies["longitude"] = "def"
        self.assertEqual(self.client.get(self.url).status_code, 200)


class LotCreateViewTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.open_auction, self.open_location = _open_auction(self.user)
        AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.open_auction,
            pickup_location=self.open_location,
            bidder_number="702",
            is_admin=True,
        )
        self.seller_tos = AuctionTOS.objects.create(
            user=self.userB, auction=self.open_auction, pickup_location=self.open_location, bidder_number="703"
        )
        give_contact_info(self.userB)
        self.client.force_login(self.userB)

    def _post(self, name="Brand new lot"):
        return self.client.post(
            f"{reverse('new_lot')}?auction={self.open_auction.slug}",
            {
                "lot_name": name,
                "quantity": 1,
                "reserve_price": 2,
                "part_of_auction": "True",
                "auction": self.open_auction.pk,
            },
        )

    def test_a_joined_seller_creates_a_lot_with_an_invoice(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        lot = Lot.objects.get(lot_name="Brand new lot")
        self.assertEqual(lot.auctiontos_seller, self.seller_tos)
        self.assertEqual(lot.user, self.userB)
        self.assertTrue(Invoice.objects.filter(auctiontos_user=self.seller_tos).exists())

    def test_a_seller_banned_by_a_co_admin_cannot_add_lots(self):
        """CreateLotForm checks only the creator's bans; LotValidation.form_valid catches the rest."""
        UserBan.objects.create(user=self.admin_user, banned_user=self.userB)
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Lot.objects.filter(lot_name="Brand new lot").exists())

    def test_lot_submission_ended_redirects_to_the_auction(self):
        self.open_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(hours=1)
        self.open_auction.save()
        response = self.client.get(f"{reverse('new_lot')}?auction={self.open_auction.slug}")
        self.assertRedirects(response, self.open_auction.get_absolute_url(), fetch_redirect_response=False)

    def test_an_unknown_auction_slug_just_shows_the_form(self):
        response = self.client.get(f"{reverse('new_lot')}?auction=no-such-auction")
        self.assertEqual(response.status_code, 200)

    def test_missing_contact_info_redirects_before_the_form(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(reverse("new_lot"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("contact_info"), response["Location"])


class ImageEndpointTests(WritableMediaRoot, StandardTestCase):
    def setUp(self):
        super().setUp()
        self.image_lot = Lot.objects.create(
            lot_name="Photo lot", auction=self.online_auction, auctiontos_seller=self.online_tos, quantity=1
        )
        buffer = BytesIO()
        Image.new("RGB", (20, 10), "red").save(buffer, format="JPEG")
        self.photo = LotImage.objects.create(
            lot_number=self.image_lot,
            image=SimpleUploadedFile("fish.jpg", buffer.getvalue(), content_type="image/jpeg"),
            image_source="RANDOM",
            is_primary=True,
        )
        self.second = LotImage.objects.create(
            lot_number=self.image_lot, url="https://example.com/fish.jpg", image_source="RANDOM"
        )

    def _size(self):
        self.photo.refresh_from_db()
        with self.photo.image.open() as f:
            return Image.open(f).size

    def test_the_owner_rotates_an_image(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/images/rotate/", {"pk": self.photo.pk, "angle": 90})
        self.assertEqual(response.content, b"Success")
        self.assertEqual(self._size(), (10, 20))

    def test_a_stranger_cannot_rotate(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post("/api/images/rotate/", {"pk": self.photo.pk, "angle": 90})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._size(), (20, 10))

    def test_rotate_with_a_bad_angle(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/images/rotate/", {"pk": self.photo.pk, "angle": "sideways"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._size(), (20, 10))

    def test_rotate_a_url_only_image(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/images/rotate/", {"pk": self.second.pk, "angle": 90})
        self.assertEqual(response.content, b"No image")

    def test_the_owner_changes_the_primary_image(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/images/primary/", {"pk": self.second.pk})
        self.assertEqual(response.content, b"Success")
        self.photo.refresh_from_db()
        self.second.refresh_from_db()
        self.assertFalse(self.photo.is_primary)
        self.assertTrue(self.second.is_primary)

    def test_a_stranger_cannot_change_the_primary_image(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post("/api/images/primary/", {"pk": self.second.pk})
        self.assertEqual(response.status_code, 302)
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_primary)

    def test_primary_with_a_missing_image(self):
        self.client.force_login(self.user)
        response = self.client.post("/api/images/primary/", {"pk": "nope"})
        self.assertEqual(response.status_code, 200)
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_primary)


class LotDeactivateTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.standalone = Lot.objects.create(lot_name="Standalone", user=self.user, quantity=1, date_end=_future())
        Bid.objects.create(user=self.userB, lot_number=self.standalone, amount=5)
        self.url = f"/api/lots/deactivate/{self.standalone.pk}/"

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_the_owner_deactivates_and_the_bids_go(self):
        self.client.force_login(self.user)
        self.client.post(self.url)
        self.standalone.refresh_from_db()
        self.assertTrue(self.standalone.deactivated)
        self.assertFalse(Bid.objects.exclude(is_deleted=True).filter(lot_number=self.standalone).exists())

    def test_posting_again_reactivates(self):
        self.client.force_login(self.user)
        self.client.post(self.url)
        self.client.post(self.url)
        self.standalone.refresh_from_db()
        self.assertFalse(self.standalone.deactivated)

    def test_a_stranger_cannot_deactivate(self):
        self.client.force_login(self.userB)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.standalone.refresh_from_db()
        self.assertFalse(self.standalone.deactivated)

    def test_a_lot_in_an_auction_cannot_be_deactivated_even_by_its_seller(self):
        self.client.force_login(self.user)
        self.client.post(f"/api/lots/deactivate/{self.unsoldLot.pk}/")
        self.unsoldLot.refresh_from_db()
        self.assertFalse(self.unsoldLot.deactivated)


class UserBanTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.open_auction, self.open_location = _open_auction(self.user)
        self.banned_tos = AuctionTOS.objects.create(
            user=self.userB, auction=self.open_auction, pickup_location=self.open_location, bidder_number="704"
        )
        self.banned_lot = Lot.objects.create(
            lot_name="Soon banned", auction=self.open_auction, auctiontos_seller=self.banned_tos, user=self.userB
        )
        self.own_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.open_location, bidder_number="705"
        )
        self.own_lot = Lot.objects.create(
            lot_name="Mine", auction=self.open_auction, auctiontos_seller=self.own_tos, user=self.user
        )

    def test_anonymous_is_refused(self):
        response = self.client.post(f"/api/users/ban/{self.userB.pk}/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserBan.objects.exists())

    def test_ban_removes_their_lots_from_my_auctions(self):
        self.client.force_login(self.user)
        response = self.client.post(f"/api/users/ban/{self.userB.pk}/")
        self.assertRedirects(
            response, reverse("userpage", kwargs={"slug": self.userB.username}), fetch_redirect_response=False
        )
        self.assertTrue(UserBan.objects.filter(user=self.user, banned_user=self.userB).exists())
        self.banned_lot.refresh_from_db()
        self.assertTrue(self.banned_lot.banned)

    def test_ban_leaves_lots_in_auctions_i_do_not_run_alone(self):
        self.client.force_login(self.user_with_no_lots)
        self.client.post(f"/api/users/ban/{self.userB.pk}/")
        self.banned_lot.refresh_from_db()
        self.assertFalse(self.banned_lot.banned)

    def test_banning_an_unknown_user_is_a_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/api/users/ban/999999999/").status_code, 404)

    # ajax.py: CreateUserBan accepts pk == request.user, so one POST removes the caller's own lots from their auctions.
    def test_banning_yourself_does_not_remove_your_own_lots(self):
        self.client.force_login(self.user)
        self.client.post(f"/api/users/ban/{self.user.pk}/")
        self.own_lot.refresh_from_db()
        self.assertFalse(self.own_lot.banned)

    def test_unban_removes_only_my_ban(self):
        UserBan.objects.create(user=self.user, banned_user=self.userB)
        UserBan.objects.create(user=self.admin_user, banned_user=self.userB)
        self.client.force_login(self.user)
        self.client.post(f"/api/users/unban/{self.userB.pk}/")
        self.assertFalse(UserBan.objects.filter(user=self.user, banned_user=self.userB).exists())
        self.assertTrue(UserBan.objects.filter(user=self.admin_user, banned_user=self.userB).exists())

    def test_unbanning_someone_never_banned_leaves_no_ban(self):
        self.client.force_login(self.user)
        response = self.client.post(f"/api/users/unban/{self.userB.pk}/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserBan.objects.exists())
