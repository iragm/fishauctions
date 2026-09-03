"""Club API keys, and the page that documents the API they open.

``club_api_documentation_context`` fills in the documentation include, and is deliberately shared
with the ``club_api`` assistant tool so that a number in an example is the number the code
enforces. The endpoints themselves are in :mod:`auctions.views.club_api`.
"""

import logging
from datetime import timedelta
from datetime import timezone as date_tz

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db.models.base import Model as Model
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from auctions.llm import assist_enabled
from auctions.models import (
    Category,
    ClubAPIKey,
    ClubAPIKeyFieldMap,
    ClubHistory,
    ClubMember,
)
from auctions.serializers import (
    CLUB_MEMBER_API_KEY_MAPPING_FIELDS,
)
from auctions.species_matching import (
    MAX_SUGGESTIONS,
    LLMBudget,
)

from .base import ClubViewMixin, _compute_member_renewal_expiration
from .club_api import (
    BAP_LOT_DEFAULT_DAYS,
    LOT_ORDERING,
    LOT_PAGE_SIZE,
    MAX_LOT_PAGE_SIZE,
    SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY,
    club_api_current_auction,
    club_api_latest_auction,
)

logger = logging.getLogger(__name__)


# class ClubMemberIngestAPIView(APIView):
#     """API key-authenticated endpoint for external services to create ClubMember records."""

#     authentication_classes = [APIKeyAuthentication]
#     permission_classes = []
#     throttle_classes = [ApiKeyThrottle]

#     def post(self, request, slug=None):
#         api_key = request.api_key
#         club = request.club
#         if not slug or club.slug != slug:
#             return Response({"error": "API key does not belong to this club."}, status=403)
#         if not api_key.can_add_club_members:
#             return Response({"error": "API key cannot add club members."}, status=403)
#         mapped = map_fields(dict(request.data), api_key)
#         serializer = ClubMemberIngestSerializer(data=mapped)
#         if not serializer.is_valid():
#             received_fields = ", ".join(mapped.keys()) if mapped else "none"
#             ClubHistory.objects.create(
#                 club=club,
#                 user=None,
#                 action=(
#                     f"API ingest rejected [{api_key.prefix}] ({api_key.name}): {serializer.errors} "
#                     f"— received fields: {received_fields}. "
#                     f"Set up field mapping on this key to resolve this issue."
#                 ),
#                 applies_to="MEMBERS",
#             )
#             return Response({"status": "error", "errors": serializer.errors}, status=400)
#         member, created = create_club_member_from_api(serializer.validated_data, club, api_key)
#         return Response(
#             {"status": "created" if created else "duplicate", "member_id": member.pk},
#             status=201 if created else 200,
#         )


class ClubAPIKeyListView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """List all API keys for a club (requires permission_edit_club)."""

    active_tab = "api_keys"
    template_name = "auctions/club_api_keys.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["club"] = self.club
        ctx["api_keys"] = self.club.api_keys.order_by("-created_at")
        return ctx


class ClubAPIKeyCreateView(LoginRequiredMixin, ClubViewMixin, View):
    """Create a new ClubAPIKey; display the raw key once via session."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        return render(
            request,
            "auctions/club_api_key_create.html",
            {
                "club": self.club,
                "form_values": {
                    "name": "",
                    "can_add_club_members": True,
                    "can_read_club_member_list": False,
                    "can_update_club_members": False,
                    "can_add_bap_points": False,
                    "can_renew_memberships": False,
                    "can_look_up_species": False,
                    "can_read_auction_info": False,
                    "can_read_public_lots": False,
                    "can_read_private_lots": False,
                },
            },
        )

    def post(self, request, slug):
        def checkbox_value(name, *, default=False):
            if f"{name}_present" not in request.POST:
                return default
            return request.POST.get(name) == "on"

        name = request.POST.get("name", "").strip()
        form_values = {
            "name": name,
            "can_add_club_members": checkbox_value("can_add_club_members", default=True),
            "can_read_club_member_list": checkbox_value("can_read_club_member_list"),
            "can_update_club_members": checkbox_value("can_update_club_members"),
            "can_add_bap_points": checkbox_value("can_add_bap_points"),
            "can_renew_memberships": checkbox_value("can_renew_memberships"),
            "can_look_up_species": checkbox_value("can_look_up_species"),
            "can_read_auction_info": checkbox_value("can_read_auction_info"),
            "can_read_public_lots": checkbox_value("can_read_public_lots"),
            "can_read_private_lots": checkbox_value("can_read_private_lots"),
        }
        if not name:
            return render(
                request,
                "auctions/club_api_key_create.html",
                {"club": self.club, "error": "Name is required.", "form_values": form_values},
            )
        raw_key, prefix, key_hash = ClubAPIKey.generate()
        api_key = ClubAPIKey.objects.create(
            club=self.club,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            created_by=request.user,
            can_add_club_members=form_values["can_add_club_members"],
            can_read_club_member_list=form_values["can_read_club_member_list"],
            can_update_club_members=form_values["can_update_club_members"],
            can_add_bap_points=form_values["can_add_bap_points"],
            can_renew_memberships=form_values["can_renew_memberships"],
            can_look_up_species=form_values["can_look_up_species"],
            can_read_auction_info=form_values["can_read_auction_info"],
            can_read_public_lots=form_values["can_read_public_lots"],
            can_read_private_lots=form_values["can_read_private_lots"],
        )
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Created API key [{prefix}] '{name}'",
            applies_to="SETTINGS",
        )
        request.session[f"new_api_key_{api_key.pk}"] = raw_key
        return redirect(reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk}))


def _as_api_timestamp(value):
    """Format a datetime the way DRF renders one, so doc examples match real responses."""
    return value.astimezone(date_tz.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def club_api_documentation_context(club, api_key):
    """Everything ``_club_api_endpoints.html`` needs, for the page and for the assistant.

    This page *is* the club API's documentation -- every endpoint is written up in that template,
    behind the ``{% if %}`` for the permission it needs, and nowhere else. That sentence stayed
    true when an agent became the second reader: ``palette_actions.club_api`` renders the same
    include as plain text for somebody writing an integration, so building the context here
    rather than inside ``get_context_data`` is what makes that a second reader rather than a
    second copy that drifts.

    Every example is filled in from this club, so what it shows is a request its admin can paste
    and run, and the numbers are read off the code that enforces them.
    """
    today = timezone.now().date()
    now = timezone.now()
    return {
        "site_domain": Site.objects.get_current().domain,
        "example_member_id": (
            club.members.filter(is_deleted=False).order_by("-pk").values_list("pk", flat=True).first()
        ),
        # Dates in the renew example, so it shows what this club's renewal actually returns.
        "example_last_paid": today,
        "example_new_expiration": _compute_member_renewal_expiration(
            club, ClubMember(club=club, membership_expiration_date=None), today
        ),
        # The BAP lot example shows the real default window, formatted the way the API returns it.
        "bap_lot_default_days": BAP_LOT_DEFAULT_DAYS,
        "example_bap_range_end": _as_api_timestamp(now),
        "example_bap_range_start": _as_api_timestamp(now - timedelta(days=BAP_LOT_DEFAULT_DAYS)),
        "example_bap_lot_timestamp": _as_api_timestamp(now - timedelta(days=2)),
        "example_bap_award_date": (now - timedelta(days=2)).date(),
        # Field mappings rename incoming *club member* fields, so they mean nothing to a key that
        # only reads lots or looks up species -- and a settings box that does nothing is worse
        # than no box.
        "key_writes_club_members": any(
            (
                api_key.can_add_club_members,
                api_key.can_read_club_member_list,
                api_key.can_update_club_members,
                api_key.can_renew_memberships,
            )
        ),
        # Species lookup: the documented numbers come from the matcher itself, so the page can't
        # drift away from what the endpoint actually does.
        "species_lookup_max_results": MAX_SUGGESTIONS,
        "species_lookup_llm_calls_per_day": SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY,
        "species_lookup_llm_available": assist_enabled(),
        # What is left of it right now, so the page a club admin reads and the header their
        # software reads are the same number.
        "species_lookup_llm_remaining": LLMBudget.for_club(club, SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY).remaining,
        "example_category": Category.objects.order_by("name").first(),
        "lot_page_size": LOT_PAGE_SIZE,
        "max_lot_page_size": MAX_LOT_PAGE_SIZE,
        "lot_ordering": sorted(LOT_ORDERING),
        # A real slug from this club, so the example URLs are ones an admin can paste and run.
        "example_auction": club_api_current_auction(club) or club_api_latest_auction(club),
    }


class ClubAPIKeyDetailView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Manage a single ClubAPIKey and its field mappings."""

    template_name = "auctions/club_api_key_detail.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        api_key = get_object_or_404(ClubAPIKey, pk=kwargs["pk"], club=self.club)
        session_key = f"new_api_key_{api_key.pk}"
        new_raw_key = self.request.session.pop(session_key, None)
        ctx["club"] = self.club
        ctx["api_key"] = api_key
        ctx["field_mappings"] = api_key.field_mappings.order_by("external_field")
        ctx["new_raw_key"] = new_raw_key
        ctx["available_fields"] = sorted(CLUB_MEMBER_API_KEY_MAPPING_FIELDS)
        ctx.update(club_api_documentation_context(self.club, api_key))
        return ctx


class ClubAPIKeyRevokeView(LoginRequiredMixin, ClubViewMixin, View):
    """GET: confirmation page. POST: revoke (deactivate) a ClubAPIKey."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug, pk):
        api_key = get_object_or_404(ClubAPIKey, pk=pk, club=self.club, is_active=True)
        return render(request, "auctions/club_api_key_revoke_confirm.html", {"club": self.club, "api_key": api_key})

    def post(self, request, slug, pk):
        api_key = get_object_or_404(ClubAPIKey, pk=pk, club=self.club)
        api_key.is_active = False
        api_key.save(update_fields=["is_active"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Revoked API key [{api_key.prefix}] '{api_key.name}'",
            applies_to="SETTINGS",
        )
        messages.success(request, f"API key '{api_key.name}' has been revoked.")
        return redirect(reverse("club_api_keys", kwargs={"slug": self.club.slug}))


class ClubAPIKeyFieldMapCreateView(LoginRequiredMixin, ClubViewMixin, View):
    """POST-only: add a field mapping to a ClubAPIKey."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk):
        api_key = get_object_or_404(ClubAPIKey, pk=pk, club=self.club)
        external_field = request.POST.get("external_field", "").strip()
        internal_field = request.POST.get("internal_field", "").strip()
        if external_field and internal_field and internal_field in CLUB_MEMBER_API_KEY_MAPPING_FIELDS:
            ClubAPIKeyFieldMap.objects.get_or_create(
                api_key=api_key,
                external_field=external_field,
                defaults={"internal_field": internal_field},
            )
        return redirect(reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": pk}))


class ClubAPIKeyFieldMapDeleteView(LoginRequiredMixin, ClubViewMixin, View):
    """POST-only: delete a field mapping from a ClubAPIKey."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk, map_pk):
        api_key = get_object_or_404(ClubAPIKey, pk=pk, club=self.club)
        ClubAPIKeyFieldMap.objects.filter(pk=map_pk, api_key=api_key).delete()
        return redirect(reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": pk}))


class ClubMemberMapView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Map of club members who have geocoded coordinates."""

    active_tab = "map"
    template_name = "auctions/club_member_map.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_view"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import BooleanField, Case, Value, When
        from django.utils import timezone

        today = timezone.now().date()
        expired_whens = [When(membership_expiration_date__lt=today, then=Value(True))]
        if self.club.membership_annual_fee:
            expired_whens.append(When(membership_expiration_date__isnull=True, then=Value(True)))
        qs = (
            ClubMember.objects.filter(club=self.club, is_deleted=False, lat__isnull=False, lng__isnull=False)
            .exclude(address="")
            .annotate(
                is_expired=Case(
                    *expired_whens,
                    default=Value(False),
                    output_field=BooleanField(),
                )
            )
            .values("pk", "name", "email", "address", "lat", "lng", "is_expired")
        )
        context["club"] = self.club
        context["members_json"] = list(qs)
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        return context


class SelfServeContactLinkView(ClubViewMixin, View):
    """Allow a club member to update their own communication preferences via a UUID link.

    Accessible without authentication — the UUID in the URL acts as the token.
    URL levels: none → do_not_contact, essential → non_essential, all → contact
    """

    allow_non_admins = True

    _LEVEL_TO_STATUS = {
        "none": "do_not_contact",
        "essential": "non_essential",
        "all": "contact",
    }
    _STATUS_LABELS = {
        "do_not_contact": "no emails",
        "non_essential": "essential emails only",
        "contact": "all emails",
    }

    def get(self, request, slug, uuid, level):
        if level not in self._LEVEL_TO_STATUS:
            raise Http404
        member = get_object_or_404(ClubMember, club=self.club, uuid=uuid, is_deleted=False)
        new_status = self._LEVEL_TO_STATUS[level]
        ClubMember.objects.filter(pk=member.pk).update(contact_status=new_status)
        label = self._STATUS_LABELS[new_status]
        # The member acts on their own UUID link, so there's no acting user (matches
        # ClubMemberSelfServiceView, which logs the same kind of change)
        ClubHistory.objects.create(
            club=self.club,
            user=None,
            action=f"{member} set their email preferences to {label} (self-service)",
            applies_to="MEMBERS",
        )
        return render(
            request,
            "auctions/self_serve_contact.html",
            {"club": self.club, "member": member, "level": level, "label": label},
        )
