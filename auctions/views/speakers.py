"""The speaker directory: who will come and talk to a club, and what about.

Behind ``NECSpeakerAccessMixin``: any club permission in a club flagged ``is_nec_club``.
"""

import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models.base import Model as Model
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.views.generic import DetailView, View
from django.views.generic.edit import (
    CreateView,
    UpdateView,
)

from auctions.filters import (
    SpeakerFilter,
)
from auctions.forms import (
    SpeakerCommentForm,
    SpeakerForm,
)
from auctions.models import (
    ClubMember,
    Speaker,
    SpeakerComment,
    SpeakerTag,
    distance_to,
)
from auctions.tables import (
    SpeakerHTMxTable,
)

from .base import HTMxTableView, check_club_permission, clubs_with_any_permission

logger = logging.getLogger(__name__)


class NECSpeakerAccessMixin(LoginRequiredMixin):
    """Gate the speaker directory to people involved with an NEC member club.

    Renders an explanation rather than a bare 403, since most people reaching it are in clubs that
    simply aren't NEC members.
    """

    def dispatch(self, request, *args, **kwargs):
        self.nec_clubs = []
        self._origin = None
        if request.user.is_authenticated:
            self.nec_clubs = list(clubs_with_any_permission(request.user))
            # A superuser gets in before any club is flagged, or nobody could tick the box.
            if not self.nec_clubs and not request.user.is_superuser:
                return render(request, "auctions/speaker_no_access.html", status=403)
        return super().dispatch(request, *args, **kwargs)

    @property
    def nec_club_ids(self):
        return [club.pk for club in self.nec_clubs]

    def visible_speakers(self):
        """Speakers this user may see: currently all of them, since everyone here is in an NEC club.

        When the directory opens up, only this method changes: ``nec_only`` rows drop out for others.
        """
        queryset = Speaker.objects.filter(is_deleted=False)
        if self.nec_clubs or self.request.user.is_superuser:
            return queryset
        return queryset.filter(nec_only=False)

    def resolve_origin(self):
        """What distances on this page are measured from.

        ``?club=<slug>`` wins when the user has a permission in that club, so the page is shareable between
        officers; otherwise the user's own coordinates. Returns (latitude, longitude, club, label,
        change_url). Cached per request, since four hooks need the same answer.
        """
        if self._origin is not None:
            return self._origin
        self._origin = self._resolve_origin_uncached()
        return self._origin

    def _resolve_origin_uncached(self):
        requested_slug = (self.request.GET.get("club") or "").strip()
        if requested_slug:
            club = next((c for c in self.nec_clubs if c.slug == requested_slug), None)
            if club:
                change_url = (
                    reverse("club_edit", kwargs={"slug": club.slug})
                    if check_club_permission(self.request.user, club, "permission_edit_club")
                    else None
                )
                if club.latitude and club.longitude:
                    return club.latitude, club.longitude, club, club.name, change_url
                # A club with no address still scopes the page; say so rather than using the user's
                # location.
                return None, None, club, club.name, change_url
        # contact_info is the page with the location map, not preferences.
        userdata = getattr(self.request.user, "userdata", None)
        if userdata and userdata.latitude and userdata.longitude:
            return (
                userdata.latitude,
                userdata.longitude,
                None,
                "your location",
                reverse("contact_info"),
            )
        return None, None, None, "", reverse("contact_info")


class SpeakerListView(NECSpeakerAccessMixin, HTMxTableView):
    """The speaker directory, as a sortable table or a map of the same filtered set.

    Subclasses HTMxTableView because the htmx response also carries an out-of-band payload of every
    matching speaker's coordinates, so the map redraws without losing its pan and zoom.
    """

    model = Speaker
    table_class = SpeakerHTMxTable
    filterset_class = SpeakerFilter
    template_name = "auctions/speaker_list.html"
    htmx_template_name = "auctions/partials/speaker_table.html"
    htmx_table_header_template = "auctions/partials/speaker_table_header.html"

    def get_queryset(self):
        """Newest first.

        The model orders by `name` ("Last, First"), but the list renders `display_name`, so that reads as
        unsorted. Recency puts what changed since the last visit at the top, and the "New" badge marks the
        same speakers in other orders. Distance is a click on the Location column
        (SpeakerHTMxTable.order_location), so the annotation stays either way.
        """
        queryset = self.visible_speakers().prefetch_related("topics")
        latitude, longitude, *_ = self.resolve_origin()
        if latitude is not None and longitude is not None:
            queryset = queryset.annotate(distance=distance_to(latitude, longitude))
        # -pk too: the 405 imported speakers share a timestamp, so pagination could repeat one.
        return queryset.order_by("-createdon", "-pk")

    def get_filterset_kwargs(self, filterset_class):
        kwargs = super().get_filterset_kwargs(filterset_class)
        latitude, longitude, *_ = self.resolve_origin()
        kwargs["latitude"] = latitude
        kwargs["longitude"] = longitude
        kwargs["nec_club_ids"] = self.nec_club_ids
        kwargs["user"] = self.request.user
        return kwargs

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        latitude, longitude, *_ = self.resolve_origin()
        kwargs["has_origin"] = latitude is not None and longitude is not None
        return kwargs

    def get_filter_placeholder_text(self):
        # Also the only hint that a radius can be searched for. Short: this box is a phone wide.
        return 'Search speakers, or "within 50 miles"'

    def get_possible_filters(self):
        # photo / mapped / myclub work as search keywords but aren't how anyone looks for a
        # speaker, and every menu row is one more to read past.
        return [
            ("<small class='text-muted'>Tagged as:</small>", ""),
            ("<i class='bi bi-hand-thumbs-up'></i> Would book again", "recommended"),
            ("<i class='bi bi-camera-video'></i> Presents remotely", "remote"),
            ("<i class='bi bi-car-front'></i> Willing to travel", "travels"),
            ("<i class='bi bi-box-seam'></i> Brings auction items", "auctionitems"),
            ("<i class='bi bi-tag'></i> Not tagged yet", "untagged"),
        ]

    def speakers_for_map(self, filterset):
        """Coordinates for every speaker matching the filters, ignoring pagination: a map of one page would
        mislead.
        """
        queryset = filterset.qs.filter(latitude__isnull=False, longitude__isnull=False)
        return [
            {
                "slug": speaker.slug,
                "name": speaker.display_name,
                "location": speaker.location,
                "lat": speaker.latitude,
                "lng": speaker.longitude,
                "thumbnail": speaker.thumbnail_url or "",
            }
            for speaker in queryset[:1000]
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        latitude, longitude, club, label, change_url = self.resolve_origin()
        filterset = context.get("filter")
        context["origin_club"] = club
        context["origin_label"] = label
        context["origin_change_url"] = change_url
        context["has_origin"] = latitude is not None and longitude is not None
        context["origin_latitude"] = latitude
        context["origin_longitude"] = longitude
        # The topic menu is markup the template writes, so the choices come through the context.
        context["topic_choices"] = filterset.topic_choices() if filterset else []
        selected_topic = self.request.GET.get("topic", "")
        context["selected_topic"] = selected_topic
        # Empty unless a topic is set, so the button reads "Topics".
        context["selected_topic_label"] = (
            dict(context["topic_choices"]).get(selected_topic, "") if selected_topic else ""
        )
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        context["google_maps_map_id"] = settings.GOOGLE_MAPS_MAP_ID
        context["is_htmx"] = bool(self.request.htmx)
        context["speakers_json"] = self.speakers_for_map(filterset) if filterset else []
        if filterset:
            total = filterset.qs.count()
            context["unmapped_count"] = total - len(context["speakers_json"])
            context["result_count"] = total
        context["default_view"] = "map" if self.request.GET.get("view") == "map" else "list"
        context["club_query"] = self.request.GET.get("club", "")
        # Only the full page renders the banner, and working it out reads every speaker's name.
        context["has_unlisted_members"] = False if self.request.htmx else self.has_unlisted_club_members()
        if filterset and context.get("result_count") == 0:
            context["no_results"] = self._build_no_results_html()
        return context

    def _build_no_results_html(self):
        """Empty state offering to add the person just searched for, which is when people notice a speaker is
        missing.
        """
        query = (self.request.GET.get("query") or "").strip()
        create_url = reverse("speaker_add")
        params = {}
        # Only prefill a name when the search looks like one, not a keyword or a scrap of a bio.
        keywords = set(SpeakerFilter.TAG_TOKENS) | {
            "photo",
            "photos",
            "mapped",
            "located",
            "myclub",
            "untagged",
            "needsreview",
        }
        if query and re.fullmatch(r"[A-Za-z\s\-'.]{2,60}", query) and query.lower() not in keywords:
            params["name"] = query
        club_slug = (self.request.GET.get("club") or "").strip()
        if club_slug:
            params["club"] = club_slug
        if params:
            create_url += f"?{urlencode(params)}"
        message = (
            format_html("<p class='text-muted mb-2'>No speakers match <strong>{}</strong>.</p>", query)
            if query
            else format_html("<p class='text-muted mb-2'>No speakers match these filters.</p>")
        )
        return format_html(
            "<div class='text-center py-3'>{}<a class='btn btn-info btn-sm' href='{}'>"
            "<i class='bi bi-person-plus-fill'></i> Add a speaker</a></div>",
            message,
            create_url,
        )

    def has_unlisted_club_members(self):
        """Whether any member of the user's NEC clubs is missing from the directory.

        Only ever a yes/no, and matched loosely: a false "already listed" beats nagging a club.
        """
        if not self.nec_clubs:
            return False
        # The NEC import stores "Last, First" and club members are "First Last", so index both.
        existing_names = set()
        for speaker in Speaker.objects.filter(is_deleted=False).only("name"):
            existing_names.add(speaker.name.casefold())
            existing_names.add(speaker.display_name.casefold())
        member_names = (
            ClubMember.objects.filter(club__in=self.nec_clubs, is_deleted=False)
            .exclude(name="")
            .values_list("name", flat=True)
        )
        return any(name.casefold() not in existing_names for name in member_names)


class SpeakerPanelView(NECSpeakerAccessMixin, DetailView):
    """The speaker card beside the list: an htmx fragment, and a whole page for a shared link (see
    SpeakerDetailView).
    """

    model = Speaker
    template_name = "auctions/partials/speaker_panel.html"
    context_object_name = "speaker"

    def get_queryset(self):
        return self.visible_speakers().prefetch_related("topics")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        speaker = self.object
        context["tag_counts"] = speaker.tag_counts()
        context["my_tags"] = speaker.tags_by_user(self.request.user)
        context["tag_groups"] = SpeakerTag.grouped_definitions()
        context["comments"] = speaker.comments.filter(is_deleted=False).select_related("user", "club")
        context["comment_form"] = SpeakerCommentForm()
        context["can_delete"] = speaker.can_be_deleted_by(self.request.user)
        context["can_edit"] = speaker.can_be_deleted_by(self.request.user)
        latitude, longitude, _club, _label, _change_url = self.resolve_origin()
        if latitude is not None and speaker.has_coordinates:
            context["distance"] = int(
                Speaker.objects.filter(pk=speaker.pk)
                .annotate(distance=distance_to(latitude, longitude))
                .values_list("distance", flat=True)
                .first()
                or 0
            )
        return context


class SpeakerDetailView(SpeakerPanelView):
    """A speaker's own page, for when someone shares the URL the panel pushed."""

    template_name = "auctions/speaker_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["standalone"] = True
        return context


class SpeakerCreateView(NECSpeakerAccessMixin, CreateView):
    """Add a speaker.  Anyone who can see the directory can add to it."""

    model = Speaker
    form_class = SpeakerForm
    template_name = "auctions/speaker_form.html"

    def get_initial(self):
        initial = super().get_initial()
        # Prefilled by the "add your club members" prompt on the list page.
        for field in ("name", "email", "phone"):
            value = self.request.GET.get(field)
            if value:
                initial[field] = value
        return initial

    def club_being_represented(self):
        """The club to record as the source of this entry, without asking.

        ``?club=`` is the club they came in from; failing that, somebody in exactly one NEC club can only
        be representing that one. Somebody in several records no club. Same rule as SpeakerCommentView.
        """
        club_slug = (self.request.GET.get("club") or "").strip()
        if club_slug:
            club = next((c for c in self.nec_clubs if c.slug == club_slug), None)
            if club:
                return club
        return self.nec_clubs[0] if len(self.nec_clubs) == 1 else None

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.club = self.club_being_represented()
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.display_name} has been added to the speaker directory.")
        return response

    def get_success_url(self):
        return reverse("speaker_detail", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = "Add a speaker"
        return context


class SpeakerUpdateView(NECSpeakerAccessMixin, UpdateView):
    """Edit a speaker; restricted to whoever added them (imported rows: superusers only)."""

    model = Speaker
    form_class = SpeakerForm
    template_name = "auctions/speaker_form.html"

    def get_queryset(self):
        return self.visible_speakers()

    def get_object(self, queryset=None):
        speaker = super().get_object(queryset)
        if not speaker.can_be_deleted_by(self.request.user):
            raise PermissionDenied()
        return speaker

    def get_success_url(self):
        return reverse("speaker_detail", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = f"Edit {self.object.display_name}"
        return context


class SpeakerDeleteView(NECSpeakerAccessMixin, View):
    """Soft-delete a speaker you added, keeping other clubs' tags and comments in case it's a mistake."""

    def post(self, request, slug):
        speaker = get_object_or_404(self.visible_speakers(), slug=slug)
        if not speaker.can_be_deleted_by(request.user):
            raise PermissionDenied()
        Speaker.objects.filter(pk=speaker.pk).update(is_deleted=True)
        messages.success(request, f"{speaker.display_name} has been removed from the speaker directory.")
        return redirect("speaker_list")


class SpeakerTagView(NECSpeakerAccessMixin, View):
    """Toggle one of the current user's tags on a speaker and re-render the tag block."""

    def post(self, request, slug):
        speaker = get_object_or_404(self.visible_speakers(), slug=slug)
        tag = (request.POST.get("tag") or "").strip()
        if tag not in SpeakerTag.TAG_LABELS:
            raise Http404
        existing = SpeakerTag.objects.filter(speaker=speaker, user=request.user, tag=tag)
        if existing.exists():
            existing.delete()
        else:
            SpeakerTag.objects.get_or_create(speaker=speaker, user=request.user, tag=tag)
        return render(
            request,
            "auctions/partials/speaker_tags.html",
            {
                "speaker": speaker,
                "tag_counts": speaker.tag_counts(),
                "my_tags": speaker.tags_by_user(request.user),
                "tag_groups": SpeakerTag.grouped_definitions(),
            },
        )


class SpeakerCommentView(NECSpeakerAccessMixin, View):
    """Add a comment to a speaker, and re-render the comment list."""

    def post(self, request, slug):
        speaker = get_object_or_404(self.visible_speakers(), slug=slug)
        form = SpeakerCommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.speaker = speaker
            comment.user = request.user
            # Recorded for the admin only, and only when there's one club to attach.
            comment.club = self.nec_clubs[0] if len(self.nec_clubs) == 1 else None
            comment.save()
            form = SpeakerCommentForm()
        return render(
            request,
            "auctions/partials/speaker_comments.html",
            {
                "speaker": speaker,
                "comments": speaker.comments.filter(is_deleted=False).select_related("user", "club"),
                "comment_form": form,
            },
        )


class SpeakerCommentDeleteView(NECSpeakerAccessMixin, View):
    """Remove your own comment (or, for a superuser, anyone's)."""

    def post(self, request, slug, pk):
        speaker = get_object_or_404(self.visible_speakers(), slug=slug)
        comment = get_object_or_404(SpeakerComment, pk=pk, speaker=speaker)
        if not comment.can_be_deleted_by(request.user):
            raise PermissionDenied()
        SpeakerComment.objects.filter(pk=comment.pk).update(is_deleted=True)
        return render(
            request,
            "auctions/partials/speaker_comments.html",
            {
                "speaker": speaker,
                "comments": speaker.comments.filter(is_deleted=False).select_related("user", "club"),
                "comment_form": SpeakerCommentForm(),
            },
        )
