"""The superuser's dashboard: traffic, signups, referrers, the user flow map.

Charts and maps over the whole site rather than over one auction. The setup checklist that the
admin landing page is built around is next door in :mod:`auctions.views.admin_checklist`.
"""

import collections
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from chartjs.views.lines import BaseLineChartView
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import (
    Count,
    Q,
)
from django.db.models.base import Model as Model
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDay
from django.http import (
    JsonResponse,
)
from django.shortcuts import redirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView

from auctions.helper_functions import bin_data
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    Lot,
    MobileDevice,
    PageView,
    UserData,
)

from .base import AdminOnlyViewMixin

logger = logging.getLogger(__name__)


class AdminErrorPage(AdminOnlyViewMixin, TemplateView):
    """A sanity check to make sure the 500 error emails are working as they should be"""

    template_name = "dashboard.html"

    def get(self, request, *args, **kwargs):
        return 1 / 0


class AdminTraffic(AdminOnlyViewMixin, TemplateView):
    """Popular pages and user last activity"""

    template_name = "dashboard_traffic.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        context["days"] = days
        timeframe = timezone.now() - timedelta(days=days)

        # this next section is the user last activity
        # this is very old code, and it would probably be far better to use PageViews
        # for logged in and not logged in users instead to show overall traffic over time
        qs = UserData.objects.filter(user__is_active=True)
        activity = (
            qs.filter(last_activity__gte=timezone.now() - timedelta(days=60))
            .annotate(day=TruncDay("last_activity"))
            .order_by("-day")
            .values("day")
            .annotate(c=Count("pk"))
            .values("day", "c")
        )
        context["last_activity_days"] = []
        context["last_activity_count"] = []
        for day in activity:
            context["last_activity_days"].append((timezone.now() - day["day"]).days)
            context["last_activity_count"].append(day["c"])
        # popular page stuff follows
        page_view_qs = PageView.objects.filter(date_start__gte=timeframe)
        # may want to move this to a get param at some point
        number_of_popular_pages_to_show = 50
        context["page_views"] = (
            page_view_qs.values("url", "title")
            .annotate(
                # there's no way this code is right,
                # it dates back to when view counter was being used, and that field is no longer filled out
                # total_view_count=Sum("counter") + F("unique_view_count"),
                view_count=Count("url"),
            )
            .order_by("-view_count")[:number_of_popular_pages_to_show]
        )
        # Top user agents over the last 24 hours, to help spot and filter out bots
        last_24_hours = timezone.now() - timedelta(hours=24)
        context["top_user_agents"] = list(
            PageView.objects.filter(date_start__gte=last_24_hours)
            .exclude(user_agent__isnull=True)
            .exclude(user_agent="")
            .values("user_agent")
            .annotate(view_count=Count("pk"))
            .order_by("-view_count")[:5]
        )
        # heat  map stuff follows
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["pageviews"] = PageView.objects.exclude(latitude=0).filter(date_start__gte=timeframe)
        return context


class AdminTrafficJSON(AdminOnlyViewMixin, BaseLineChartView):
    """JSON userdata"""

    def dispatch(self, request, *args, **kwargs):
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        self.bins = days
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        return [(f"{i - 1} days ago") for i in range(self.bins, 0, -1)][::-1]

    def get_providers(self):
        return ["Views"]

    def get_data(self):
        timeframe = timezone.now() - timedelta(days=self.bins)
        views = PageView.objects.filter(date_start__gte=timeframe).order_by("-date_start")

        # what follows is a delightful reminder of how important a consistent naming scheme is
        return [
            bin_data(views, "date_start", self.bins, timeframe, timezone.now())[::-1],
        ]


class AdminTrafficTimeOfDayJSON(AdminOnlyViewMixin, BaseLineChartView):
    """Page views binned by hour and day of week"""

    def dispatch(self, request, *args, **kwargs):
        days_param = self.request.GET.get("days", 30)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 30
        self.bins = days
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        return [f"{h}:00" for h in range(24)]

    def get_providers(self):
        return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def get_data(self):
        timeframe = timezone.now() - timedelta(days=self.bins)
        counts = (
            PageView.objects.filter(date_start__gte=timeframe)
            .annotate(hour=ExtractHour("date_start", tzinfo=timezone.get_current_timezone()))
            .annotate(dow=ExtractIsoWeekDay("date_start", tzinfo=timezone.get_current_timezone()))
            .values("dow", "hour")
            .annotate(count=Count("pk"))
        )
        grid = [[0] * 24 for _ in range(7)]
        for row in counts:
            grid[row["dow"] - 1][row["hour"]] += row["count"]
        return grid


class AdminUserSignups(AdminOnlyViewMixin, TemplateView):
    """Cumulative user signups over time"""

    template_name = "dashboard_user_signups.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days_param = self.request.GET.get("days", "")
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = None
        context["days"] = days
        return context


class AdminUserSignupsJSON(AdminOnlyViewMixin, BaseLineChartView):
    """JSON data for cumulative user signups chart, aggregated by day"""

    def dispatch(self, request, *args, **kwargs):
        days_param = self.request.GET.get("days", "")
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = None
        self._end = timezone.now().date()
        if days:
            self._start = (timezone.now() - timedelta(days=days)).date()
        else:
            earliest = User.objects.order_by("date_joined").values_list("date_joined", flat=True).first()
            self._start = earliest.date() if earliest else self._end
        self._days = (self._end - self._start).days
        # count users that already existed before _start (cumulative offset)
        start_dt = timezone.make_aware(
            datetime.combine(self._start, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        self._stale_cutoff = timezone.now() - timedelta(days=400)
        self._initial_count = User.objects.filter(date_joined__lt=start_dt).count()
        self._initial_tos_count = (
            User.objects.filter(date_joined__lt=start_dt, auctiontos__isnull=False).distinct().count()
        )
        self._initial_won_sold_count = (
            User.objects.filter(date_joined__lt=start_dt)
            .filter(Q(winner__isnull=False) | Q(lot__winning_price__isnull=False))
            .distinct()
            .count()
        )
        self._initial_stale_count = User.objects.filter(
            date_joined__lt=start_dt, userdata__last_activity__lt=self._stale_cutoff
        ).count()
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        return [(self._start + timedelta(days=i)).strftime("%b %-d, %Y") for i in range(self._days + 1)]

    def get_providers(self):
        return ["Total users", "Joined an auction", "Won or sold a lot", "Stale (400+ days inactive)"]

    def get_data(self):
        start_dt = timezone.make_aware(
            datetime.combine(self._start, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        end_dt = timezone.make_aware(
            datetime.combine(self._end + timedelta(days=1), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        stale_cutoff = self._stale_cutoff
        base_qs = User.objects.filter(date_joined__gte=start_dt, date_joined__lt=end_dt)

        def daily_count(qs):
            return (
                qs.annotate(join_date=TruncDay("date_joined"))
                .values("join_date")
                .annotate(count=Count("pk", distinct=True))
                .order_by("join_date")
            )

        def make_cumulative(daily_qs, initial):
            date_counts = {item["join_date"].date(): item["count"] for item in daily_qs}
            cumulative = []
            running = initial
            for i in range(self._days + 1):
                running += date_counts.get(self._start + timedelta(days=i), 0)
                cumulative.append(running)
            return cumulative

        return [
            make_cumulative(daily_count(base_qs), self._initial_count),
            make_cumulative(daily_count(base_qs.filter(auctiontos__isnull=False)), self._initial_tos_count),
            make_cumulative(
                daily_count(base_qs.filter(Q(winner__isnull=False) | Q(lot__winning_price__isnull=False))),
                self._initial_won_sold_count,
            ),
            make_cumulative(
                daily_count(base_qs.filter(userdata__last_activity__lt=stale_cutoff)),
                self._initial_stale_count,
            ),
        ]


class AdminReferrers(AdminOnlyViewMixin, TemplateView):
    """Where's your traffic coming from?"""

    template_name = "dashboard_referrers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        days_param = self.request.GET.get("days", 7)
        try:
            days = int(days_param)
        except (ValueError, TypeError):
            days = 7
        context["days"] = days
        timeframe = timezone.now() - timedelta(days=days)
        page_view_qs = PageView.objects.filter(date_end__gte=timeframe)
        referrers = (
            page_view_qs.exclude(referrer__isnull=True)
            .exclude(referrer="")
            .exclude(referrer__startswith="http://127.0.0.1:8000")
        )
        # comment out next line to include internal referrers
        referrers = referrers.exclude(referrer__startswith="https://" + Site.objects.get_current().domain)
        referrers = referrers.exclude(referrer__startswith="" + Site.objects.get_current().domain)
        context["referrers"] = (
            referrers.values("referrer", "url", "title")
            .annotate(
                total_clicks=Count("referrer"),
                # total_view_count=Sum('counter') + F('unique_view_count')
            )
            .order_by("-total_clicks")[:100]
        )
        return context


class AdminDashboard(AdminOnlyViewMixin, TemplateView):
    """Currently active users overview"""

    template_name = "dashboard.html"

    def unique_page_views(self, minutes, view_type="anon"):
        timeframe = timezone.now() - timezone.timedelta(minutes=minutes)
        base_qs = PageView.objects.filter(date_start__gte=timeframe)
        if view_type == "logged_in":
            # return base_qs.filter(user__isnull=False).aggregate(unique_views=Count("user", distinct=True))[
            #    "unique_views"
            # ]
            return base_qs.filter(user__isnull=False).values("user").distinct().count()
        if view_type == "anon":
            # return base_qs.filter(user__isnull=True, session_id__isnull=False).aggregate(
            #    unique_views=Count("session_id", distinct=True)
            # )["unique_views"]
            # this one is the same as above.  Both use session which is somehow getting clobbered.  Maybe cloudflare.
            # return (
            #     base_qs.filter(user__isnull=True, session_id__isnull=False)
            #     .exclude(session_id="")
            #     .values("session_id")
            #     .distinct()
            #     .count()
            # )
            return (
                base_qs.filter(user__isnull=True)
                .exclude(ip_address="")
                .exclude(ip_address__isnull=True)
                .values("ip_address")
                .distinct()
                .count()
            )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = UserData.objects.filter(user__is_active=True)
        context["total_users"] = qs.count()

        context["verified_emails_count"] = User.objects.filter(emailaddress__verified=True).distinct().count()
        context["users_with_email_no_account_count"] = (
            AuctionTOS.objects.filter(user__isnull=True, email__isnull=False).values("email").distinct().count()
        )

        # Mobile app adoption
        app_devices = MobileDevice.objects.filter(user__is_active=True)
        mobile_app_users = app_devices.values("user").distinct().count()
        context["mobile_app_users_count"] = mobile_app_users
        context["mobile_app_users_percent"] = int(mobile_app_users / qs.count() * 100) if qs.count() else 0
        context["mobile_app_users_ios"] = (
            app_devices.filter(platform=MobileDevice.PLATFORM_IOS).values("user").distinct().count()
        )
        context["mobile_app_users_android"] = (
            app_devices.filter(platform=MobileDevice.PLATFORM_ANDROID).values("user").distinct().count()
        )
        context["mobile_app_users_active_30d"] = (
            app_devices.filter(last_seen__gte=timezone.now() - timezone.timedelta(days=30))
            .values("user")
            .distinct()
            .count()
        )
        context["mobile_app_versions"] = (
            app_devices.exclude(app_version="")
            .values("app_version")
            .annotate(count=Count("user", distinct=True))
            .order_by("-count")
        )

        # context["unsubscribes"] = qs.filter(has_unsubscribed=True).count()
        # context["anonymous"] = (
        #     qs.filter(username_visible=False).exclude(user__username__icontains="@").count()
        # )  # inactive users with an email as their username were set to anonymous Nov 2023
        # context["light_theme"] = qs.filter(use_dark_theme=False).count()
        # context["hide_ads"] = qs.filter(show_ads=False).count()
        # context["no_club_auction"] = qs.filter(user__auctiontos__isnull=True).distinct().count()
        # context["no_participate"] = (
        #     qs.exclude(Q(user__winner__isnull=False) | Q(user__lot__isnull=False)).distinct().count()
        # )
        # context["using_watch"] = qs.exclude(user__watch__isnull=True).distinct().count()
        # context["using_buy_now"] = qs.filter(user__winner__buy_now_used=True).count()
        # context["using_proxy_bidding"] = qs.filter(has_used_proxy_bidding=True).count()
        # context["buyers"] = qs.filter(user__winner__isnull=False).distinct().count()
        # context["sellers"] = qs.filter(user__lot__isnull=False).distinct().count()
        # context["has_location"] = qs.exclude(latitude=0).count()
        # context["new_lots_last_7_days"] = (
        #     Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - timedelta(days=7)).count()
        # )
        # context["new_lots_last_30_days"] = (
        #     Lot.objects.exclude(is_deleted=True).filter(date_posted__gte=timezone.now() - timedelta(days=30)).count()
        # )
        # context["bidders_last_30_days"] = (
        #     qs.filter(user__bid__last_bid_time__gte=timezone.now() - timedelta(days=30))
        #     .values("user")
        #     .distinct()
        #     .count()
        # )
        # context["feedback_last_30_days"] = (
        #     Lot.objects.exclude(feedback_rating=0).filter(date_posted__gte=timezone.now() - timedelta(days=30)).count()
        # )
        # context["users_with_search_history"] = User.objects.filter(searchhistory__isnull=False).distinct().count()
        logged_in_5m = self.unique_page_views(5, "logged_in")
        anon_5m = self.unique_page_views(5, "anon")
        logged_in_30m = self.unique_page_views(30, "logged_in")
        anon_30m = self.unique_page_views(30, "anon")
        logged_in_1d = self.unique_page_views(24 * 60, "logged_in")
        anon_1d = self.unique_page_views(24 * 60, "anon")
        context["day_views_count"] = logged_in_1d + anon_1d
        context["5m_views_count"] = logged_in_5m + anon_5m
        context["30m_views_count"] = logged_in_30m + anon_30m
        if logged_in_1d + anon_1d == 0:
            anon_1d = 1  # so it's a hack to avoid /0, whatever
        context["day_views_count_percent_with_account"] = int(logged_in_1d / (logged_in_1d + anon_1d) * 100)
        timeframe = timezone.now() - timezone.timedelta(minutes=30)
        # check to make sure no auctions are happening before applying server updates
        context["in_person_lots_ended"] = Lot.objects.filter(
            is_deleted=False, auction__is_online=False, date_end__gte=timeframe, date_end__lte=timezone.now()
        ).count()
        timeframe = timezone.now() + timezone.timedelta(minutes=120)
        context["online_auction_lots_ending"] = Lot.objects.filter(
            is_deleted=False, date_end__lte=timeframe, date_end__gte=timezone.now()
        ).count()
        # users_with_printed_labels = User.objects.filter(lot__label_printed=True).distinct()
        # context["users_with_printed_labels"] = users_with_printed_labels.count()
        # context["preset_counts"] = (
        #     UserLabelPrefs.objects.filter(user__in=users_with_printed_labels)
        #     .values("preset")
        #     .annotate(count=Count("user"))
        #     .order_by("-count")
        # )
        return context


class UserMap(TemplateView):
    template_name = "user_map.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.request.user.is_superuser:
            messages.error(self.request, "Only admins can view the user map")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        data = self.request.GET.copy()
        view = data.get("view")
        filter1 = data.get("filter")
        try:
            numeric_filter = int(filter1)
        except (TypeError, ValueError):
            numeric_filter = None
        # view_qs = PageView.objects.exclude(latitude=0)
        qs = (
            User.objects.filter(userdata__isnull=False, is_active=True)
            .exclude(userdata__latitude=0, userdata__longitude=0)
            .select_related("userdata")
            .annotate(lots_sold=Count("lot", distinct=True), lots_bought=Count("winner", distinct=True))
        )
        if view == "club" and filter1:
            # Users from a club
            qs = qs.filter(userdata__club__name=filter1)
        elif view == "buyers_and_sellers" and numeric_filter is not None:
            # Users who sold and bought
            qs = qs.filter(lots_sold__gte=numeric_filter, lots_bought__gte=numeric_filter)
        elif view == "volume" and numeric_filter is not None:
            # users by top volume_percentile
            qs = qs.filter(userdata__volume_percentile__lte=numeric_filter)
        elif view == "recent" and numeric_filter is not None:
            # view_qs = view_qs.filter(date_start__gte=timezone.now() - timedelta(hours=int(filter1)))
            qs = qs.filter(userdata__last_activity__gte=timezone.now() - timedelta(hours=numeric_filter))
        context["users"] = qs
        # context["pageviews"] = view_qs
        return context


class AdminUserFlow(AdminOnlyViewMixin, TemplateView):
    """Navigation flow analysis for Command Palette design.

    Shows per-auction: which page sections users visit most, and where they go next.
    Scoped to logged-in users only. Sessions split on 30-minute idle gaps.
    """

    template_name = "dashboard_user_flow.html"

    SESSION_GAP = timedelta(minutes=30)

    # Ordered — first match wins
    URL_SECTIONS = [
        ("Bulk Add Lots", re.compile(r"^/auctions/[^/]+/(users/[^/]+/(bulk-add-auto)?$|lots/bulk-add(-auto)?/)")),
        ("Auction Rules", re.compile(r"^/auctions/[^/]+/rules/")),
        ("Auction Invoice (Mine)", re.compile(r"^/auctions/[^/]+/invoice/")),
        ("Auction Invoices", re.compile(r"^/auctions/[^/]+/invoices/")),
        ("Auction Stats", re.compile(r"^/auctions/[^/]+/stats/")),
        ("Auction Edit", re.compile(r"^/auctions/[^/]+/edit/")),
        ("Auction Browse", re.compile(r"^/auctions/[^/]+/?$")),
        ("All Auctions", re.compile(r"^/auctions/?$")),
        ("Lot Detail", re.compile(r"^/lots/\d+")),
        ("Lot Detail", re.compile(r"^/auctions/[^/]+/lots/")),
        ("Add Lot", re.compile(r"^/lots/new/")),
        ("Edit Lot", re.compile(r"^/lots/edit/\d+")),
        ("Invoice", re.compile(r"^/invoices/[^/]")),
        ("User Profile", re.compile(r"^/users/")),
        ("My Account", re.compile(r"^/account/")),
        ("All Lots", re.compile(r"^/lots/?$")),
        ("Homepage", re.compile(r"^/?$")),
    ]

    @classmethod
    def classify_url(cls, url):
        if not url:
            return "Other"
        for label, pattern in cls.URL_SECTIONS:
            if pattern.match(url):
                return label
        return "Other"

    @classmethod
    def _process_session(cls, session, transitions):
        sections = []
        for v in session:
            section = cls.classify_url(v["url"])
            if not sections or sections[-1] != section:
                sections.append(section)
        for i in range(len(sections) - 1):
            transitions[sections[i]][sections[i + 1]] += 1

    @classmethod
    def _compute_flow(cls, auction):
        """Compute (frequency_table, transition_table) for auction, or all auctions if None."""
        if auction is None:
            views_qs = (
                PageView.objects.filter(user__isnull=False)
                .order_by("user_id", "date_start")
                .values("user_id", "url", "date_start", "total_time")
            )
        else:
            auction_views = PageView.objects.filter(auction=auction, user__isnull=False).values(
                "user_id", "url", "date_start", "total_time"
            )
            lot_views = PageView.objects.filter(lot_number__auction=auction, user__isnull=False).values(
                "user_id", "url", "date_start", "total_time"
            )
            # UNION keeps both branches on their own index paths; OR forces a full scan
            views_qs = auction_views.union(lot_views, all=True).order_by("user_id", "date_start")

        section_stats = collections.defaultdict(lambda: {"views": 0, "users": set(), "total_time": 0})
        transitions = collections.defaultdict(lambda: collections.defaultdict(int))

        current_user = None
        session = []
        for v in views_qs:
            if v["user_id"] != current_user:
                if session:
                    cls._process_session(session, transitions)
                current_user = v["user_id"]
                session = [v]
            else:
                if session and (v["date_start"] - session[-1]["date_start"]) > cls.SESSION_GAP:
                    cls._process_session(session, transitions)
                    session = [v]
                else:
                    session.append(v)
            section = cls.classify_url(v["url"])
            section_stats[section]["views"] += 1
            section_stats[section]["users"].add(v["user_id"])
            section_stats[section]["total_time"] += v["total_time"] or 0
        if session:
            cls._process_session(session, transitions)

        frequency_table = sorted(
            [
                {
                    "section": section,
                    "views": stats["views"],
                    "unique_users": len(stats["users"]),
                    "avg_time": round(stats["total_time"] / stats["views"]) if stats["views"] else 0,
                }
                for section, stats in section_stats.items()
            ],
            key=lambda x: -x["views"],
        )
        transition_table = []
        for from_section, nexts in transitions.items():
            total = sum(nexts.values())
            top_nexts = sorted(nexts.items(), key=lambda x: -x[1])[:5]
            transition_table.append(
                {
                    "from": from_section,
                    "total_transitions": total,
                    "nexts": [{"section": s, "count": c, "pct": round(100 * c / total)} for s, c in top_nexts],
                }
            )
        transition_table.sort(key=lambda x: -x["total_transitions"])
        return frequency_table, transition_table

    def post(self, request, *args, **kwargs):
        from auctions.tasks import USER_FLOW_LOCK_KEY, compute_user_flow_all

        # The task drops a second request rather than queueing it -- it holds a worker slot for as
        # long as it takes, and the worker has two. Ask the same lock here so the page says what
        # actually happened; the task asks again for real, so a press landing in the gap between
        # these two lines is still dropped there rather than run twice.
        if cache.get(USER_FLOW_LOCK_KEY):
            messages.info(request, "A user flow computation is already running. Refresh in a few minutes.")
        else:
            compute_user_flow_all.delay()
            messages.success(request, "User flow computation started in the background. Refresh after a few minutes.")
        target = request.get_full_path()
        if url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(target)
        return redirect("/")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auctions"] = Auction.objects.order_by("-date_end")[:50]
        context["all_flow_cached_at"] = cache.get("user_flow_all_computed_at")

        auction_slug = self.request.GET.get("auction")
        if not auction_slug:
            return context

        if auction_slug == "__all__":
            cached = cache.get("user_flow_all")
            if cached:
                context["is_all_auctions"] = True
                context["flow_cached_at"] = cache.get("user_flow_all_computed_at")
                context["frequency_table"] = cached["frequency_table"]
                context["transition_table"] = cached["transition_table"]
            else:
                context["is_all_auctions"] = True
                context["flow_not_cached"] = True
            return context

        try:
            auction = Auction.objects.get(slug=auction_slug)
        except Auction.DoesNotExist:
            return context
        context["selected_auction"] = auction

        cached = cache.get(f"user_flow_{auction.pk}")
        if cached:
            context["flow_cached_at"] = cached.get("computed_at")
            context["frequency_table"] = cached["frequency_table"]
            context["transition_table"] = cached["transition_table"]
            return context

        frequency_table, transition_table = self._compute_flow(auction)
        context["frequency_table"] = frequency_table
        context["transition_table"] = transition_table
        return context


class ClubMap(TemplateView):
    template_name = "clubs.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_CLUB_FINDER:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["clubs"] = Club.objects.filter(active=True, latitude__isnull=False)
        context["location_message"] = "Set your location to see clubs near you"
        latitude_cookie = self.request.COOKIES.get("latitude")
        longitude_cookie = self.request.COOKIES.get("longitude")
        if latitude_cookie:
            context["latitude"] = latitude_cookie
            context["longitude"] = longitude_cookie
        context["hide_google_login"] = True
        return context


class UserAgreement(TemplateView):
    template_name = "tos_wrapper.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        tos_path = Path(settings.BASE_DIR / "tos.html")
        if Path.exists(tos_path):
            with Path.open(tos_path) as file:
                context["tos_content"] = file.read()
        else:
            msg = "No TOS found.  You must place a file called tos.html in the root project directory (next to the .env file)"
            raise ImproperlyConfigured(msg)
        return context


def site_webmanifest(request):
    """Web app manifest so Android/Chrome use the real icons when adding to the home screen.

    Served from a view rather than a static file so the name follows NAVBAR_BRAND.
    """
    return JsonResponse(
        {
            "name": settings.NAVBAR_BRAND,
            "short_name": settings.NAVBAR_BRAND,
            "icons": [
                {"src": static("android-chrome-192x192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": static("android-chrome-512x512.png"), "sizes": "512x512", "type": "image/png"},
                {
                    "src": static("android-chrome-maskable-192x192.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                {
                    "src": static("android-chrome-maskable-512x512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "theme_color": "#212529",
            "background_color": "#212529",
            "display": "browser",
        },
        content_type="application/manifest+json",
    )
