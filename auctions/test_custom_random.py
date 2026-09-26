"""The custom random field: an auction's option list that each lot is dealt from, and nobody edits."""

from unittest.mock import patch

from django.test import RequestFactory
from django.urls import reverse

from auctions import palette_actions
from auctions.filters import LotAdminFilter, LotFilter
from auctions.forms import QUICK_ADD_LOT_FIELDS, CreateLotForm, LabelPrintFieldsForm
from auctions.mobile.services.label_pdf import build_label_view
from auctions.models import AuctionDropdown, AuctionHistory, AuctionRandomOption, Lot
from auctions.services import clone_auction
from auctions.tests import StandardTestCase


class CustomRandomTestCase(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.auction = self.online_auction

    def switch_on(self, *options, name="Table"):
        for value in options:
            AuctionRandomOption.objects.create(auction=self.auction, user=self.user, value=value)
        self.auction.use_custom_random_field = True
        self.auction.custom_random_name = name
        self.auction.save()
        self.auction.assign_custom_random()

    def new_lot(self, name="Another lot"):
        return Lot.objects.create(lot_name=name, auction=self.auction, auctiontos_seller=self.online_tos, quantity=1)

    def values(self):
        return list(Lot.objects.filter(auction=self.auction, is_deleted=False).values_list("custom_random", flat=True))


class DealingTests(CustomRandomTestCase):
    def test_switching_it_on_deals_every_lot_already_there(self):
        self.switch_on("A", "B")
        self.assertTrue(all(value in {"A", "B"} for value in self.values()), self.values())

    def test_a_new_lot_is_dealt_an_option(self):
        self.switch_on("A", "B")
        self.assertIn(self.new_lot().custom_random, {"A", "B"})

    def test_the_deal_ignores_what_other_lots_have(self):
        """Balancing would make the next lot's option predictable, so a seller could order their lots."""
        self.switch_on("A", "B")
        Lot.objects.filter(auction=self.auction).update(custom_random="A")
        with patch("auctions.models.secrets.choice", return_value="A") as choice:
            self.assertEqual(self.new_lot().custom_random, "A")
        choice.assert_called_once_with(["A", "B"])

    def test_nothing_is_dealt_while_it_is_off(self):
        AuctionRandomOption.objects.create(auction=self.auction, value="A")
        AuctionRandomOption.objects.create(auction=self.auction, value="B")
        self.assertEqual(self.new_lot().custom_random, "")

    def test_a_lot_keeps_its_option_when_saved_again(self):
        self.switch_on("A", "B", "C")
        lot = self.new_lot()
        dealt = lot.custom_random
        for _ in range(5):
            lot.save()
        lot.refresh_from_db()
        self.assertEqual(lot.custom_random, dealt)

    def test_renaming_an_option_renames_it_on_every_lot(self):
        self.switch_on("A", "B")
        option = AuctionRandomOption.objects.get(auction=self.auction, value="A")
        had_it = Lot.objects.filter(auction=self.auction, custom_random="A").count()
        option.value = "Front"
        option.save()
        self.assertEqual(Lot.objects.filter(auction=self.auction, custom_random="Front").count(), had_it)
        self.assertFalse(Lot.objects.filter(auction=self.auction, custom_random="A").exists())

    def test_renaming_onto_another_option_merges_them(self):
        self.switch_on("A", "B", "C")
        Lot.objects.filter(auction=self.auction).update(custom_random="C")
        option = AuctionRandomOption.objects.get(auction=self.auction, value="C")
        option.value = "a"
        option.save()
        self.assertEqual(set(self.values()), {"A"})
        self.assertEqual(AuctionRandomOption.objects.filter(auction=self.auction).count(), 2)

    def test_removing_an_option_deals_its_lots_again_and_flags_printed_labels(self):
        self.switch_on("A", "B", "C")
        Lot.objects.filter(auction=self.auction).update(custom_random="C", label_printed=True)
        AuctionRandomOption.objects.get(auction=self.auction, value="C").delete()
        values = self.values()
        self.assertTrue(all(value in {"A", "B"} for value in values), values)
        self.assertFalse(Lot.objects.filter(auction=self.auction, label_needs_reprinting=False).exists())

    def test_a_lot_moved_from_another_auction_is_dealt_again(self):
        self.switch_on("A", "B")
        lot = self.in_person_lot
        Lot.objects.filter(pk=lot.pk).update(custom_random="Elsewhere")
        lot.refresh_from_db()
        lot.auction = self.auction
        lot.save()
        self.assertIn(lot.custom_random, {"A", "B"})

    def test_history_names_the_list(self):
        self.switch_on("A", "B")
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action="Added custom random option 'A'").exists()
        )


class NobodyEditsItTests(CustomRandomTestCase):
    def test_no_lot_form_has_it(self):
        self.assertNotIn("custom_random", CreateLotForm.base_fields)
        self.assertNotIn("custom_random", QUICK_ADD_LOT_FIELDS)

    def test_the_admin_shows_it_read_only(self):
        from auctions.admin import LotAdmin

        self.assertIn("custom_random", LotAdmin.readonly_fields)

    def test_the_custom_fields_form_wont_switch_it_on_with_one_option(self):
        from auctions.forms import AuctionCustomFieldsForm

        AuctionRandomOption.objects.create(auction=self.auction, value="A")
        form = AuctionCustomFieldsForm(instance=self.auction)
        data = {name: form.initial.get(name) for name in form.fields}
        data = {name: value for name, value in data.items() if value not in (None, False)}
        data.update(use_custom_random_field="on", custom_random_name="Table")
        form = AuctionCustomFieldsForm(data, instance=self.auction)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.use_custom_random_field)
        self.assertTrue(form.custom_random_auto_disabled)


class ShownPrintedAndSearchedTests(CustomRandomTestCase):
    def test_both_lot_pages_show_it(self):
        self.switch_on("Table 3", "Table 4")
        Lot.objects.filter(pk=self.lot.pk).update(custom_random="Table 3")
        self.lot.refresh_from_db()
        full = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        self.assertContains(full, "Table:")
        self.assertContains(full, "Table 3")
        # The pull-up view is the auctioneer's.
        self.client.login(username="my_lot", password="testpassword")
        simple = self.client.get(
            reverse("htmx_lot", kwargs={"slug": self.auction.slug, "custom_lot_number": self.lot.lot_number_display})
        )
        self.assertContains(simple, "Table:")
        self.assertContains(simple, "Table 3")

    def test_a_switched_off_field_is_not_shown(self):
        Lot.objects.filter(pk=self.lot.pk).update(custom_random="Table 3")
        self.auction.custom_random_name = "Table"
        self.auction.save()
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.custom_random_label, "")

    def test_it_is_on_the_label_by_default(self):
        self.switch_on("Table 3", "Table 4")
        self.assertIn("custom_random_label", self.auction.label_print_fields)
        self.lot.refresh_from_db()
        request = RequestFactory().get("/")
        request.user = self.admin_user
        view = build_label_view(self.lot, request, mark_printed=False)
        label = view.get_context_data()["labels"][0]
        self.assertIn(self.lot.custom_random, label.tags_left + label.tags_right)

    def test_the_label_setup_page_offers_it_by_name(self):
        self.switch_on("A", "B", name="Group")
        form = LabelPrintFieldsForm(auction=self.auction)
        self.assertEqual(form.fields["custom_random_label"].label, "Group")
        self.assertTrue(form.fields["custom_random_label"].initial)

    def test_admin_search_finds_it(self):
        self.switch_on("Table 3", "Table 13")
        Lot.objects.filter(pk=self.lot.pk).update(custom_random="Table 3")
        Lot.objects.filter(auction=self.auction).exclude(pk=self.lot.pk).update(custom_random="Table 13")
        qs = Lot.objects.filter(auction=self.auction)
        search = LotAdminFilter()
        search.queryset = qs
        self.assertEqual(list(search.generic(qs, "table 3")), [self.lot])

    def test_public_search_finds_the_whole_value(self):
        self.switch_on("Table 3", "Table 13")
        Lot.objects.filter(pk=self.lot.pk).update(custom_random="Table 3")
        Lot.objects.filter(auction=self.auction).exclude(pk=self.lot.pk).update(custom_random="Table 13")
        found = LotFilter(user=self.user).text_filter(Lot.objects.filter(auction=self.auction), "q", "Table 3")
        self.assertEqual(list(found), [self.lot])

    def test_copying_the_auction_copies_the_options(self):
        self.switch_on("A", "B")
        copy = clone_auction(self.auction, title="Next year", date_start=self.auction.date_start, created_by=self.user)
        self.assertTrue(copy.use_custom_random_field)
        self.assertEqual(
            sorted(AuctionRandomOption.objects.filter(auction=copy).values_list("value", flat=True)), ["A", "B"]
        )


class OptionsAPITests(CustomRandomTestCase):
    def url(self):
        return reverse("auction_custom_random_options", kwargs={"slug": self.auction.slug})

    def test_an_admin_can_add_rename_and_remove(self):
        self.client.login(username="my_lot", password="testpassword")
        created = self.client.post(self.url(), {"action": "create", "value": "A"}).json()
        self.assertTrue(created["success"], created)
        option_id = created["option"]["id"]
        renamed = self.client.post(self.url(), {"action": "update", "option_id": option_id, "value": "Front"}).json()
        self.assertTrue(renamed["success"], renamed)
        self.assertTrue(AuctionRandomOption.objects.filter(pk=option_id, value="Front").exists())
        deleted = self.client.post(self.url(), {"action": "delete", "option_id": option_id}).json()
        self.assertTrue(deleted["success"], deleted)
        self.assertFalse(AuctionRandomOption.objects.filter(pk=option_id).exists())
        self.assertFalse(AuctionDropdown.objects.filter(auction=self.auction).exists())

    def test_anyone_else_is_refused(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(self.url(), {"action": "create", "value": "A"})
        self.assertIn(response.status_code, [302, 403])
        self.assertFalse(AuctionRandomOption.objects.filter(auction=self.auction).exists())

    def test_the_custom_fields_page_lists_both(self):
        AuctionRandomOption.objects.create(auction=self.auction, value="Back table")
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("edit_auction_custom_fields", kwargs={"slug": self.auction.slug}))
        self.assertContains(response, "Custom random options")
        self.assertContains(response, "Custom dropdown options")
        self.assertContains(response, "Back table")


class OptionsOverMCPTests(CustomRandomTestCase):
    def run_action(self, action, user=None, **params):
        request = RequestFactory().post("/")
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.run_action(request, action, {"auction": self.auction.slug, **params})

    def test_random_options_can_be_added_renamed_and_removed(self):
        for value in ("A", "B"):
            added = self.run_action("add_random_option", option=value)
            self.assertTrue(added.get("ok"), added)
        renamed = self.run_action("rename_random_option", option="a", new_name="Front")
        self.assertTrue(renamed.get("ok"), renamed)
        self.assertEqual(renamed["options"], ["Front", "B"])
        removed = self.run_action("remove_random_option", option="Front")
        self.assertTrue(removed.get("ok"), removed)
        self.assertEqual(removed["options"], ["B"])

    def test_dropdown_options_can_be_renamed(self):
        AuctionDropdown.objects.create(auction=self.auction, value="Cichlid")
        renamed = self.run_action("rename_dropdown_option", option="Cichlid", new_name="Cichlids")
        self.assertTrue(renamed.get("ok"), renamed)
        self.assertTrue(AuctionDropdown.objects.filter(auction=self.auction, value="Cichlids").exists())
        self.assertEqual(renamed["undo"]["params"]["new_name"], "Cichlid")

    def test_a_rename_onto_another_option_is_refused(self):
        for value in ("A", "B"):
            self.run_action("add_random_option", option=value)
        clash = self.run_action("rename_random_option", option="A", new_name="b")
        self.assertNotIn("ok", clash)

    def test_a_stranger_cannot_touch_either_list(self):
        AuctionDropdown.objects.create(auction=self.auction, value="Cichlid")
        AuctionRandomOption.objects.create(auction=self.auction, value="A")
        for action, params in (
            ("add_random_option", {"option": "B"}),
            ("rename_random_option", {"option": "A", "new_name": "Z"}),
            ("remove_random_option", {"option": "A"}),
            ("rename_dropdown_option", {"option": "Cichlid", "new_name": "Z"}),
        ):
            with self.subTest(action):
                result = self.run_action(action, user=self.user_with_no_lots, **params)
                self.assertNotIn("ok", result)
        self.assertEqual(
            list(AuctionRandomOption.objects.filter(auction=self.auction).values_list("value", flat=True)), ["A"]
        )
        self.assertTrue(AuctionDropdown.objects.filter(auction=self.auction, value="Cichlid").exists())

    def test_describe_lot_says_what_it_was_dealt(self):
        self.switch_on("Table 3", "Table 4")
        self.lot.refresh_from_db()
        result = self.run_action("describe_lot", lot=self.lot.lot_number_display)
        self.assertIn(self.lot.custom_random, result.get("summary", ""), result)
