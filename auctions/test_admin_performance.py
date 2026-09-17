"""The admin's own query counts: no dropdown over an unbounded table, and no query per inline row.

Both halves of `auctions/admin_performance.py` are invisible when they break -- delete the rule and
every page still renders, the change page for one pickup location just lists every `AuctionTOS` on
the site again. So there is a structural test walking every admin form, and growth tests that add
inline rows and assert the page doesn't get more expensive.
"""

from django.apps import apps
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import connection
from django.forms.models import ModelChoiceField, ModelMultipleChoiceField
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from auctions.admin_performance import BOUNDED_TABLES, every_admin, use_lookup_widgets
from auctions.models import (
    AdCampaign,
    AdCampaignGroup,
    AdCampaignResponse,
    Bid,
    Club,
    ClubMember,
    Invoice,
    Lot,
    Species,
    SpeciesCommonName,
    UserData,
    Watch,
)
from auctions.tests import StandardTestCase


def _widget_of(field):
    """The real widget: the admin wraps every relation field in a `RelatedFieldWidgetWrapper`, so the class
    on `field.widget` says nothing about whether it renders a dropdown.
    """
    return getattr(field.widget, "widget", field.widget)


def _relation_fields(form):
    for field_name, field in form.base_fields.items():
        if isinstance(field, ModelChoiceField | ModelMultipleChoiceField):
            yield field_name, field


def _our_forms():
    """Every form the admin renders that we are responsible for, as (name, form). Third-party admins are
    skipped, as the rule itself skips them.
    """
    request = RequestFactory().get("/")
    request.user = User(is_superuser=True, is_staff=True, is_active=True)
    for model, model_admin in django_admin.site._registry.items():
        owners = [(type(model_admin), lambda a=model_admin: a.get_form(request))]
        owners += [
            (inline, lambda c=inline: c(model, django_admin.site).get_formset(request).form)
            for inline in model_admin.inlines
        ]
        for owner, build in owners:
            # Filter before building: post_office's EmailAdmin raises on a form with no instance.
            if owner.__module__.split(".")[0] == "auctions":
                yield owner.__name__, build()


class NoDropdownOverAnUnboundedTableTests(TestCase):
    def test_every_relation_the_admin_shows_is_a_search_box_or_a_short_list(self):
        """The rule asserted from the other end: what the rendered forms actually got.

        A new foreign key or `ModelAdmin` is covered the moment it exists, which is the point of applying
        this to the registry.
        """
        dropdowns = []
        for name, form in _our_forms():
            for field_name, field in _relation_fields(form):
                # A search box renders only what is selected; these are the ones Django ships.
                if type(_widget_of(field)).__name__.startswith(("Autocomplete", "ForeignKeyRawId", "ManyToManyRawId")):
                    continue
                label = field.queryset.model._meta.label
                if label not in BOUNDED_TABLES:
                    dropdowns.append(f"{name}.{field_name} -> {label}")
        self.assertEqual(
            dropdowns,
            [],
            "these render one <option> per row of a table that grows with the site; either the "
            "target belongs in BOUNDED_TABLES because it cannot grow, or admin_performance.py is "
            f"no longer reaching them: {dropdowns}",
        )

    def test_bounded_tables_all_name_a_real_model(self):
        """A typo in that set is otherwise silent: it just never matches anything."""
        labels = {model._meta.label for model in apps.get_models()}
        self.assertEqual(sorted(BOUNDED_TABLES - labels), [])

    def test_every_model_something_autocompletes_to_has_an_order(self):
        """Every model something autocompletes to has an order.

        An autocomplete paginates, and paginating an unordered queryset lies: page 2 can repeat or skip a
        row, which Django reports as an `UnorderedObjectListWarning` from inside the view, where nobody sees
        it.
        """
        unordered = set()
        for name, model_admin, _target in every_admin(django_admin.site):
            if type(model_admin).__module__.split(".")[0] != "auctions":
                continue
            for field_name in model_admin.autocomplete_fields:
                target = model_admin.model._meta.get_field(field_name).related_model
                target_admin = django_admin.site._registry.get(target)
                if not target._meta.ordering and not (target_admin and target_admin.ordering):
                    unordered.add(f"{target._meta.label} (reached from {name}.{field_name})")
        self.assertEqual(
            sorted(unordered),
            [],
            "these are autocompleted to but have neither Meta.ordering nor ModelAdmin.ordering, so "
            f"the picker's second page is not reliable: {sorted(unordered)}",
        )

    def test_applying_the_rule_again_changes_nothing(self):
        """It runs at import, so a second call must not append a field twice or fight a declaration."""
        self.assertEqual(use_lookup_widgets(django_admin.site), {})


class AdminChangePageGrowthTests(StandardTestCase):
    """An inline row must not cost a query; what it costs is declared on the inline as `FlatInline`."""

    def setUp(self):
        super().setUp()
        self.admin_user.is_superuser = True
        self.admin_user.is_staff = True
        self.admin_user.save()
        self.client = Client()
        self.client.force_login(self.admin_user)

    def assert_flat(self, url, make_rows, extra=4):
        """Load `url` with N rows and then N+extra, and assert the cost didn't move."""
        make_rows(extra)
        self.client.get(url)
        with CaptureQueriesContext(connection) as before:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        make_rows(extra)
        with CaptureQueriesContext(connection) as after:
            self.assertEqual(self.client.get(url).status_code, 200)
        added = len(after.captured_queries) - len(before.captured_queries)
        self.assertEqual(
            added,
            0,
            f"{url} cost {added} more queries for {extra} more rows -- something on the row is "
            "reading one at a time. Fix it where the page builds its queryset, not here.",
        )

    def _next_username(self, prefix):
        return f"{prefix}{User.objects.count()}"

    def test_the_lot_change_page_does_not_query_per_bid_or_watcher(self):
        def make_rows(count):
            for _ in range(count):
                user = User.objects.create_user(username=self._next_username("bidder"))
                Bid.objects.create(user=user, lot_number=self.lot, amount=5)
                Watch.objects.create(user=user, lot_number=self.lot)

        self.assert_flat(reverse("admin:auctions_lot_change", args=[self.lot.pk]), make_rows)

    def test_the_pickup_location_change_page_does_not_query_per_person(self):
        from auctions.models import AuctionTOS

        def make_rows(count):
            for _ in range(count):
                AuctionTOS.objects.create(
                    user=User.objects.create_user(username=self._next_username("attendee")),
                    auction=self.online_auction,
                    pickup_location=self.location,
                )

        self.assert_flat(reverse("admin:auctions_pickuplocation_change", args=[self.location.pk]), make_rows)

    def test_the_invoice_change_page_does_not_query_per_lot(self):
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)

        def make_rows(count):
            for _ in range(count):
                Lot.objects.create(
                    lot_name=f"invoice lot {Lot.objects.count()}",
                    auction=self.online_auction,
                    auctiontos_seller=self.online_tos,
                    quantity=1,
                    winning_price=10,
                    active=False,
                    seller_invoice=invoice,
                )

        self.assert_flat(reverse("admin:auctions_invoice_change", args=[invoice.pk]), make_rows)

    def test_the_club_change_page_does_not_query_per_member(self):
        club = Club.objects.create(name="Query count club")

        def make_rows(count):
            for _ in range(count):
                user = User.objects.create_user(username=self._next_username("member"))
                UserData.objects.filter(user=user).update(club=club)
                ClubMember.objects.create(club=club, user=user)

        self.assert_flat(reverse("admin:auctions_club_change", args=[club.pk]), make_rows)

    def test_the_species_change_page_does_not_query_per_common_name(self):
        species = Species.objects.create(common_name="Query fish", scientific_name="Queryus fishus")

        def make_rows(count):
            for _ in range(count):
                SpeciesCommonName.objects.create(species=species, name=f"name {SpeciesCommonName.objects.count()}")

        self.assert_flat(reverse("admin:auctions_species_change", args=[species.pk]), make_rows)

    def test_the_ad_campaign_group_page_does_not_query_per_campaign(self):
        """Four queries a row: a category dropdown, the group's name, and `__str__`'s click rate.

        The click rate is the one worth naming, because nothing on the page asks for it: each row's heading
        is `AdCampaign.__str__`, which prints it, so two `COUNT`s over every ad ever shown paid for a string.
        """
        group = AdCampaignGroup.objects.create(title="Query count group")

        def make_rows(count):
            for _ in range(count):
                AdCampaign.objects.create(campaign_group=group, title=f"ad {AdCampaign.objects.count()}")

        self.assert_flat(reverse("admin:auctions_adcampaigngroup_change", args=[group.pk]), make_rows)

    def test_the_ad_changelists_do_not_query_per_row(self):
        """Both changelists print counts over `AdCampaignResponse` per row, and that table holds a row per ad
        ever shown.
        """
        group = AdCampaignGroup.objects.create(title="Changelist group")

        def make_campaigns(count):
            for _ in range(count):
                campaign = AdCampaign.objects.create(
                    campaign_group=group, title=f"listed ad {AdCampaign.objects.count()}"
                )
                AdCampaignResponse.objects.create(
                    campaign=campaign,
                    user=User.objects.create_user(username=self._next_username("adviewer")),
                    clicked=True,
                )

        self.assert_flat(reverse("admin:auctions_adcampaign_changelist"), make_campaigns)

        def make_groups(count):
            for _ in range(count):
                other = AdCampaignGroup.objects.create(title=f"listed group {AdCampaignGroup.objects.count()}")
                campaign = AdCampaign.objects.create(campaign_group=other, title=f"in {other.title}")
                AdCampaignResponse.objects.create(
                    campaign=campaign,
                    user=User.objects.create_user(username=self._next_username("groupviewer")),
                    clicked=False,
                )

        self.assert_flat(reverse("admin:auctions_adcampaigngroup_changelist"), make_groups)

    def test_the_annotated_group_totals_are_the_same_numbers_the_properties_give(self):
        """The annotated group totals match the properties.

        Two multi-valued joins in one query is where `Count` would multiply -- campaigns x responses -- so
        this counts real rows: two campaigns, three responses, two clicks.
        """
        group = AdCampaignGroup.objects.create(title="Totalled group")
        first = AdCampaign.objects.create(campaign_group=group, title="first")
        second = AdCampaign.objects.create(campaign_group=group, title="second")
        for index, (campaign, clicked) in enumerate([(first, True), (first, False), (second, True)]):
            AdCampaignResponse.objects.create(
                campaign=campaign,
                user=User.objects.create_user(username=f"totalviewer{index}"),
                clicked=clicked,
            )

        plain = AdCampaignGroup.objects.get(pk=group.pk)
        self.assertEqual((plain.number_of_campaigns, plain.number_of_impressions, plain.number_of_clicks), (2, 3, 2))

        annotated = AdCampaignGroup.annotate_totals(AdCampaignGroup.objects.all()).get(pk=group.pk)
        self.assertEqual(
            (annotated.number_of_campaigns, annotated.number_of_impressions, annotated.number_of_clicks),
            (2, 3, 2),
        )
        self.assertEqual(annotated.click_rate, plain.click_rate)

        empty = AdCampaignGroup.annotate_totals(AdCampaignGroup.objects.all()).get(
            pk=AdCampaignGroup.objects.create(title="Nothing yet").pk
        )
        self.assertEqual((empty.number_of_campaigns, empty.number_of_impressions, empty.number_of_clicks), (0, 0, 0))

    def test_the_annotated_ad_counts_are_the_same_numbers_the_properties_give(self):
        """The annotated ad counts match the properties.

        Two aggregates over one join is where that goes wrong, so this counts three impressions, two clicks,
        and a second campaign to prove the join isn't mixing them.
        """
        group = AdCampaignGroup.objects.create(title="Counted group")
        campaign = AdCampaign.objects.create(campaign_group=group, title="counted")
        other = AdCampaign.objects.create(campaign_group=group, title="not counted")
        for index, clicked in enumerate([True, True, False]):
            AdCampaignResponse.objects.create(
                campaign=campaign,
                user=User.objects.create_user(username=f"viewer{index}"),
                clicked=clicked,
            )
        AdCampaignResponse.objects.create(campaign=other, user=self.user, clicked=True)

        plain = AdCampaign.objects.get(pk=campaign.pk)
        self.assertEqual((plain.number_of_impressions, plain.number_of_clicks), (3, 2))

        annotated = AdCampaign.annotate_response_counts(AdCampaign.objects.all()).get(pk=campaign.pk)
        self.assertEqual((annotated.number_of_impressions, annotated.number_of_clicks), (3, 2))
        self.assertEqual(annotated.click_rate, plain.click_rate)

        annotated_other = AdCampaign.annotate_response_counts(AdCampaign.objects.all()).get(pk=other.pk)
        self.assertEqual((annotated_other.number_of_impressions, annotated_other.number_of_clicks), (1, 1))

    def test_a_lookup_widget_still_reads_back_what_it_renders(self):
        """A lookup widget still reads back what it renders.

        `value_from_datadict` is the widget's own reading of the POST -- the step a dropdown and a search box
        could disagree on -- and `clean` turns that into the saved object. Both kinds are exercised over
        `auth.User`.
        """
        checked = {}
        for name, form in _our_forms():
            for field_name, field in _relation_fields(form):
                widget = _widget_of(field)
                kind = type(widget).__name__
                if not kind.startswith(("Autocomplete", "ForeignKeyRawId")) or kind in checked:
                    continue
                if field.queryset.model is not User:
                    continue
                checked[kind] = f"{name}.{field_name}"
                posted = field.widget.value_from_datadict({field_name: str(self.admin_user.pk)}, {}, field_name)
                self.assertEqual(posted, str(self.admin_user.pk), f"{name}.{field_name} misread its POST")
                self.assertEqual(field.clean(posted), self.admin_user, f"{name}.{field_name}")
        self.assertEqual(
            sorted(checked),
            ["AutocompleteSelect", "ForeignKeyRawIdWidget"],
            f"expected both kinds of lookup widget over auth.User, found {checked}",
        )
