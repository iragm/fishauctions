"""Regression tests for the auction stats charts, the lot-browsing endpoints, the superuser dashboards and the
per-user and per-lot admin charts: who is refused, what a permitted request returns on the shared fixture and on
an auction with nothing in it, and the ``?compare=`` guard that keeps another auction's cached stats out of a
chart unless the viewer administers that auction too.
"""

import datetime
import html
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    AdCampaign,
    AdCampaignResponse,
    Auction,
    AuctionTOS,
    Bid,
    Category,
    Club,
    ClubMember,
    Lot,
    PageView,
    PickupLocation,
    UserIgnoreCategory,
)
from auctions.tests import StandardTestCase

LINE_CHARTS = {
    "auction_stats_activity": "activity",
    "auction_stats_attrition": "attrition",
    "auction_stats_auctioneer": "auctioneer_speed",
}
BAR_CHARTS = {
    "auction_stats_pictures": "images",
    "auction_stats_distance_traveled": "travel_distance",
    "auction_stats_previous_auctions": "previous_auctions",
    "auction_stats_lots_submitted": "lots_submitted",
    "auction_stats_location_volume": "location_volume",
    "auction_stats_feature_use": "feature_use",
    "auction_stats_referrers": "referrers",
    "auction_sell_prices": "lot_sell_prices",
}
PLAIN_CHARTS = ("auction_funnel_chart", "auction_lot_bidders", "auction_lot_categories")
ALL_STATS = (*LINE_CHARTS, *BAR_CHARTS, *PLAIN_CHARTS)


def fake_cached_stats(tag):
    """One dataset per chart, labelled with ``tag`` so a test can tell whose stats ended up in a response."""
    stats = {
        key: {"labels": ["a"], "providers": [f"{tag} series"], "data": [[7]]}
        for key in (*LINE_CHARTS.values(), *BAR_CHARTS.values())
    }
    return stats


class AuctionStatsChartTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.client.raise_request_exception = False
        self.online_auction.make_stats_public = True
        self.online_auction.save()
        self.empty_auction = Auction.objects.create(
            created_by=self.user,
            title="Nothing happened here",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=2),
        )

    def url(self, name, auction=None, **params):
        url = reverse(name, kwargs={"slug": (auction or self.online_auction).slug})
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return url

    def assertSaneShape(self, name, data):
        if name in PLAIN_CHARTS:
            self.assertIn("labels", data)
            return
        self.assertIn("labels", data)
        self.assertIsInstance(data["datasets"], list)
        self.assertTrue(data["datasets"], name)
        for dataset in data["datasets"]:
            self.assertIn("data", dataset)
            self.assertIn("label", dataset)

    def test_auction_admin_gets_json_from_every_chart(self):
        self.client.force_login(self.admin_user)
        for name in ALL_STATS:
            with self.subTest(name):
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, 200)
                self.assertSaneShape(name, response.json())

    def test_every_chart_but_feature_use_answers_for_an_auction_with_no_lots_or_people(self):
        self.client.force_login(self.user)
        for name in ALL_STATS:
            if name == "auction_stats_feature_use":
                continue
            with self.subTest(name):
                response = self.client.get(self.url(name, self.empty_auction))
                self.assertEqual(response.status_code, 200)
                self.assertSaneShape(name, response.json())

    # Fallback in AuctionStatsLocationFeatureUseJSONView divides by the count of participants with accounts
    # unguarded (watch/notification/proxy/chat percents); Auction.set_stat_feature_use guards the same maths.
    def test_feature_use_answers_for_an_auction_with_no_participants(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url("auction_stats_feature_use", self.empty_auction))
        self.assertEqual(response.status_code, 200)

    def test_participant_who_is_not_an_admin_is_refused_even_when_stats_are_public(self):
        self.client.force_login(self.userB)
        for name in ALL_STATS:
            with self.subTest(name):
                response = self.client.get(self.url(name))
                if name in BAR_CHARTS:
                    self.assertEqual(response.status_code, 403)
                else:
                    self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_unknown_or_deleted_auction_is_404(self):
        self.client.force_login(self.user)
        self.empty_auction.delete()
        for name in ALL_STATS:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, kwargs={"slug": "no-such-auction"})).status_code, 404)
                self.assertEqual(self.client.get(self.url(name, self.empty_auction)).status_code, 404)

    def test_compare_with_someone_elses_auction_is_ignored(self):
        stranger = User.objects.create_user(username="stats_stranger", password="x")
        theirs = Auction.objects.create(
            created_by=stranger,
            title="Not yours",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=timezone.now() - datetime.timedelta(days=2),
            cached_stats=fake_cached_stats("SECRET"),
        )
        self.client.force_login(self.user)
        for name in (*LINE_CHARTS, *BAR_CHARTS):
            with self.subTest(name):
                response = self.client.get(self.url(name, compare=theirs.slug))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("SECRET", response.content.decode())

    def test_compare_with_an_auction_you_run_adds_its_series(self):
        Auction.objects.filter(pk=self.in_person_auction.pk).update(cached_stats=fake_cached_stats("MINE"))
        self.client.force_login(self.user)
        for name in (*LINE_CHARTS, *BAR_CHARTS):
            with self.subTest(name):
                response = self.client.get(self.url(name, compare=self.in_person_auction.slug))
                self.assertEqual(response.status_code, 200)
                labels = [d.get("label") for d in response.json()["datasets"]]
                self.assertIn(f"MINE series ({self.in_person_auction.title})", labels)

    def test_compare_with_unknown_slug_is_ignored(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url("auction_stats_pictures", compare="does-not-exist"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["datasets"]), 3)

    def test_cached_stats_are_served_instead_of_recomputed(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(cached_stats=fake_cached_stats("CACHED"))
        self.client.force_login(self.user)
        data = self.client.get(self.url("auction_stats_referrers")).json()
        self.assertEqual([d["label"] for d in data["datasets"]], ["CACHED series"])
        self.assertEqual(data["datasets"][0]["data"], [7])

    def test_funnel_and_bidder_counts(self):
        Bid.objects.create(user=self.userB, lot_number=self.lot, amount=5)
        Bid.objects.create(user=self.user_with_no_lots, lot_number=self.lot, amount=6)
        self.client.force_login(self.user)
        funnel = self.client.get(self.url("auction_funnel_chart")).json()
        self.assertEqual(funnel["data"][2], 2)
        self.assertEqual(funnel["data"][3], 1)
        bidders = self.client.get(self.url("auction_lot_bidders")).json()["data"]
        # The unsold lot; lot with two bidders; two sold lots with no bids count as one bidder each.
        self.assertEqual(bidders, [1, 2, 1, 0, 0, 0, 0])

    def test_categories_chart_reports_share_of_lots(self):
        category = Category.objects.create(name="Stats test category")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=category)
        self.client.force_login(self.user)
        data = self.client.get(self.url("auction_lot_categories")).json()
        # Volumes are Decimals, so they arrive as strings.
        volumes = [float(v) for v in data["volumes"]]
        shown = dict(zip(data["labels"], zip(data["lots"], volumes, strict=True), strict=True))
        # One of the four lots and a third of the money.
        self.assertEqual(shown["Stats test category"], (25.0, round(10 / 30 * 100, 2)))


class RecommendedLotsTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open auction for recommendations",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
            promote_this_auction=True,
        )
        location = PickupLocation.objects.create(
            name="rec location", auction=self.open_auction, pickup_time=now + datetime.timedelta(days=4)
        )
        seller = AuctionTOS.objects.create(user=self.user, auction=self.open_auction, pickup_location=location)
        self.lots = [
            Lot.objects.create(
                lot_name=f"Recommendable {i}",
                auction=self.open_auction,
                auctiontos_seller=seller,
                quantity=1,
                active=True,
            )
            for i in range(3)
        ]
        Lot.objects.filter(pk__in=[lot.pk for lot in self.lots]).update(date_posted=now - datetime.timedelta(days=1))
        self.url = "/api/lots/get_recommended/"

    def test_anonymous_visitor_gets_lots_from_the_auction(self):
        response = self.client.get(self.url, {"auction": self.open_auction.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({lot.pk for lot in response.context["object_list"]}, {lot.pk for lot in self.lots})

    def test_qty_and_exclude(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(
            self.url, {"auction": self.open_auction.slug, "qty": "1", "exclude": self.lots[0].pk}
        )
        pks = [lot.pk for lot in response.context["object_list"]]
        self.assertEqual(len(pks), 1)
        self.assertNotIn(self.lots[0].pk, pks)

    def test_junk_qty_and_exclude_fall_back_to_defaults(self):
        response = self.client.get(self.url, {"auction": self.open_auction.slug, "qty": "lots", "exclude": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["object_list"]), 3)

    def test_list_view_preference_picks_the_list_template(self):
        self.user_with_no_lots.userdata.use_list_view = True
        self.user_with_no_lots.userdata.save()
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(self.url, {"auction": self.open_auction.slug})
        self.assertTemplateUsed(response, "lot_list_page.html")


class NoLotAuctionsTests(StandardTestCase):
    url = "/api/lots/new_lot_last_auction/"

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.open_auction = Auction.objects.create(
            created_by=self.user,
            title="Accepting lots",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
            lot_submission_start_date=now - datetime.timedelta(days=1),
            lot_submission_end_date=now + datetime.timedelta(days=2),
        )
        self.location = PickupLocation.objects.create(
            name="nl location", auction=self.open_auction, pickup_time=now + datetime.timedelta(days=4)
        )

    def use_auction(self, auction):
        self.user.userdata.last_auction_used = auction
        self.user.userdata.save()
        self.client.force_login(self.user)

    def result(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        return html.unescape(response.json()["result"])

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_no_last_auction_is_empty(self):
        self.use_auction(None)
        self.assertEqual(self.result(), "")

    def test_open_auction_is_empty(self):
        self.use_auction(self.open_auction)
        self.assertEqual(self.result(), "")

    def test_ended_auction(self):
        self.use_auction(self.online_auction)
        self.assertEqual(self.result(), f"{self.online_auction} has ended<br>")

    def test_selling_not_allowed(self):
        AuctionTOS.objects.create(
            user=self.user, auction=self.open_auction, pickup_location=self.location, selling_allowed=False
        )
        self.use_auction(self.open_auction)
        self.assertIn("You don't have permission to add lots", self.result())

    def test_lot_limit_counts_only_non_donations_when_donations_are_extra(self):
        Auction.objects.filter(pk=self.open_auction.pk).update(
            max_lots_per_user=3, allow_additional_lots_as_donation=True
        )
        tos = AuctionTOS.objects.create(user=self.user, auction=self.open_auction, pickup_location=self.location)
        for donation in (False, True):
            Lot.objects.create(
                lot_name="mine",
                user=self.user,
                auction=self.open_auction,
                auctiontos_seller=tos,
                quantity=1,
                donation=donation,
            )
        self.use_auction(Auction.objects.get(pk=self.open_auction.pk))
        self.assertEqual(self.result(), f"You've added 1 of 3 lots to {self.open_auction}<br>")


class AuctionNotificationsTests(StandardTestCase):
    url = "/api/users/auction_notifications/"

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.nearby = Auction.objects.create(
            created_by=self.user,
            title="Nearby auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
            promote_this_auction=True,
        )
        PickupLocation.objects.create(
            name="near",
            auction=self.nearby,
            pickup_time=now + datetime.timedelta(days=4),
            latitude=40.0,
            longitude=-75.0,
        )
        userdata = self.user_with_no_lots.userdata
        userdata.latitude = 40.0
        userdata.longitude = -75.0
        userdata.save()

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_nearby_auction_is_reported(self):
        self.client.force_login(self.user_with_no_lots)
        data = self.client.post(self.url).json()
        self.assertEqual(data["new"], 1)
        self.assertEqual(data["slug"], self.nearby.slug)
        self.assertEqual(data["distance_unit"], "miles")

    def test_already_joined_auction_is_not_reported(self):
        AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.nearby, pickup_location=self.nearby.location_qs.first()
        )
        self.client.force_login(self.user_with_no_lots)
        data = self.client.post(self.url).json()
        self.assertEqual(data["new"], "")
        self.assertEqual(data["slug"], "")

    def test_no_location_reports_nothing_in_the_users_unit(self):
        self.userB.userdata.distance_unit = "km"
        self.userB.userdata.save()
        self.client.force_login(self.userB)
        data = self.client.post(self.url).json()
        self.assertEqual(data["new"], "")
        self.assertEqual(data["distance_unit"], "km")
        self.assertEqual(data["distance"], round(self.userB.userdata.email_me_about_new_auctions_distance * 1.60934))


class LotListViewTests(StandardTestCase):
    """Through LotsByUser, the plainest LotListView subclass."""

    url = "/lots/user/"

    def test_anonymous_listing(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["lotsAreHidden"], -1)
        self.assertTrue(response.context["display_auction_on_lots"])
        self.assertIsNone(response.context["page_view_auction"])

    def test_auction_filter_sets_the_auction_and_the_users_tos(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"auction": self.online_auction.slug, "user": self.user.username})
        self.assertEqual(response.context["auction"], self.online_auction)
        self.assertEqual(response.context["auction_tos"], self.online_tos)
        self.assertEqual(response.context["page_view_auction"], self.online_auction.pk)
        self.assertEqual(response.context["user"], self.user)

    def test_unknown_auction_and_user_fall_back_quietly(self):
        response = self.client.get(self.url, {"auction": "no-such-auction", "user": "nobody-at-all"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["auction"])
        self.assertTrue(response.context["no_filters"])
        self.assertIsNone(response.context["user"])

    def test_hidden_category_count(self):
        UserIgnoreCategory.objects.create(user=self.user, category=Category.objects.create(name="Hidden"))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).context["lotsAreHidden"], 1)


class RenderAdTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.campaign = AdCampaign.objects.create(
            title="Buy fish",
            external_url="https://example.com/",
            begin_date=timezone.now() - datetime.timedelta(days=1),
        )

    def fetch(self, *rolls, **params):
        with patch("auctions.views.browse.uniform", side_effect=list(rolls)):
            return self.client.get(reverse("get_ad"), params)

    def test_house_ad_records_an_impression(self):
        self.client.force_login(self.user)
        response = self.fetch(99, 0)
        self.assertEqual(response.status_code, 200)
        impression = AdCampaignResponse.objects.get(campaign=self.campaign)
        self.assertEqual(impression.user, self.user)
        self.assertEqual(response.context["object"], impression)

    def test_google_roll_records_nothing(self):
        response = self.fetch(0)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AdCampaignResponse.objects.exists())

    def test_campaign_for_another_auction_is_not_shown_on_this_one(self):
        self.campaign.auction = self.in_person_auction
        self.campaign.save()
        self.fetch(99, 0, auction=self.online_auction.slug)
        self.assertFalse(AdCampaignResponse.objects.exists())

    # RenderAd only narrows by auction when ?auction= is given, so a campaign the help text says runs
    # "only on a particular auction" is shown on every page without one.
    def test_auction_only_campaign_is_not_shown_site_wide(self):
        self.campaign.auction = self.in_person_auction
        self.campaign.save()
        self.fetch(99, 0)
        self.assertFalse(AdCampaignResponse.objects.exists())

    # The limit check is `number_of_impressions > max_ads`, so a campaign is shown max_ads + 1 times.
    def test_campaign_stops_at_max_ads(self):
        self.campaign.max_ads = 1
        self.campaign.save()
        AdCampaignResponse.objects.create(campaign=self.campaign)
        self.fetch(99, 0)
        self.assertEqual(AdCampaignResponse.objects.filter(campaign=self.campaign).count(), 1)


class ClubMemberMergeAutocompleteTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Merge Autocomplete Club")
        self.editor = ClubMember.objects.create(club=self.club, user=self.user, name="Editor", permission_add_edit=True)
        ClubMember.objects.create(club=self.club, user=self.userB, name="Viewer", permission_view=True)
        self.active = ClubMember.objects.create(club=self.club, name="Alice Active", email="alice@example.com")
        self.gone = ClubMember.objects.create(club=self.club, name="Alice Gone", is_deleted=True)
        other = Club.objects.create(name="Some Other Club")
        ClubMember.objects.create(club=other, name="Alice Elsewhere")

    def results(self, q="Alice", **forward):
        forward.setdefault("club_slug", self.club.slug)
        response = self.client.get(reverse("club-member-merge-autocomplete"), {"q": q, "forward": json.dumps(forward)})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("club-member-merge-autocomplete"))
        self.assertEqual(response.status_code, 302)

    def test_member_without_add_edit_gets_nothing(self):
        self.client.force_login(self.userB)
        self.assertEqual(self.results(), [])

    def test_no_club_gets_nothing(self):
        self.client.force_login(self.user)
        self.assertEqual(self.results(club_slug=""), [])

    def test_editor_sees_this_clubs_members_including_deactivated(self):
        self.client.force_login(self.user)
        results = self.results()
        self.assertEqual([r["id"] for r in results], [str(self.active.pk), str(self.gone.pk)])
        self.assertIn("(alice@example.com)", results[0]["text"])
        self.assertIn("(Deactivated)", results[1]["text"])

    def test_excluded_member_is_left_out_and_junk_exclude_is_ignored(self):
        self.client.force_login(self.user)
        self.assertEqual([r["id"] for r in self.results(exclude_member=self.active.pk)], [str(self.gone.pk)])
        self.assertEqual(len(self.results(exclude_member="abc")), 2)


class SiteAdminDashboardTests(StandardTestCase):
    PAGES = (
        "admin_dashboard",
        "admin_traffic",
        "admin_traffic_json",
        "admin_traffic_time_of_day_json",
        "admin_referrers",
        "admin_user_signups",
        "admin_user_signups_json",
        "admin_user_map",
    )

    def setUp(self):
        super().setUp()
        self.client.raise_request_exception = False
        self.superuser = User.objects.create_superuser("site_boss", "boss@example.com", "x")
        PageView.objects.create(user=self.user, url="/lots/", title="Lots", referrer="https://elsewhere.example/")

    def test_superuser_gets_every_dashboard(self):
        self.client.force_login(self.superuser)
        for name in self.PAGES:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_normal_user_is_redirected_home(self):
        self.client.force_login(self.user)
        for name in (*self.PAGES, "admin_error"):
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/")

    def test_auction_admin_is_not_a_site_admin(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(reverse("admin_dashboard")).status_code, 302)

    def test_junk_days_fall_back_to_defaults(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("admin_traffic"), {"days": "x"}).context["days"], 7)
        data = self.client.get(reverse("admin_traffic_json"), {"days": "x"}).json()
        self.assertEqual(len(data["labels"]), 7)
        self.assertEqual(len(data["datasets"][0]["data"]), 7)
        grid = self.client.get(reverse("admin_traffic_time_of_day_json"), {"days": "x"}).json()["datasets"]
        self.assertEqual(len(grid), 7)
        self.assertEqual(sum(sum(day["data"]) for day in grid), 1)

    # AdminTrafficJSON passes ?days straight to bin_data as the bin count, which divides by it.
    def test_zero_days_does_not_crash_the_traffic_chart(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("admin_traffic_json"), {"days": "0"}).status_code, 200)

    def test_signups_are_cumulative(self):
        self.client.force_login(self.superuser)
        data = self.client.get(reverse("admin_user_signups_json"), {"days": "3"}).json()
        totals = data["datasets"][0]["data"]
        self.assertEqual(len(totals), 4)
        self.assertEqual(totals[-1], User.objects.count())
        self.assertEqual(totals, sorted(totals))

    def test_user_map_filters(self):
        self.user.userdata.latitude = 40
        self.user.userdata.longitude = -75
        self.user.userdata.save()
        self.client.force_login(self.superuser)
        users = self.client.get(reverse("admin_user_map")).context["users"]
        self.assertEqual(list(users), [self.user])
        users = self.client.get(reverse("admin_user_map"), {"view": "buyers_and_sellers", "filter": "5"})
        self.assertEqual(list(users.context["users"]), [])
        response = self.client.get(reverse("admin_user_map"), {"view": "recent", "filter": "junk"})
        self.assertEqual(response.status_code, 200)

    def test_error_page_raises_for_superuser(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("admin_error")).status_code, 500)


class UserAndLotChartTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser("chart_boss", "chartboss@example.com", "x")
        self.fish = Category.objects.create(name="Chart fish")
        self.plants = Category.objects.create(name="Chart plants")
        Lot.objects.filter(pk=self.lot.pk).update(species_category=self.fish)
        Lot.objects.filter(pk=self.lotB.pk).update(species_category=self.plants)

    def test_non_superusers_are_refused(self):
        for url in (f"/api/chart/users/{self.userB.pk}/", f"/api/chart/lots/{self.lot.pk}/"):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.client.force_login(self.user)
                self.assertEqual(self.client.get(url).status_code, 403)
                self.client.logout()

    def test_user_chart_counts_bids_and_views_per_category_most_viewed_first(self):
        Bid.objects.create(user=self.userB, lot_number=self.lot, amount=5)
        PageView.objects.create(user=self.userB, lot_number=self.lotB)
        PageView.objects.create(user=self.userB, lot_number=self.lotB)
        self.client.force_login(self.superuser)
        data = self.client.get(f"/api/chart/users/{self.userB.pk}/").json()
        self.assertEqual(data, {"labels": ["Chart plants", "Chart fish"], "bids": [0, 1], "views": [2, 0]})

    def test_user_chart_for_unknown_user_is_empty(self):
        self.client.force_login(self.superuser)
        data = self.client.get("/api/chart/users/999999/").json()
        self.assertEqual(data, {"labels": [], "bids": [], "views": []})

    def test_lot_chart_lists_signed_in_viewers_by_time(self):
        PageView.objects.create(user=self.userB, lot_number=self.lot, total_time=30)
        PageView.objects.create(user=self.user, lot_number=self.lot, total_time=90)
        PageView.objects.create(user=None, lot_number=self.lot, total_time=500)
        self.client.force_login(self.superuser)
        data = self.client.get(f"/api/chart/lots/{self.lot.pk}/").json()
        self.assertEqual(data, {"labels": [str(self.user), str(self.userB)], "data": [90, 30]})
