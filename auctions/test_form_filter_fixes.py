"""Regression tests for a batch of form and filter fixes: edit locks and limits on lot forms, the posted
hidden ``auction`` no longer trusted, club roster copying gated on club permission, bounded numeric
fields, and the lot/participant filters (popularity subqueries, search caps, ``isdecimal``, keyword
word boundaries, the "Ended" status).
"""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from auctions.filters import (
    TEXT_FILTER_MAX_FRAGMENTS,
    AuctionFilter,
    AuctionTOSFilter,
    LotAdminFilter,
    LotFilter,
)
from auctions.forms import (
    AuctionEditForm,
    CreateLotForm,
    EditLot,
    InvoiceAdjustmentForm,
    PickupLocationForm,
    VolunteerJobForm,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Category,
    Club,
    ClubMember,
    Lot,
    LotHistory,
    PageView,
    PickupLocation,
    UserData,
)
from auctions.tests import StandardTestCase


def _open_auction(creator, title, **extra):
    now = timezone.now()
    auction = Auction.objects.create(
        created_by=creator,
        title=title,
        is_online=True,
        date_start=now - datetime.timedelta(days=1),
        date_end=now + datetime.timedelta(days=3),
        lot_submission_start_date=now - datetime.timedelta(days=1),
        lot_submission_end_date=now + datetime.timedelta(days=3),
        winning_bid_percent_to_club=25,
        **extra,
    )
    location = PickupLocation.objects.create(
        name=f"{title} location", auction=auction, pickup_time=now + datetime.timedelta(days=3)
    )
    return auction, location


class BulkAddFormsetEditLockTests(StandardTestCase):
    """The classic bulk formset let a seller rewrite a lot that already had bids or had sold."""

    def setUp(self):
        super().setUp()
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=7)
        self.in_person_auction.save()
        self.sold = Lot.objects.create(
            lot_name="Sold guppies",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            user=self.user_with_no_lots,
            quantity=1,
            reserve_price=5,
            auctiontos_winner=self.in_person_tos,
            winning_price=12,
        )
        self.url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.client.login(username="no_lots", password="testpassword")

    def _posted_formset(self):
        """What the page posts back untouched: every bound field's rendered value."""
        formset = self.client.get(self.url).context["formset"]
        data = {
            f"{formset.prefix}-TOTAL_FORMS": str(len(formset.forms)),
            f"{formset.prefix}-INITIAL_FORMS": str(formset.initial_form_count()),
            f"{formset.prefix}-MIN_NUM_FORMS": "0",
            f"{formset.prefix}-MAX_NUM_FORMS": "1000",
        }
        for form in formset.forms[: formset.initial_form_count()]:
            for bound in form:
                value = bound.value()
                if value is True:
                    data[bound.html_name] = "on"
                elif value not in (None, False):
                    data[bound.html_name] = str(value)
        return data, formset

    def test_changing_a_sold_lot_is_refused_and_shown(self):
        data, formset = self._posted_formset()
        index = next(i for i, form in enumerate(formset.forms) if form.instance.pk == self.sold.pk)
        data[f"form-{index}-lot_name"] = "Rewritten"
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This lot has sold")
        self.sold.refresh_from_db()
        self.assertEqual(self.sold.lot_name, "Sold guppies")

    def test_an_untouched_sold_lot_does_not_block_adding_a_new_one(self):
        data, formset = self._posted_formset()
        new = formset.initial_form_count()
        data["form-TOTAL_FORMS"] = str(new + 1)
        data[f"form-{new}-lot_name"] = "Fresh platies"
        data[f"form-{new}-quantity"] = "1"
        data[f"form-{new}-reserve_price"] = "5"
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302, response.context and response.context["formset"].errors)
        self.assertTrue(Lot.objects.filter(lot_name="Fresh platies", auctiontos_seller=self.in_person_buyer).exists())


class CreateLotFormAuctionMoveTests(StandardTestCase):
    """Editing a lot into another auction skipped that auction's limit and kept the old lot number."""

    def setUp(self):
        super().setUp()
        self.user.first_name, self.user.last_name = "Test", "User"
        self.user.save()
        UserData.objects.filter(user=self.user).update(address="123 Test St")
        self.first, first_location = _open_auction(self.admin_user, "First open auction")
        self.second, second_location = _open_auction(self.admin_user, "Second open auction")
        self.first_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.first, pickup_location=first_location, bidder_number="701"
        )
        self.second_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.second, pickup_location=second_location, bidder_number="702"
        )
        for name in ("Second one", "Second two", "Second three"):
            Lot.objects.create(
                lot_name=name, auction=self.second, auctiontos_seller=self.second_tos, user=self.user, quantity=1
            )
        self.lot = Lot.objects.create(
            lot_name="Mover", auction=self.first, auctiontos_seller=self.first_tos, user=self.user, quantity=1
        )
        self.uncategorized = Category.objects.filter(name="Uncategorized").first()

    def _data(self, auction, **extra):
        data = {
            "part_of_auction": "True",
            "auction": str(auction.pk),
            "lot_name": "Mover",
            "quantity": "1",
            "reserve_price": "5",
        }
        if self.uncategorized:
            data["species_category"] = str(self.uncategorized.pk)
        data.update(extra)
        return data

    def _form(self, data, instance=None):
        return CreateLotForm(data=data, instance=instance, user=self.user, cloned_from=None, auction=None)

    def test_moving_into_a_full_auction_is_refused(self):
        self.second.max_lots_per_user = 3
        self.second.allow_additional_lots_as_donation = False
        self.second.save()
        form = self._form(self._data(self.second), instance=Lot.objects.get(pk=self.lot.pk))
        self.assertFalse(form.is_valid())
        self.assertIn("auction", form.errors)

    def test_editing_in_place_at_the_limit_is_still_allowed(self):
        self.first.max_lots_per_user = 1
        self.first.save()
        form = self._form(self._data(self.first), instance=Lot.objects.get(pk=self.lot.pk))
        self.assertTrue(form.is_valid(), form.errors)

    def test_moving_takes_a_fresh_lot_number_in_the_new_auction(self):
        self.assertEqual(self.lot.lot_number_int, 1)
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.post(reverse("edit_lot", kwargs={"pk": self.lot.pk}), self._data(self.second))
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.auction, self.second)
        self.assertEqual(self.lot.auctiontos_seller, self.second_tos)
        self.assertEqual(self.lot.lot_number_int, 4)

    def test_moving_into_a_seller_dash_auction_takes_that_sellers_number(self):
        self.second.use_seller_dash_lot_numbering = True
        self.second.save()
        Lot.objects.filter(pk=self.lot.pk).update(custom_lot_number="701-1")
        self.client.login(username=self.user.username, password="testpassword")
        self.client.post(reverse("edit_lot", kwargs={"pk": self.lot.pk}), self._data(self.second))
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.auction, self.second)
        self.assertTrue(self.lot.custom_lot_number.startswith("702-"), self.lot.custom_lot_number)

    def test_required_buy_now_and_custom_field_are_enforced(self):
        self.first.buy_now = "required"
        self.first.custom_field_1 = "required"
        self.first.custom_field_1_name = "Tank"
        self.first.save()
        form = self._form(self._data(self.first, lot_name="New lot"))
        self.assertFalse(form.is_valid())
        self.assertIn("buy_now_price", form.errors)
        self.assertIn("custom_field_1", form.errors)
        form = self._form(self._data(self.first, lot_name="New lot", buy_now_price="20", custom_field_1="B3"))
        self.assertTrue(form.is_valid(), form.errors)


class ManageUsersThroughClubPermissionTests(StandardTestCase):
    """Turning on club management copies the club roster into the auction; it needs a club role."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Roster club")
        self.in_person_auction.club = self.club
        self.in_person_auction.manage_users_through_club = ""
        self.in_person_auction.save()
        Lot.objects.filter(auction=self.in_person_auction).delete()

    def _form(self, user):
        return AuctionEditForm(
            data={"manage_users_through_club": "all", "club": str(self.club.pk)},
            instance=self.in_person_auction,
            user=user,
            cloned_from=None,
            user_timezone="UTC",
        )

    def test_auction_creator_without_a_club_role_is_refused(self):
        form = self._form(self.user)
        form.is_valid()
        self.assertIn("manage_users_through_club", form.errors)
        self.assertIn("admins", str(form.errors["manage_users_through_club"]))

    def test_club_auction_manager_is_allowed(self):
        ClubMember.objects.create(club=self.club, user=self.user, name="Manager", permission_manage_auctions=True)
        form = self._form(self.user)
        form.is_valid()
        self.assertNotIn("manage_users_through_club", form.errors)


class EditLotUsesItsOwnAuctionTests(StandardTestCase):
    """EditLot checked the posted hidden ``auction``, which could name another auction."""

    def _form(self, **data):
        base = {"lot_name": self.lot.lot_name, "auction": str(self.in_person_auction.pk), "quantity": "1"}
        base.update(data)
        return EditLot(user=self.user, lot=self.lot, auction=self.online_auction, instance=self.lot, data=base)

    def test_the_lots_auction_rules_apply_whatever_is_posted(self):
        self.online_auction.only_whole_dollar_bids = True
        self.online_auction.save()
        form = self._form(reserve_price="1.50")
        self.assertFalse(form.is_valid())
        self.assertIn("reserve_price", form.errors)

    def test_a_negative_sell_price_is_refused(self):
        form = self._form(reserve_price="1", auctiontos_winner=str(self.tosB.pk), winning_price="-5")
        self.assertFalse(form.is_valid())
        self.assertIn("winning_price", form.errors)


class PickupLocationFormAuctionTests(StandardTestCase):
    def test_second_mail_location_is_refused_whatever_auction_is_posted(self):
        PickupLocation.objects.create(name="By mail", auction=self.online_auction, pickup_by_mail=True)
        form = PickupLocationForm(
            self.user,
            self.online_auction,
            {"name": "Another", "mail_or_not": "True", "auction": ""},
            instance=None,
            is_edit_form=False,
            pickup_location=None,
            user_timezone="UTC",
        )
        form.is_valid()
        self.assertIn("mail_or_not", form.errors)


class NumericLimitTests(StandardTestCase):
    def test_volunteer_bounty_cannot_be_negative(self):
        form = VolunteerJobForm(data={"description": "Net fish", "bounty": "-5", "people_needed": "1"})
        self.assertFalse(form.is_valid())
        self.assertIn("bounty", form.errors)

    def test_auction_tax_is_at_most_100_percent(self):
        form = AuctionEditForm(
            data={"tax": "150"},
            instance=self.online_auction,
            user=self.user,
            cloned_from=None,
            user_timezone="UTC",
        )
        form.is_valid()
        self.assertIn("tax", form.errors)

    def test_invoice_adjustment_amount_is_bounded(self):
        form = InvoiceAdjustmentForm(
            data={"adjustment_type": "ADD", "amount": "1000000000", "notes": "x"}, invoice=self.invoice
        )
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)
        form = InvoiceAdjustmentForm(
            data={"adjustment_type": "ADD", "amount": "500", "notes": "x"}, invoice=self.invoice
        )
        self.assertTrue(form.is_valid(), form.errors)


class LotFilterTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = User.objects.create_user(username="viewer", password="testpassword")

    def _qs(self, **data):
        return LotFilter(data=data, user=self.viewer, regardingAuction=self.in_person_auction).qs

    def test_popularity_counts_each_kind_once(self):
        lot = self.in_person_lot
        for _ in range(3):
            PageView.objects.create(lot_number=lot, user=self.viewer)
        LotHistory.objects.create(lot=lot, message="nice", changed_price=False)
        LotHistory.objects.create(lot=lot, message="nicer", changed_price=False)
        LotHistory.objects.create(lot=lot, message="bid", changed_price=True)
        row = self._qs(order="popularity").get(pk=lot.pk)
        # 2 x 3 views + 2 chats + 2.5 x 1 price change; a joined aggregate multiplied these.
        self.assertEqual(Decimal(str(row.popularity)), Decimal("10.5"))

    def test_unloved_sort_runs(self):
        self.assertIn(self.in_person_lot, list(self._qs(order="unloved")))

    def test_closed_status_shows_only_ended_lots(self):
        ended = Lot.objects.create(lot_name="Ended lot", auction=self.in_person_auction, quantity=1, active=False)
        lots = list(self._qs(status="closed"))
        self.assertIn(ended, lots)
        self.assertNotIn(self.in_person_lot, lots)

    def test_text_search_caps_or_fragments(self):
        names = [f"zq{letter}fish" for letter in "abcdefgh"]
        for name in names:
            Lot.objects.create(lot_name=name, auction=self.in_person_auction, quantity=1)
        found = LotFilter(user=self.viewer).text_filter(Lot.objects.all(), "q", " or ".join(names))
        self.assertEqual(sorted(found.values_list("lot_name", flat=True)), sorted(names[:TEXT_FILTER_MAX_FRAGMENTS]))

    def test_text_search_caps_length(self):
        Lot.objects.create(lot_name="Tailfish", auction=self.in_person_auction, quantity=1)
        value = ("x" * 250) + " or Tailfish"
        found = LotFilter(user=self.viewer).text_filter(Lot.objects.all(), "q", value)
        self.assertFalse(found.filter(lot_name="Tailfish").exists())

    def test_vulgar_fraction_is_text_not_a_number(self):
        found = LotFilter(user=self.viewer).text_filter(Lot.objects.all(), "q", "½")
        self.assertEqual(list(found), [])


class NumericLookingSearchTests(StandardTestCase):
    """ "½" and "²" pass isnumeric() and then broke int() and integer lookups."""

    def test_auction_filter(self):
        list(AuctionFilter().auction_search(Auction.objects.all(), "query", "²"))

    def test_lot_admin_filter(self):
        admin_filter = LotAdminFilter(queryset=Lot.objects.filter(auction=self.online_auction))
        for value in ("½", "lot:½"):
            with self.subTest(value=value):
                list(admin_filter.generic(Lot.objects.filter(auction=self.online_auction), value))


class AuctionTOSKeywordBoundaryTests(StandardTestCase):
    """Keywords like "sus" and "open" matched the start of names like Susan and Openshaw."""

    def setUp(self):
        super().setUp()
        self.susan = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, name="Susan Smith", bidder_number="901"
        )
        self.openshaw = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, name="Openshaw Jones", bidder_number="902"
        )
        self.qs = AuctionTOS.objects.filter(auction=self.online_auction)

    def test_names_starting_with_a_keyword_are_names(self):
        self.assertIn(self.susan, AuctionTOSFilter.generic(None, self.qs, "susan"))
        self.assertIn(self.openshaw, AuctionTOSFilter.generic(None, self.qs, "openshaw"))

    def test_the_keywords_still_work(self):
        self.assertIn(self.susan, AuctionTOSFilter.generic(None, self.qs, "sus susan"))
        # "open" filters to draft invoices; neither new participant has an invoice.
        self.assertNotIn(self.openshaw, AuctionTOSFilter.generic(None, self.qs, "open openshaw"))


class GetClubsTests(StandardTestCase):
    def test_post_without_search_is_not_a_500(self):
        Club.objects.create(name="Listed club")
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post("/api/clubs/", {})
        self.assertEqual(response.status_code, 200)
