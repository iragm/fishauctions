"""The public club finder: a map of clubs, and the same clubs as a filtered list.

Everything here is visible to anybody, signed in or not, and that is the whole constraint on the
module. ``Club.objects.listed()`` is the only gate -- approved, and not since folded.

What that rules out is worth writing down, because the filters are where it would leak: no
addresses (the map has always shown a pin and deliberately not the street it sits on), no member
names or counts, no contact addresses, and nothing from the outreach queue -- ``outreach_stage``,
``stall_reason``, ``date_contacted`` and ``notes`` are a record of our conversations with a club,
not facts about it. So the filters are built out of interests, what a club has coming up, and
whether it is taking new members: three things a visitor could already read off the club's page.

There is deliberately **no summary card** here, which is the one place this differs from the
speaker directory it is otherwise built like. A row and a map pin both lead to the club's own page.
A card would be a second public surface carrying the same privacy rules, needing to be kept in step
with the page forever; and finding a club is a find-one task, unlike comparing speakers, so the
page load it saves is not worth that. The filters live in the query string, so Back returns to the
same list.

Distance is the one number here that isn't stored on the club. It is measured from the pin, which
is already public, to a location the *visitor* supplied, so it tells them something without telling
anybody anything about the club.
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

#: Ceiling on how many pins one map draws. Far above the number of clubs that exist, and here for
#: the same reason the speaker map has one: the payload is every *matching* club rather than the
#: current page, so it is the one query on this page with no natural limit.
CLUB_MAP_LIMIT = 1000


def _upcoming_events_subquery():
    """The club's next event, as a subquery -- the same events its public page lists.

    Pickup times are excluded here exactly as ``club_events.next_member_facing_event`` excludes
    them: an online auction's pickup window is a logistical detail for people who already bought
    something, and reading "next event: pickup" tells a stranger nothing about whether this club
    meets.
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

    Built the way the speaker directory is, and for the same reason -- an htmx filter normally
    swaps the table and nothing else, so the response here also carries an out-of-band payload of
    every matching club's coordinates and the map redraws its markers from that. Both halves
    therefore always show the same clubs, and filtering neither reloads the page nor loses the
    map's pan and zoom.
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
        """Where distances are measured from: (latitude, longitude), or (None, None).

        Cached because four different hooks on this view need the same answer, and
        ``get_coordinates`` reads cookies and may touch ``userdata`` each time it is asked.
        """
        latitude, longitude = self.get_coordinates()
        if not latitude or not longitude:
            return None, None
        return latitude, longitude

    @property
    def has_origin(self):
        return self.origin[0] is not None

    def get_queryset(self):
        """Listed clubs, in alphabetical order, annotated with what's coming up.

        Alphabetical rather than the speaker directory's newest-first: a club list is something
        people scan for a name they already half-know, and clubs are added here rarely enough that
        recency would be a near-random order to a reader.
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
        # Doubles as the only hint that a radius can be searched for, the way the speaker box does.
        # Short, because this box is the width of a phone.
        return 'Search clubs, or "within 50 miles"'

    def get_possible_filters(self):
        return [
            ("<small class='text-muted'>Show only clubs:</small>", ""),
            ("<i class='bi bi-calendar-event'></i> With something coming up", "events"),
            ("<i class='bi bi-person-plus'></i> Taking new members", "joinable"),
            ("<i class='bi bi-globe'></i> With a website", "website"),
        ]

    def clubs_for_map(self, filterset):
        """Coordinates for every club matching the current filters, not just this page.

        A map that only plotted the current page of results would be actively misleading, which is
        why this deliberately ignores pagination.

        Name and slug are all a pin needs: it opens an info window naming the club, and the name is
        a link to the club's own page.
        """
        queryset = filterset.qs.filter(latitude__isnull=False, longitude__isnull=False)
        return [
            {
                "slug": club.slug,
                "name": club.name,
                "lat": club.latitude,
                "lng": club.longitude,
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
        # The interest menu is markup the template writes itself (radios in a dropdown), so the
        # choices come through the context rather than off a rendered widget.
        context["interest_choices"] = filterset.interest_choices() if filterset else []
        selected_interest = self.request.GET.get("interest", "")
        context["selected_interest"] = selected_interest
        # Empty unless one is picked, so the button falls back to reading "Interests".
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
