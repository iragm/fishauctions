"""Tests for the measurement half of the usability campaign: what an edit changed, and who has ever
changed it.

Covers ``auctions.history`` (``changed_fields`` on both changelogs), ``auctions.field_adoption``
(the retroactive "has anybody ever moved this off its default" table) and the essentials/advanced
split in ``auctions.auction_form_layout``.
"""

import datetime
from decimal import Decimal

from django import forms
from django.contrib.auth.models import User
from django.template import Context
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import field_adoption, history
from auctions.auction_form_layout import ESSENTIAL_FIELDS
from auctions.forms import AuctionEditForm
from auctions.models import Auction, AuctionHistory, Club, ClubHistory
from auctions.tests import StandardTestCase


def _as_posted(bound_field):
    """One bound field's value as a browser would submit it, or None for "not submitted at all".

    An unchecked checkbox is *absent* from a POST rather than present and empty, and a select
    posts one scalar rather than the list ``ChoiceWidget.format_value`` returns. Getting either
    wrong makes a form that changed nothing look like a form that changed ten things.
    """
    widget = bound_field.field.widget
    value = bound_field.value()
    if isinstance(widget, forms.CheckboxInput):
        return "on" if value else None
    if isinstance(widget, forms.SelectMultiple):
        return [str(item) for item in (value or [])]
    if value is None:
        return ""
    formatted = widget.format_value(value)
    if isinstance(formatted, list | tuple):
        formatted = formatted[0] if formatted else ""
    return "" if formatted is None else formatted


class JsonableTests(TestCase):
    """auctions.history.jsonable: nothing reaches a JSONField that json.dumps would reject."""

    def test_passes_through_json_scalars(self):
        for value in (None, True, False, 0, 5, -3, 1.5, "text"):
            self.assertEqual(history.jsonable(value), value)

    def test_decimal_becomes_a_number(self):
        self.assertEqual(history.jsonable(Decimal("12.50")), 12.5)

    def test_dates_become_strings(self):
        stamp = datetime.datetime(2026, 9, 8, 12, 30, tzinfo=datetime.UTC)
        self.assertEqual(history.jsonable(stamp), str(stamp))
        self.assertEqual(history.jsonable(datetime.date(2026, 9, 8)), "2026-09-08")

    def test_non_finite_floats_do_not_reach_the_column(self):
        # json.dumps encodes these as bare Infinity/NaN, which is not JSON and which MariaDB
        # rejects when the column's CHECK constraint validates it -- inside the edit's transaction.
        self.assertEqual(history.jsonable(float("inf")), "inf")
        self.assertEqual(history.jsonable(float("nan")), "nan")

    def test_long_strings_are_truncated_and_marked(self):
        result = history.jsonable("x" * 5000)
        self.assertEqual(len(result), history.MAX_VALUE_LENGTH)
        self.assertTrue(result.endswith("…"))

    def test_an_object_whose_str_raises_does_not_raise_here(self):
        class Hostile:
            def __str__(self):
                message = "no"
                raise ValueError(message)

        self.assertEqual(history.jsonable(Hostile()), "[unreadable]")

    def test_a_model_instance_becomes_its_str(self):
        club = Club.objects.create(name="Jsonable club")
        self.assertEqual(history.jsonable(club), str(club))

    def test_lists_are_stored_element_by_element(self):
        self.assertEqual(history.jsonable([1, "two", Decimal(3)]), [1, "two", 3.0])

    def test_deeply_nested_values_stop_recursing(self):
        self.assertEqual(history.jsonable([[["deep"]]]), [["['deep']"]])


class SecretFieldTests(TestCase):
    """A changelog is not a place to keep a credential."""

    def test_credential_shaped_names_are_recognised(self):
        for name in ("paypal_secret", "brevo_api_key", "password1", "mailchimp_access_token"):
            self.assertTrue(history.is_secret_field(name), name)

    def test_ordinary_names_are_not(self):
        for name in ("tax", "date_end", "invoice_rounding", "club"):
            self.assertFalse(history.is_secret_field(name), name)

    def test_a_secret_field_is_recorded_as_changed_but_not_stored(self):
        class FakeForm:
            changed_data = ["paypal_secret", "tax"]
            initial = {"paypal_secret": "old-secret", "tax": 0}
            cleaned_data = {"paypal_secret": "new-secret", "tax": 7}

        summary = history.changed_field_summary(FakeForm())
        self.assertEqual(summary["paypal_secret"], {"from": history.REDACTED, "to": history.REDACTED})
        self.assertEqual(summary["tax"], {"from": 0, "to": 7})


class ChangedFieldSummaryTests(TestCase):
    def test_no_form_is_an_empty_summary(self):
        self.assertEqual(history.changed_field_summary(None), {})

    def test_a_form_that_cannot_say_what_changed_does_not_take_the_edit_down(self):
        class Broken:
            @property
            def changed_data(self):
                message = "validation exploded"
                raise ValueError(message)

        self.assertEqual(history.changed_field_summary(Broken()), {})

    def test_more_changes_than_a_human_makes_are_capped(self):
        names = [f"field_{index}" for index in range(history.MAX_FIELDS + 50)]

        class FakeForm:
            changed_data = names
            initial = {}
            cleaned_data = {}

        self.assertEqual(len(history.changed_field_summary(FakeForm())), history.MAX_FIELDS)


class AuctionHistoryChangedFieldsTests(StandardTestCase):
    """Auction.create_history writes the queryable summary alongside the prose."""

    def _bound_form(self, **overrides):
        """The edit form, resubmitted exactly as rendered, with `overrides` applied.

        Values come through each widget's ``format_value``, which is what the browser posts back.
        Building the dict from ``form.initial`` instead looks equivalent and is not: for a datetime
        whose widget declares no microsecond support, ``get_initial_for_field`` strips microseconds
        from the *initial* side of the comparison and nothing strips them from the data side, so
        all four date fields come back as changed on a submission that changed nothing. That is a
        bug in the test, not in the form -- see test_resubmitting_the_form_unchanged_changes_nothing
        -- and it is worth the four lines here, because a helper that quietly marks four fields
        dirty would make every assertion below weaker than it looks.
        """
        kwargs = {
            "instance": self.online_auction,
            "user": self.user,
            "cloned_from": None,
            "user_timezone": "America/New_York",
        }
        unbound = AuctionEditForm(**kwargs)
        data = {}
        for name in unbound.fields:
            value = _as_posted(unbound[name])
            if value is not None:
                data[name] = value
        data.update(overrides)
        form = AuctionEditForm(data=data, **kwargs)
        form.is_valid()
        return form

    def test_resubmitting_the_form_unchanged_changes_nothing(self):
        """Saving a form you did not touch must record nothing.

        This is the guard on everything else here. A field that reads as changed on every save
        writes a history row every time, and its adoption numbers -- both halves -- become noise
        that looks like signal. The datetime pickers are the ones to watch: their rendered format
        has to round-trip through DateTimeField.has_changed(), and nothing else on the site would
        notice if it stopped.
        """
        self.assertEqual(self._bound_form().changed_data, [])

    def test_prose_is_unchanged_and_the_summary_is_written(self):
        form = self._bound_form(tax=7)
        self.assertIn("tax", form.changed_data)
        self.online_auction.create_history(applies_to="RULES", user=self.user, form=form)
        row = AuctionHistory.objects.filter(auction=self.online_auction).order_by("-id").first()
        self.assertIn("tax", row.action.lower())
        self.assertEqual(row.changed_fields["tax"]["from"], 25)
        self.assertEqual(row.changed_fields["tax"]["to"], 7)

    def test_a_field_the_edit_left_alone_is_not_in_the_summary(self):
        form = self._bound_form(tax=7)
        self.online_auction.create_history(applies_to="RULES", user=self.user, form=form)
        row = AuctionHistory.objects.filter(auction=self.online_auction).order_by("-id").first()
        self.assertNotIn("minimum_bid", row.changed_fields)
        self.assertNotIn("lot_entry_fee", row.changed_fields)

    def test_the_summary_is_queryable_by_field_name(self):
        AuctionHistory.objects.create(
            auction=self.online_auction,
            action="Edited Tax",
            applies_to="RULES",
            changed_fields={"tax": {"from": 0, "to": 5}},
        )
        self.assertEqual(AuctionHistory.objects.filter(changed_fields__has_key="tax").count(), 1)
        self.assertEqual(AuctionHistory.objects.filter(changed_fields__has_key="minimum_bid").count(), 0)

    def test_history_without_a_form_stores_an_empty_summary(self):
        self.online_auction.create_history(applies_to="STATS", action="Stats updated")
        row = AuctionHistory.objects.filter(auction=self.online_auction).order_by("-id").first()
        self.assertEqual(row.changed_fields, {})

    def test_a_field_below_the_800_character_truncation_is_still_in_the_summary(self):
        """The bug the summary exists to fix: prose truncates in form-field order.

        A wide edit loses the tail of `action`, and which fields are in the tail is decided by
        where they sit in the layout -- so the prose column systematically forgets the bottom of
        the form. The summary is a JSON column with no such limit.
        """
        names = [f"field_number_{index}_with_a_long_name" for index in range(60)]

        class WideForm:
            changed_data = names
            initial = dict.fromkeys(names, "before")
            cleaned_data = dict.fromkeys(names, "after")
            instance = self.online_auction

        form = WideForm()
        self.online_auction.create_history(applies_to="RULES", user=self.user, form=form)
        row = AuctionHistory.objects.filter(auction=self.online_auction).order_by("-id").first()
        self.assertEqual(len(row.action), 800)
        self.assertNotIn(names[-1].replace("_", " ").title(), row.action)
        self.assertIn(names[-1], row.changed_fields)


class ClubHistoryChangedFieldsTests(StandardTestCase):
    def test_record_club_history_names_the_fields_and_stores_them(self):
        club = Club.objects.create(name="Recorded club")

        class FakeForm:
            changed_data = ["name", "homepage"]
            initial = {"name": "Old", "homepage": ""}
            cleaned_data = {"name": "Recorded club", "homepage": "https://example.com"}
            instance = club

        row = history.record_club_history(club, "SETTINGS", action="Updated club settings", form=FakeForm())
        self.assertIn("Updated club settings", row.action)
        self.assertEqual(row.changed_fields["homepage"]["to"], "https://example.com")
        self.assertEqual(ClubHistory.objects.filter(changed_fields__has_key="homepage").count(), 1)

    def test_a_club_history_row_with_no_form_still_works(self):
        club = Club.objects.create(name="Plain club")
        row = history.record_club_history(club, "MEMBERS", action="Added member Bob")
        self.assertEqual(row.action, "Added member Bob")
        self.assertEqual(row.changed_fields, {})


class FieldAdoptionTests(StandardTestCase):
    """The retroactive half: which settings has anybody ever moved off the default."""

    def test_a_field_nobody_has_touched_reads_as_unused(self):
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        # Nothing in the fixture sets a minimum bid, and no history row names it.
        self.assertEqual(rows["minimum_bid"].off_default, 0)
        self.assertEqual(rows["minimum_bid"].edits, 0)
        self.assertEqual(rows["minimum_bid"].verdict, "unused")

    def test_a_field_the_fixture_sets_reads_as_used(self):
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        # Both fixture auctions carry tax=25 against a default of 0.
        self.assertGreaterEqual(rows["tax"].off_default, 2)
        self.assertEqual(rows["tax"].verdict, "used")

    def test_a_field_with_no_default_is_reported_as_unmeasured_rather_than_guessed(self):
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        self.assertEqual(rows["date_start"].verdict, "unmeasured")
        self.assertFalse(rows["date_start"].default_known)

    def test_history_rows_are_counted_per_auction_not_per_edit(self):
        for _ in range(4):
            AuctionHistory.objects.create(
                auction=self.online_auction,
                action="Edited Minimum bid",
                applies_to="RULES",
                changed_fields={"minimum_bid": {"from": 0, "to": 3}},
            )
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        self.assertEqual(rows["minimum_bid"].edits, 1)

    def test_an_undone_change_still_counts_as_an_edit(self):
        """The one thing the off_default column cannot see."""
        AuctionHistory.objects.create(
            auction=self.online_auction,
            action="Edited Minimum bid",
            applies_to="RULES",
            changed_fields={"minimum_bid": {"from": 0, "to": 3}},
        )
        AuctionHistory.objects.create(
            auction=self.online_auction,
            action="Edited Minimum bid",
            applies_to="RULES",
            changed_fields={"minimum_bid": {"from": 3, "to": 0}},
        )
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        self.assertEqual(rows["minimum_bid"].off_default, 0)
        self.assertEqual(rows["minimum_bid"].edits, 1)
        self.assertNotEqual(rows["minimum_bid"].verdict, "unused")

    def test_every_field_on_the_form_gets_a_row(self):
        rows = {row.name: row for row in field_adoption.auction_field_adoption(use_cache=False)}
        for name in AuctionEditForm.Meta.fields:
            self.assertIn(name, rows, f"{name} is on the form but not in the adoption table")

    def test_deleted_auctions_are_left_out_of_the_denominator(self):
        before = field_adoption.auction_field_adoption(use_cache=False)[0].total
        Auction.objects.create(
            created_by=self.user,
            title="Deleted auction",
            is_online=True,
            date_end=timezone.now(),
            date_start=timezone.now(),
            is_deleted=True,
        )
        after = field_adoption.auction_field_adoption(use_cache=False)[0].total
        self.assertEqual(before, after)


class AuctionEditFormLayoutTests(StandardTestCase):
    """The essentials/advanced split -- see auctions/auction_form_layout.py."""

    def _form(self, auction=None, user=None, data=None):
        return AuctionEditForm(
            instance=auction or self.online_auction,
            user=user or self.user,
            cloned_from=None,
            user_timezone="America/New_York",
            data=data,
        )

    def test_every_form_field_is_in_exactly_one_half(self):
        form = self._form()
        essential = [name for name in form.fields if name in ESSENTIAL_FIELDS]
        advanced = form.advanced_fields
        self.assertEqual(sorted(essential + advanced), sorted(form.fields))
        self.assertFalse(set(essential) & set(advanced))

    def test_every_essential_field_actually_exists_on_the_form(self):
        """ESSENTIAL_FIELDS naming something the form does not have would silently do nothing."""
        form = self._form()
        self.assertEqual(ESSENTIAL_FIELDS - set(form.fields), set())

    def test_every_field_is_rendered_somewhere(self):
        """A field dropped from the layout keeps its value out of the POST and silently resets it."""
        form = self._form()
        rendered = str(form.helper.render_layout(form, Context({"form": form}), template_pack="bootstrap5"))
        for name in form.fields:
            self.assertIn(f'name="{name}"', rendered, f"{name} is on the form but not in the layout")

    def test_the_advanced_section_opens_and_closes(self):
        form = self._form()
        rendered = str(form.helper.render_layout(form, Context({"form": form}), template_pack="bootstrap5"))
        self.assertEqual(rendered.count("<details"), 1)
        self.assertEqual(rendered.count("</details>"), 1)

    def test_a_setting_already_in_use_forces_the_section_open(self):
        """The rule that keeps Advanced from hiding somebody's decision."""
        self.online_auction.minimum_bid = 7
        self.online_auction.save()
        self.assertIn("minimum_bid", self._form().advanced_fields)
        self.assertTrue(self._form().advanced_open)

    def test_an_all_default_auction_for_a_new_organizer_starts_closed(self):
        auction = Auction.objects.create(
            created_by=self.user_with_no_lots,
            title="A first auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=3),
            date_start=timezone.now(),
        )
        form = self._form(auction=auction, user=self.user_with_no_lots)
        self.assertFalse(form.advanced_open)

    def test_a_rejected_advanced_field_forces_the_section_open(self):
        """A closed <details> over a rejected field is a form that cannot be fixed."""
        form = self._form(data={"tax": "not a number", "date_start": "", "date_end": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("tax", form.errors)
        self.assertTrue(form.advanced_open)

    def test_an_experienced_organizer_gets_it_open(self):
        experienced = User.objects.create_user(username="experienced", password="testpassword")
        for index in range(4):
            Auction.objects.create(
                created_by=experienced,
                title=f"Auction {index}",
                is_online=True,
                date_end=timezone.now() + datetime.timedelta(days=3),
                date_start=timezone.now(),
            )
        self.assertTrue(experienced.userdata.is_experienced)
        auction = Auction.objects.filter(created_by=experienced).first()
        self.assertTrue(self._form(auction=auction, user=experienced).advanced_open)

    def test_the_edit_page_renders_the_disclosure(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('class="auction-advanced', page)
        self.assertIn("Advanced settings", page)
        for name in ("id_tax", "id_minimum_bid", "id_date_start"):
            self.assertIn(name, page)
