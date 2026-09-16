"""The public club finder: a map of clubs, and the same clubs as a filtered list.

Everything here is visible to anybody, which is the whole constraint. ``Club.objects.listed()`` --
approved, and not since folded -- is the only gate.

So there are no addresses (the map has always shown a pin, not the street), no member names or
counts, no contact addresses, and nothing from the outreach queue: ``outreach_stage``,
``stall_reason``, ``date_contacted`` and ``notes`` are a record of our conversations, not facts
about the club. The filters are interests, what a club has coming up, and whether it is taking
members -- three things already on the club's page.

There is deliberately **no detail panel**: a row leads to the club's own page. A pin opens a small
info window with the name, website and Facebook links, interests and "View all club info", because
making a visitor open the club page to reach its website is a wasted load. That window is a second
public surface, so it carries nothing the club page doesn't show a signed-out visitor, and
``test_club_finder`` pins the payload to those fields.

Distance is measured from the public pin to a location the visitor supplied, so it tells them
something without telling anybody anything about the club.
"""

import logging

from django.conf import settings
from django.db.models import Exists, OuterRef, Q, Subquery
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from auctions.filters import ClubFilter
from auctions.models import Club, ClubEvent, distance_to
from auctions.tables import ClubHTMxTable

from .base import HTMxTableView, LocationMixin

logger = logging.getLogger(__name__)

#: Ceiling on pins per map. Far above the number of clubs that exist, and here because the payload
#: is every matching club rather than the current page.
CLUB_MAP_LIMIT = 1000


def _external_url(value):
    """A club's typed-in website or Facebook page as a link, prefixed as club_detail.html does.

    Anything not starting with ``http`` gets ``https://``, which also stops a typed-in ``javascript:``
    becoming a live link.
    """
    value = (value or "").strip()
    if not value:
        return ""
    return value if value.startswith("http") else f"https://{value}"


def _upcoming_events_subquery():
    """The club's next event as a subquery -- the same events its public page lists.

    Pickup times are excluded as ``club_events.next_member_facing_event`` excludes them: "next event:
    pickup" tells a stranger nothing about whether this club meets.
    """
    now = timezone.now()
    return (
        ClubEvent.objects.filter(club=OuterRef("pk"), is_deleted=False, cancelled=False)
        .exclude(source=ClubEvent.SOURCE_PICKUP)
        .filter(Q(date_end__gte=now) | Q(date_end__isnull=True, date_start__gte=now))
        .order_by("date_start")
    )


class ClubFinderView(LocationMixin, HTMxTableView):
    """Find a club: the list and the map of it, filtered together.

    Like the speaker directory, the htmx response carries an out-of-band payload of every matching
    club's coordinates, so the map redraws without reloading the page or losing its pan and zoom.
    """

    model = Club
    table_class = ClubHTMxTable
    filterset_class = ClubFilter
    template_name = "clubs.html"
    htmx_template_name = "auctions/partials/club_table.html"
    htmx_table_header_template = "auctions/partials/club_table_header.html"
    no_location_message = "Set your location to see clubs near you"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_CLUB_FINDER:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def origin(self):
        """Where distances are measured from, as (latitude, longitude) or (None, None).

        Cached because four hooks need the same answer and ``get_coordinates`` reads cookies and userdata.
        """
        latitude, longitude = self.get_coordinates()
        if not latitude or not longitude:
            return None, None
        return latitude, longitude

    @property
    def has_origin(self):
        return self.origin[0] is not None

    def get_queryset(self):
        """Listed clubs in alphabetical order, annotated with what's coming up.

        Alphabetical rather than newest-first: people scan a club list for a name they half-know, and clubs
        are added rarely enough that recency would look random.
        """
        upcoming = _upcoming_events_subquery()
        queryset = (
            Club.objects.listed()
            .prefetch_related("interests")
            .annotate(
                has_upcoming_event=Exists(upcoming),
                next_event_title=Subquery(upcoming.values("title")[:1]),
                next_event_start=Subquery(upcoming.values("date_start")[:1]),
            )
        )
        latitude, longitude = self.origin
        if latitude is not None:
            queryset = queryset.annotate(distance=distance_to(latitude, longitude))
        return queryset.order_by("name")

    def get_filterset_kwargs(self, filterset_class):
        kwargs = super().get_filterset_kwargs(filterset_class)
        latitude, longitude = self.origin
        kwargs["latitude"] = latitude
        kwargs["longitude"] = longitude
        return kwargs

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["has_origin"] = self.has_origin
        return kwargs

    def get_filter_placeholder_text(self):
        # Also the only hint that a radius can be searched for. Short: this box is a phone wide.
        return 'Search clubs, or "within 50 miles"'

    def get_possible_filters(self):
        return [
            ("<small class='text-muted'>Show only clubs:</small>", ""),
            ("<i class='bi bi-calendar-event'></i> With something coming up", "events"),
            ("<i class='bi bi-person-plus'></i> Taking new members", "joinable"),
            ("<i class='bi bi-globe'></i> With a website", "website"),
        ]

    def clubs_for_map(self, filterset):
        """Coordinates for every club matching the filters, ignoring pagination: a map of one page would
        mislead. Everything here is also on the club's public page.
        """
        queryset = filterset.qs.filter(latitude__isnull=False, longitude__isnull=False)
        return [
            {
                "slug": club.slug,
                "name": club.name,
                "lat": club.latitude,
                "lng": club.longitude,
                "homepage": _external_url(club.homepage),
                "facebook": _external_url(club.facebook_page),
                "interests": sorted(interest.name for interest in club.interests.all()),
            }
            for club in queryset[:CLUB_MAP_LIMIT]
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filterset = context.get("filter")
        latitude, longitude = self.origin
        context["has_origin"] = self.has_origin
        context["origin_latitude"] = latitude
        context["origin_longitude"] = longitude
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["google_maps_map_id"] = settings.GOOGLE_MAPS_MAP_ID
        # The interest menu is markup the template writes, so the choices come through the context.
        context["interest_choices"] = filterset.interest_choices() if filterset else []
        selected_interest = self.request.GET.get("interest", "")
        context["selected_interest"] = selected_interest
        # Empty unless one is picked, so the button reads "Interests".
        context["selected_interest_label"] = (
            dict(context["interest_choices"]).get(selected_interest, "") if selected_interest else ""
        )
        context["clubs_json"] = self.clubs_for_map(filterset) if filterset else []
        if filterset:
            total = filterset.qs.count()
            context["result_count"] = total
            context["unmapped_count"] = total - len(context["clubs_json"])
            if total == 0:
                context["no_results"] = (
                    "<div class='text-center py-3'><p class='text-muted mb-0'>No clubs match these filters.</p></div>"
                )
        context["default_view"] = "map" if self.request.GET.get("view") == "map" else "list"
        return context
