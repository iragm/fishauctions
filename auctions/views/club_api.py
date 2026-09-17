"""The club REST API: ``/api/v1/clubs/<slug>/…``.

A ``ClubAPIKey`` or a signed-in club admin, both through ``require_club_permission``. Anything
naming a person is in a ``private`` block absent without the privacy flag; ``?filter=`` searches
public columns only; ``?ordering=`` is an allowlist.
"""

import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as date_tz

from django.core.exceptions import PermissionDenied
from django.db.models import (
    Q,
    prefetch_related_objects,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import generics
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from auctions.authentication import ApiKeyThrottle, OptionalAPIKeyAuthentication
from auctions.models import (
    Auction,
    BapAward,
    Category,
    Club,
    ClubHistory,
    ClubMember,
    Lot,
    LotImage,
    SpeciesCommonName,
    distance_to,
    normalize_species_name,
)
from auctions.serializers import (
    BapAwardAPIKeyCreateSerializer,
    ClubApiAuctionSerializer,
    ClubApiAuctionSummarySerializer,
    ClubApiLotSerializer,
    ClubBapLotSerializer,
    ClubMemberAPIKeySerializer,
    ClubMemberSerializer,
    SpeciesCommonNameCreateSerializer,
    SpeciesCreateSerializer,
    SpeciesMatchSerializer,
)
from auctions.services import (
    map_fields,
)
from auctions.species_matching import (
    MAX_SUGGESTIONS,
    LLMBudget,
    species_already_named,
    species_carrying_common_name,
    suggest_species,
    visible_common_names,
    visible_species,
)

from .base import IsAuthenticatedOrAPIKey, check_club_permission
from .club_members import renew_club_member

logger = logging.getLogger(__name__)


class ClubAPIViewMixin:
    """Shared mixin for club REST API views"""

    serializer_class = ClubMemberSerializer
    authentication_classes = [TokenAuthentication, SessionAuthentication, OptionalAPIKeyAuthentication]
    permission_classes = [IsAuthenticatedOrAPIKey]
    throttle_classes = [ApiKeyThrottle]

    def get_club(self):
        if not hasattr(self, "_club"):
            slug = self.kwargs.get("slug")
            self._club = get_object_or_404(Club, slug=slug)
            api_key = getattr(self.request, "api_key", None)
            if api_key and api_key.club_id != self._club.pk:
                msg = "API key does not belong to this club."
                raise PermissionDenied(msg)
        return self._club

    def is_api_key_request(self):
        return hasattr(self.request, "api_key")

    def initial(self, request, *args, **kwargs):
        """Touch last_used_at on every successful API key request."""
        super().initial(request, *args, **kwargs)
        if self.is_api_key_request():
            request.api_key.last_used_at = timezone.now()
            request.api_key.save(update_fields=["last_used_at"])

    def require_club_permission(self, user_permission, api_key_permission, message):
        club = self.get_club()
        if self.is_api_key_request():
            if not getattr(self.request.api_key, api_key_permission, False):
                self.permission_denied(self.request, message=message)
            return club
        if not check_club_permission(self.request.user, club, user_permission):
            self.permission_denied(self.request, message=message)
        return club

    def get_serializer_class(self):
        if self.is_api_key_request() and self.request.method in {"POST", "PUT", "PATCH"}:
            return ClubMemberAPIKeySerializer
        return self.serializer_class

    def get_mapped_request_data(self):
        if not self.is_api_key_request():
            return self.request.data
        return map_fields(dict(self.request.data), self.request.api_key)

    def get_queryset(self):
        club = self.require_club_permission(
            "permission_view",
            "can_read_club_member_list",
            "You do not have permission to view members of this club.",
        )
        qs = ClubMember.objects.filter(club=club, is_deleted=False)
        if club.latitude and club.longitude:
            qs = qs.annotate(
                distance_to=distance_to(club.latitude, club.longitude, lat_field_name="lat", lng_field_name="lng")
            )
        return qs


class ClubMemberListCreateAPIView(ClubAPIViewMixin, generics.ListCreateAPIView):
    """List and create club members via REST API"""

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        name = params.get("name", "").strip()
        filter_query = params.get("filter", "").strip()
        if name:
            qs = qs.filter(name__icontains=name)
        if filter_query:
            from auctions.filters import ClubMemberFilter

            qs = ClubMemberFilter({"query": filter_query}, queryset=qs).qs
        return qs

    def create(self, request, *args, **kwargs):
        if not self.is_api_key_request():
            return super().create(request, *args, **kwargs)
        serializer = self.get_serializer(data=self.get_mapped_request_data())
        try:
            serializer.is_valid(raise_exception=True)
        except Exception:
            # Logged with the raw POST so admins can diagnose it.
            try:
                club = self.get_club()
                actor = f"API key [{request.api_key.prefix}] ({request.api_key.name})"
                field_dump = ", ".join(f"{k}={v!r}" for k, v in request.data.items())
                errors = serializer.errors
                ClubHistory.objects.create(
                    club=club,
                    user=None,
                    action=(
                        f"Failed to create member via {actor} — validation errors: {errors} — POST data: {field_dump}"
                    ),
                    applies_to="MEMBERS",
                )
            except Exception:
                pass  # Never let the history write mask the original error
            raise
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=201, headers=headers)

    def perform_create(self, serializer):
        club = self.require_club_permission(
            "permission_add_edit",
            "can_add_club_members",
            "You do not have permission to add members to this club.",
        )
        save_kwargs = {"club": club}
        if self.is_api_key_request():
            save_kwargs["added_by"] = None
            save_kwargs["source"] = self.request.api_key.name
        else:
            save_kwargs["added_by"] = self.request.user
        member = serializer.save(**save_kwargs)
        actor = (
            f"API key [{self.request.api_key.prefix}] ({self.request.api_key.name})"
            if self.is_api_key_request()
            else "API"
        )
        ClubHistory.objects.create(
            club=club,
            user=None if self.is_api_key_request() else self.request.user,
            action=f"Added member {member} via {actor}",
            applies_to="MEMBERS",
        )


class ClubMemberDetailAPIView(ClubAPIViewMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a club member via REST API"""

    def get_queryset(self):
        if self.is_api_key_request() and self.request.method in {"PUT", "PATCH"}:
            club = self.require_club_permission(
                "permission_add_edit",
                "can_update_club_members",
                "You do not have permission to edit members of this club.",
            )
            return ClubMember.objects.filter(club=club, is_deleted=False)
        return super().get_queryset()

    def update(self, request, *args, **kwargs):
        if not self.is_api_key_request():
            return super().update(request, *args, **kwargs)
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=self.get_mapped_request_data(), partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        if getattr(instance, "_prefetched_objects_cache", None):
            instance._prefetched_objects_cache = {}
        return Response(serializer.data)

    def perform_update(self, serializer):
        club = self.require_club_permission(
            "permission_add_edit",
            "can_update_club_members",
            "You do not have permission to edit members of this club.",
        )
        member = serializer.save()
        actor = (
            f"API key [{self.request.api_key.prefix}] ({self.request.api_key.name})"
            if self.is_api_key_request()
            else "API"
        )
        ClubHistory.objects.create(
            club=club,
            user=None if self.is_api_key_request() else self.request.user,
            action=f"Updated member {member} via {actor}",
            applies_to="MEMBERS",
        )

    def perform_destroy(self, instance):
        if self.is_api_key_request():
            self.permission_denied(self.request, message="API keys cannot delete club members.")
        club = self.get_club()
        if not check_club_permission(self.request.user, club, "permission_add_edit"):
            self.permission_denied(self.request, message="You do not have permission to delete members of this club.")
        # Soft delete
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted"])
        ClubHistory.objects.create(
            club=club,
            user=self.request.user,
            action=f"Deleted member {instance}",
            applies_to="MEMBERS",
        )


class ClubMemberRenewAPIView(ClubAPIViewMixin, APIView):
    """Renew a membership from an external system.

    The member is found by email in the club (or created), then renewed exactly as the Renew button
    does. Writable member fields sent along are applied first; blanks are ignored.
    """

    def _actor(self):
        if self.is_api_key_request():
            return f"API key [{self.request.api_key.prefix}] ({self.request.api_key.name})"
        return "API"

    def post(self, request, slug):
        club = self.require_club_permission(
            "permission_add_edit",
            "can_renew_memberships",
            "You do not have permission to renew memberships for this club.",
        )
        data = self.get_mapped_request_data()
        email = (data.get("email") or "").strip().lower()
        if not email:
            return Response({"email": ["An email address is required to look up or create the member."]}, status=400)
        data = {key: value for key, value in data.items() if value not in ("", None)}
        member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).order_by("pk").first()
        created = member is None
        serializer = ClubMemberAPIKeySerializer(instance=member, data=data, partial=not created)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception:
            try:
                field_dump = ", ".join(f"{k}={v!r}" for k, v in request.data.items())
                ClubHistory.objects.create(
                    club=club,
                    user=None,
                    action=(
                        f"Failed to renew membership via {self._actor()} — validation errors: "
                        f"{serializer.errors} — POST data: {field_dump}"
                    ),
                    applies_to="MEMBERSHIP",
                )
            except Exception:
                pass  # Never let the history write mask the original error
            raise
        save_kwargs = {}
        if created:
            save_kwargs = {"club": club}
            if self.is_api_key_request():
                save_kwargs["added_by"] = None
                save_kwargs["source"] = self.request.api_key.name
            else:
                save_kwargs["added_by"] = request.user
        member = serializer.save(**save_kwargs)
        if created:
            ClubHistory.objects.create(
                club=club,
                user=None if self.is_api_key_request() else request.user,
                action=f"Added member {member} via {self._actor()} (membership renewal)",
                applies_to="MEMBERS",
            )
        member = renew_club_member(
            member,
            acting_user=None if self.is_api_key_request() else request.user,
            actor=self._actor(),
        )
        return Response(
            {"created": created, **ClubMemberSerializer(member).data},
            status=201 if created else 200,
        )


class ClubMemberBapAwardAPIView(ClubAPIViewMixin, APIView):
    """Add BAP points to a club member via REST API."""

    serializer_class = BapAwardAPIKeyCreateSerializer

    def post(self, request, slug, pk):
        club = self.require_club_permission(
            "permission_manage_bap",
            "can_add_bap_points",
            "You do not have permission to add BAP points to this club.",
        )
        if not club.enable_breeder_award_program:
            raise Http404
        member = get_object_or_404(ClubMember, pk=pk, club=club, is_deleted=False)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        award = BapAward.objects.create(
            club_member=member,
            date=serializer.validated_data.get("date") or timezone.now().date(),
            points=serializer.validated_data["points"],
            notes=serializer.validated_data.get("notes", ""),
            awarded_by=None if self.is_api_key_request() else request.user,
        )
        actor = f"API key [{request.api_key.prefix}] ({request.api_key.name})" if self.is_api_key_request() else "API"
        ClubHistory.objects.create(
            club=club,
            user=None if self.is_api_key_request() else request.user,
            action=f"Added {award} to {member} via {actor}",
            applies_to="BAP",
        )
        return Response({"id": award.pk, "member_id": member.pk, "points": award.points}, status=201)


BAP_LOT_DEFAULT_DAYS = 30


def parse_bap_lot_date_range(params):
    """Resolve ``start``/``end``/``days`` into an aware datetime range. Bare dates are inclusive; explicit
    dates beat ``days``; default is ``BAP_LOT_DEFAULT_DAYS``. Raises ValueError with a caller message.
    """
    now = timezone.now()

    def parse_bound(name, *, end_of_day):
        raw = (params.get(name) or "").strip()
        if not raw:
            return None
        # Dates first: parse_datetime accepts "2026-03-31" as midnight, dropping that day.
        parsed_date = parse_date(raw)
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, datetime.max.time() if end_of_day else datetime.min.time())
        else:
            parsed = parse_datetime(raw)
            if parsed is None:
                msg = f"Could not read {name}={raw!r}. Use YYYY-MM-DD or an ISO 8601 timestamp."
                raise ValueError(msg)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    start = parse_bound("start", end_of_day=False)
    end = parse_bound("end", end_of_day=True)
    days = None
    raw_days = (params.get("days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            msg = f"Could not read days={raw_days!r}. Use a whole number of days."
            raise ValueError(msg) from None
        if days < 1:
            msg = "days must be at least 1."
            raise ValueError(msg)
    if start is None and end is None:
        end = now
        start = end - timedelta(days=days or BAP_LOT_DEFAULT_DAYS)
    elif start is None:
        start = end - timedelta(days=days or BAP_LOT_DEFAULT_DAYS)
    elif end is None:
        end = start + timedelta(days=days) if days else now
    if end < start:
        msg = "end must not be before start."
        raise ValueError(msg)
    return start, end


class ClubBapLotListAPIView(ClubAPIViewMixin, APIView):
    """Lots from this club's auctions that ended in a date range, for an external breeder award program.

    Unsold lots are included with empty winner fields. ``lot_id`` never changes or gets reused, so
    callers can dedupe on it across overlapping pulls.

    GET /api/v1/clubs/<slug>/bap-lots/?days=30  (or ?start=YYYY-MM-DD&end=YYYY-MM-DD, inclusive)
    """

    serializer_class = ClubBapLotSerializer

    def get(self, request, slug):
        club = self.require_club_permission(
            "permission_manage_bap",
            "can_add_bap_points",
            "You do not have permission to view BAP lots for this club.",
        )
        if not club.enable_breeder_award_program:
            raise Http404
        try:
            start, end = parse_bap_lot_date_range(request.query_params)
        except ValueError as error:
            # Don't echo the parser's exception; say the accepted shape.
            logger.info("Rejected BAP lot date range for club %s: %s", club.pk, error)
            return Response(
                {"error": "Invalid date range. Use ?days=N, or ?start=YYYY-MM-DD and ?end=YYYY-MM-DD."},
                status=400,
            )
        lots = (
            Lot.objects.filter(
                auction__club=club,
                is_deleted=False,
                banned=False,
                date_end__gte=start,
                date_end__lte=end,
            )
            .select_related(
                "auctiontos_seller",
                "auctiontos_winner",
                "user",
                "winner",
                "auction__club",
                "species_category",
                "bap_award",
            )
            .order_by("-date_end")
        )
        serializer = self.serializer_class(lots, many=True)
        return Response(
            {
                # UTC, to match the timestamp on each lot
                "start": start.astimezone(date_tz.utc),
                "end": end.astimezone(date_tz.utc),
                "count": len(serializer.data),
                "results": serializer.data,
            }
        )


#: Look-back for the "current" auction; older and still not wound down isn't what anyone means.
CURRENT_AUCTION_WINDOW_DAYS = 90

#: Default and ceiling for ``?limit=`` on the lot list.
LOT_PAGE_SIZE = 100
MAX_LOT_PAGE_SIZE = 500

#: ``?ordering=`` names and their columns. An allowlist: sorting by winner email would leak it.
LOT_ORDERING = {
    "lot_number": ("lot_number_int", "lot_number"),
    "lot_name": ("lot_name", "lot_number_int"),
    "price": ("winning_price", "lot_number_int"),
    "min_bid": ("reserve_price", "lot_number_int"),
    "date_posted": ("date_posted", "lot_number_int"),
    "date_end": ("date_end", "lot_number_int"),
    "category": ("species_category__name", "lot_number_int"),
}
DEFAULT_LOT_ORDERING = "lot_number"

#: One parameter, one column, substring match. ``?filter=`` is the one that looks everywhere.
LOT_TEXT_FILTERS = {
    "lot_name": "lot_name__icontains",
    "description": "summernote_description__icontains",
    "custom_field_1": "custom_field_1__icontains",
    # A controlled vocabulary (``lot_fields.custom_dropdown_options``), so the whole value.
    "custom_dropdown": "custom_dropdown__iexact",
}

#: Plain boolean columns. ``sold`` is a property, handled separately.
LOT_BOOLEAN_FILTERS = {
    "donation": "donation",
    "i_bred_this_fish": "i_bred_this_fish",
    "custom_checkbox": "custom_checkbox",
}

#: Where ``?filter=`` looks: **public columns only, for every caller**. Unlike ``LotAdminFilter``,
#: no seller names, or a public key could confirm a name letter by letter.
LOT_GENERIC_FILTER_COLUMNS = (
    "lot_name__icontains",
    "summernote_description__icontains",
    "custom_field_1__icontains",
    "custom_dropdown__icontains",
    "custom_lot_number__iexact",
    "species__scientific_name__icontains",
    "species__common_name__icontains",
    "species_category__name__icontains",
)

#: ``?seller=`` / ``?winner=``: name, bidder number or email. Refused without the privacy flag.
LOT_PERSON_FILTERS = {"seller": "auctiontos_seller", "winner": "auctiontos_winner"}


def _api_bool(value, name):
    """``?sold=true`` -> ``(True/False/None, error)``. Explicit spellings: ``bool("false")`` is True."""
    if value is None or str(value).strip() == "":
        return None, None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True, None
    if text in ("false", "0", "no"):
        return False, None
    return None, f"{name} must be true or false."


def _lot_fields_param(value):
    """``?fields=`` -> ``(set or None, error)``. An unknown name is an error, not a silent blank column."""
    if not value:
        return None, None
    wanted = [name.strip() for name in str(value).split(",") if name.strip()]
    if not wanted:
        return None, None
    known = set(ClubApiLotSerializer.Meta.fields)
    unknown = [name for name in wanted if name not in known]
    if unknown:
        return None, f"No such field on a lot: {', '.join(unknown)}. Available: {', '.join(sorted(known))}."
    return set(wanted), None


def _club_api_auctions(club):
    """Every auction filed under this club, promoted or not: the club's own key isn't a stranger."""
    return Auction.objects.filter(club=club, is_deleted=False)


def club_api_current_auction(club):
    """The auction this club is running or about to run, or ``None``: the pinned one if not wound down,
    else the soonest. Looser than ``_club_current_auction`` (public embed, promoted only).
    """
    pinned = club.current_auction
    if pinned and pinned.club_id == club.pk and not pinned.is_deleted and not pinned.pretty_much_over:
        return pinned
    window = timezone.now() - timedelta(days=CURRENT_AUCTION_WINDOW_DAYS)
    candidates = _club_api_auctions(club).filter(date_start__gte=window).order_by("date_start")[:20]
    return next((auction for auction in candidates if not auction.pretty_much_over), None)


def club_api_latest_auction(club):
    """The last auction this club created, whatever state it is in."""
    return _club_api_auctions(club).order_by("-date_posted", "-pk").first()


def _resolve_club_api_auction(club, identifier):
    """An auction slug, or ``current`` / ``latest``. A real slug wins."""
    identifier = (identifier or "").strip()
    auction = _club_api_auctions(club).filter(slug=identifier).first()
    if auction:
        return auction
    if identifier == "current":
        return club_api_current_auction(club)
    if identifier == "latest":
        return club_api_latest_auction(club)
    return None


def _lot_images_by_owner(lots):
    """A page of lots' images in one query, keyed on the owning lot (``use_images_from`` shares them)."""
    owners = {lot.use_images_from_id or lot.pk for lot in lots}
    images = {}
    if not owners:
        return images
    for image in LotImage.objects.filter(lot_number__in=owners).order_by("-is_primary", "createdon"):
        images.setdefault(image.lot_number_id, []).append(image)
    return images


def _auto_images_by_lot_name(auction, lots, images_by_owner):
    """``Lot.auto_image`` for a whole page at once: the same rule without the per-lot queries."""
    if not auction or not auction.auto_add_images:
        return {}
    names = set()
    for lot in lots:
        if any(image.is_primary for image in images_by_owner.get(lot.use_images_from_id or lot.pk, [])):
            continue
        if lot.user and not lot.user.userdata.auto_add_images:
            continue
        names.add(lot.lot_name)
    if not names:
        return {}
    found = {}
    candidates = (
        LotImage.objects.filter(
            (Q(lot_number__user__userdata__share_lot_images=True) | Q(lot_number__user__isnull=True)),
            lot_number__lot_name__in=names,
            lot_number__is_deleted=False,
            lot_number__banned=False,
            is_primary=True,
            lot_number__auction__created_by__pk__in=auction.auction_admins_pks,
        )
        .select_related("lot_number")
        .order_by("-lot_number__date_posted")
    )
    for image in candidates:
        found.setdefault(image.lot_number.lot_name, image)
    return found


class ClubAuctionReadMixin(ClubAPIViewMixin):
    """Permission and context for the read-only auction and lot endpoints: auction info, public lots,
    and the privacy flag, three separate decisions.
    """

    def auction_info_club(self):
        return self.require_club_permission(
            "permission_manage_auctions",
            "can_read_auction_info",
            "You do not have permission to read this club's auctions.",
        )

    def lot_info_club(self):
        return self.require_club_permission(
            "permission_manage_auctions",
            "can_read_public_lots",
            "You do not have permission to read the lots in this club's auctions.",
        )

    def may_read_private(self):
        if self.is_api_key_request():
            return self.request.api_key.can_read_private_lots
        return check_club_permission(self.request.user, self.get_club(), "permission_manage_auctions")

    def serializer_context(self, **extra):
        return {
            "request": self.request,
            "private": self.may_read_private(),
            # ?src= is what page-view tracking reads, so the club sees its website's traffic.
            "src": self.request.api_key.name if self.is_api_key_request() else "",
            **extra,
        }

    def get_auction_or_404(self, identifier):
        auction = _resolve_club_api_auction(self.get_club(), identifier)
        if not auction:
            raise Http404
        return auction


class ClubAuctionListAPIView(ClubAuctionReadMixin, APIView):
    """This club's auctions, newest first. ``GET …/auctions/?limit=&offset=``. Names ``current`` and
    ``latest``.
    """

    def get(self, request, slug):
        club = self.auction_info_club()
        try:
            limit = min(max(int(request.query_params.get("limit", 25)), 1), 100)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return Response({"error": "limit and offset must be whole numbers."}, status=400)
        auctions = _club_api_auctions(club).order_by("-date_posted", "-pk")
        total = auctions.count()
        page = auctions[offset : offset + limit]
        current = club_api_current_auction(club)
        latest = club_api_latest_auction(club)
        serializer = ClubApiAuctionSummarySerializer(page, many=True, context=self.serializer_context())
        return Response(
            {
                "count": total,
                "current": current.slug if current else None,
                "latest": latest.slug if latest else None,
                "results": serializer.data,
            }
        )


class ClubAuctionDetailAPIView(ClubAuctionReadMixin, APIView):
    """One auction: dates, rules, fees, pickup locations and lot fields. ``GET …/auctions/<identifier>/``"""

    def get(self, request, slug, identifier):
        self.auction_info_club()
        auction = self.get_auction_or_404(identifier)
        return Response(ClubApiAuctionSerializer(auction, context=self.serializer_context()).data)


class ClubAuctionLotListAPIView(ClubAuctionReadMixin, APIView):
    """The lots in one auction, in lot number order. ``GET …/auctions/<identifier>/lots/``

    ``?limit=`` ``?offset=`` ``?filter=`` (all public columns) ``?lot_name=`` ``?description=``
    ``?custom_field_1=`` ``?custom_dropdown=`` ``?lot_number=`` ``?category=`` ``?category_id=``
    ``?species_id=`` ``?sold=`` ``?donation=`` ``?i_bred_this_fish=`` ``?custom_checkbox=``
    ``?seller=`` / ``?winner=`` (privacy flag) ``?ordering=`` (:data:`LOT_ORDERING`) ``?fields=``

    Removed lots are left out unless the key can read private information.
    """

    def lot_queryset(self, auction):
        lots = Lot.objects.filter(auction=auction, is_deleted=False)
        if not self.may_read_private():
            lots = lots.exclude(banned=True)
        return lots.select_related(
            "species",
            "species__parent",
            "species_category",
            "auction",
            "auctiontos_seller",
            "auctiontos_winner",
            "user__userdata",
            "winner",
        ).order_by(*LOT_ORDERING[DEFAULT_LOT_ORDERING])

    def filtered_lots(self, auction, params):
        """Apply ``?filter=`` and friends. Returns ``(lots, error)``. An unparseable value is an error, or the
        page silently lists every lot.
        """
        lots = self.lot_queryset(auction)
        for name, column in LOT_TEXT_FILTERS.items():
            value = (params.get(name) or "").strip()
            if value:
                lots = lots.filter(**{column: value})
        generic = (params.get("filter") or "").strip()
        if generic:
            if generic.isdigit():
                # All digits is a lot number only; "1" in the text columns buries the lot. Use
                # ?description=1 for digits in prose.
                match = Q(lot_number_int=int(generic)) | Q(custom_lot_number__iexact=generic)
            else:
                match = Q()
                for column in LOT_GENERIC_FILTER_COLUMNS:
                    match |= Q(**{column: generic})
            lots = lots.filter(match)
        lot_number = (params.get("lot_number") or "").strip()
        if lot_number:
            # Both spellings: lot_number_int, and custom_lot_number from seller-dash numbering.
            match = Q(custom_lot_number__iexact=lot_number)
            if lot_number.isdigit():
                match |= Q(lot_number_int=int(lot_number))
            lots = lots.filter(match)
        category, error = _resolve_category(params.get("category"), params.get("category_id"))
        if error:
            return None, error
        if category:
            lots = lots.filter(species_category=category)
        species_id = (params.get("species_id") or "").strip()
        if species_id:
            if not species_id.isdigit():
                return None, "species_id must be a whole number: the id of a species on this site."
            lots = lots.filter(species_id=int(species_id))
        for name, field in LOT_BOOLEAN_FILTERS.items():
            value, error = _api_bool(params.get(name), name)
            if error:
                return None, error
            if value is not None:
                lots = lots.filter(**{field: value})
        sold, error = _api_bool(params.get("sold"), "sold")
        if error:
            return None, error
        if sold is not None:
            # Lot.sold is a property: a winner and a price.
            has_winner = Q(winning_price__isnull=False) & (Q(auctiontos_winner__isnull=False) | Q(winner__isnull=False))
            lots = lots.filter(has_winner) if sold else lots.exclude(has_winner)
        lots, error = self._filter_by_person(lots, params)
        if error:
            return None, error
        return self._ordered(lots, params.get("ordering"))

    def _filter_by_person(self, lots, params):
        """``?seller=`` / ``?winner=``. Refused without the privacy flag, so "no lots" never means "not allowed"."""
        for name, relation in LOT_PERSON_FILTERS.items():
            value = (params.get(name) or "").strip()
            if not value:
                continue
            if not self.may_read_private():
                return None, f"{name} needs a key that can read private lot information."
            lots = lots.filter(
                Q(**{f"{relation}__name__icontains": value})
                | Q(**{f"{relation}__bidder_number__iexact": value})
                | Q(**{f"{relation}__email__iexact": value})
            )
        return lots, None

    def _ordered(self, lots, ordering):
        """``?ordering=lot_name`` / ``-lot_name``.  Returns ``(lots, error)``."""
        ordering = (ordering or DEFAULT_LOT_ORDERING).strip()
        descending = ordering.startswith("-")
        key = ordering.lstrip("-")
        if key not in LOT_ORDERING:
            return None, f"ordering must be one of: {', '.join(sorted(LOT_ORDERING))} (prefix with - to reverse)."
        columns = LOT_ORDERING[key]
        if descending:
            columns = tuple(column[1:] if column.startswith("-") else f"-{column}" for column in columns)
        return lots.order_by(*columns), None

    def page_params(self, params):
        """``(limit, offset, fields, error)``.  ``fields`` is None for "all of them"."""
        try:
            limit = min(max(int(params.get("limit", LOT_PAGE_SIZE)), 1), MAX_LOT_PAGE_SIZE)
            offset = max(int(params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return None, None, None, "limit and offset must be whole numbers."
        fields, error = _lot_fields_param(params.get("fields"))
        return limit, offset, fields, error

    def lot_context(self, auction, page, fields):
        images = _lot_images_by_owner(page)
        return self.serializer_context(
            images_by_lot=images,
            auto_images=_auto_images_by_lot_name(auction, page, images),
            fields=fields,
        )

    def get(self, request, slug, identifier):
        self.lot_info_club()
        auction = self.get_auction_or_404(identifier)
        limit, offset, fields, error = self.page_params(request.query_params)
        if error:
            return Response({"error": error}, status=400)
        lots, error = self.filtered_lots(auction, request.query_params)
        if error:
            return Response({"error": error}, status=400)
        total = lots.count()
        page = list(lots[offset : offset + limit])
        context = self.lot_context(auction, page, fields)
        return Response(
            {
                "auction": auction.slug,
                "count": total,
                "results": ClubApiLotSerializer(page, many=True, context=context).data,
            }
        )


class ClubAuctionLotDetailAPIView(ClubAuctionLotListAPIView):
    """One lot, by its label number. ``GET …/auctions/<identifier>/lots/<lot number>/``"""

    def get(self, request, slug, identifier, lot_number):
        self.lot_info_club()
        auction = self.get_auction_or_404(identifier)
        fields, error = _lot_fields_param(request.query_params.get("fields"))
        if error:
            return Response({"error": error}, status=400)
        lots = self.lot_queryset(auction)
        # Both spellings of the number.
        lot = lots.filter(custom_lot_number=lot_number).first()
        if not lot and str(lot_number).isdigit():
            lot = lots.filter(lot_number_int=int(lot_number)).first()
        if not lot:
            raise Http404
        context = self.lot_context(auction, [lot], fields)
        return Response(ClubApiLotSerializer(lot, context=context).data)


#: Daily species lookups per club that may reach the language model. Per club, not key, so one busy
#: integration can't starve the club's others. Large because answers are cached site-wide.
SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 1000


def _species_llm_budget_headers(budget):
    """Remaining model allowance as headers, on every response, so callers can back off early."""
    return {
        "X-Species-LLM-Limit": str(budget.limit),
        "X-Species-LLM-Remaining": str(budget.remaining),
        "X-Species-LLM-Reset": budget.resets_at.isoformat(),
    }


def _resolve_category(name, raw_id):
    """``category=cichlids`` (by name) or ``category_id=10``. Returns ``(category, error)``; both None if
    not mentioned. Two parameters, since "2024" is a valid name. Unknown is an error.
    """
    # str() both: a JSON body can send a number, which must be a 400, not a 500.
    name = str(name if name is not None else "").strip()
    raw_id = str(raw_id if raw_id is not None else "").strip()
    if name and raw_id:
        return None, "Pass category or category_id, not both."
    if raw_id:
        if not raw_id.isdigit():
            return None, "category_id must be a whole number: the id of a category on this site."
        category = Category.objects.filter(pk=int(raw_id)).first()
        return (category, None) if category else (None, f"No category with id {raw_id} on this site.")
    if name:
        category = Category.objects.filter(name__iexact=name).first()
        return (category, None) if category else (None, f"No category called '{name}' on this site.")
    return None, None


class ClubSpeciesLookupAPIView(ClubAPIViewMixin, APIView):
    """Turn free text into a species from this site's list, and add missing ones.

    ``GET  …/species-lookup/?q=yellow%20lab`` and ``POST …/species-lookup/`` (add a species), both
    behind ``can_look_up_species``. The POST only creates, and what it creates is the club's until
    approved.

    Runs ``suggest_species`` exactly as the lot form does. ``results`` is a shortlist;
    ``unambiguous`` means exactly one, the only case the site acts on. ``source`` is ``exact``,
    ``cache``, ``search``, ``llm`` or ``none``. The club sees the shared list plus its own unapproved
    rows (a signed-in admin also sees their own). No match is a normal 200.

    Params: ``q`` (required; blank is the one 400), ``category`` or ``category_id`` (tie-break only;
    unknown is a 400). At most ``MAX_SUGGESTIONS`` results; ``total_matches`` says if there were more.

    The model costs one :data:`SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY` unit per round. Out of budget
    with nothing to show is a 429, since "no species" would be a lie that gets cached.
    """

    serializer_class = SpeciesMatchSerializer

    def get(self, request, slug):
        club = self.require_club_permission(
            "permission_view",
            "can_look_up_species",
            "You do not have permission to look up species for this club.",
        )
        # Before anything can fail, so 400s carry the budget headers too.
        budget = LLMBudget.for_club(club, SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY)
        headers = _species_llm_budget_headers(budget)
        query = (request.query_params.get("q") or "").strip()
        if not query:
            return Response(
                {"error": "q is required: the text to match, e.g. ?q=yellow lab."}, status=400, headers=headers
            )
        category, error = _resolve_category(
            request.query_params.get("category"), request.query_params.get("category_id")
        )
        if error:
            return Response({"error": error}, status=400, headers=headers)
        matches, source = suggest_species(
            query,
            # An API key has no person: no user for LLMUsage or the per-user budget.
            user=None if self.is_api_key_request() else request.user,
            # So the club's own unapproved species are visible to its software.
            club=club,
            use_llm=True,
            category=category,
            budget=budget,
        )
        # Rebuilt: the lookup may just have spent one of these.
        headers = _species_llm_budget_headers(budget)
        if not matches and budget.blocked:
            # Out of budget with nothing to show: never answered, so not a 200.
            retry_after = max(1, int((budget.resets_at - timezone.now()).total_seconds()))
            return Response(
                {
                    "error": (
                        f"This club has used its {budget.limit} language-model calls for today, "
                        "and the database could not answer this one.  Lookups the database can answer are "
                        "unaffected; this one is worth retrying after the allowance resets."
                    ),
                    "query": query,
                },
                status=429,
                headers={**headers, "Retry-After": str(retry_after)},
            )
        shown = matches[:MAX_SUGGESTIONS]
        # One query for every result's common names instead of one each.
        prefetch_related_objects(shown, "common_names")
        serializer = self.serializer_class(shown, many=True)
        return Response(
            {
                "query": query,
                "source": source,
                "unambiguous": len(matches) == 1,
                "total_matches": len(matches),
                "count": len(serializer.data),
                # Whether a model call was spent, not whether it found something.
                "llm": bool(budget.spent),
                "results": serializer.data,
            },
            headers=headers,
        )

    def post(self, request, slug):
        """Add a species not on the list yet. Create only (:class:`SpeciesCreateSerializer`).

        Stamped ``approved=False`` with this club until an admin approves it. An existing name is a 409
        with the existing row: two rows for one fish split breeder points.
        """
        club = self.require_club_permission(
            "permission_add_edit",
            "can_look_up_species",
            "You do not have permission to add species for this club.",
        )
        # A non-object body becomes "scientific_name is required", not a 500.
        data = request.data if hasattr(request.data, "get") else {}
        category, error = _resolve_category(data.get("category"), data.get("category_id"))
        if error:
            return Response({"error": error}, status=400)
        serializer = SpeciesCreateSerializer(data=data, club=club)
        serializer.is_valid(raise_exception=True)
        cleaned = serializer.validated_data
        existing = species_already_named(
            cleaned["genus"],
            cleaned["epithet"],
            cleaned["variety"],
            club=club,
            is_hybrid=cleaned.get("is_hybrid", False),
        )
        if existing:
            return Response(
                {
                    "error": f"{existing.label} is already on this site's list.  Use it instead of adding it again.",
                    "species": self.serializer_class(existing).data,
                },
                status=409,
            )
        species = serializer.save(
            club=club,
            # A key has no person to credit; the club stamp identifies it.
            added_by=None if self.is_api_key_request() else request.user,
            category=category,
        )
        return Response(self.serializer_class(species).data, status=201)


class ClubSpeciesCommonNameAPIView(ClubAPIViewMixin, APIView):
    """Add a common name to a species already on the list.

    ``POST …/species-lookup/<id or scientific name>/common-names/``

    Stamped ``source="admin"`` so FishBase re-imports keep it, and scoped to this club until approved.
    A strain needs its full name or id, since strains share ``scientific_name``. Create only: never
    edits names, ``Species.common_name`` or ``is_preferred``. An existing name on this species is a
    200; on a different species, a 409.
    """

    serializer_class = SpeciesMatchSerializer

    #: A strain as ``full_scientific_name`` writes it: "Neocaridina davidi 'Blue Dream'".
    _STRAIN_NAME = re.compile(r"""^(?P<species>.*?)\s*['"\u2018\u2019](?P<variety>.+?)['"\u2018\u2019]$""")

    def _find_species(self, identifier, club):
        """The species this URL names, or None.  Scoped exactly like the lookup."""
        visible = visible_species(None, club)
        identifier = (identifier or "").strip()
        if identifier.isdigit():
            return visible.filter(pk=int(identifier)).first()
        strain = self._STRAIN_NAME.match(identifier)
        if strain:
            # "Hybrid 'Tibee'" is printed for crosses, but "Hybrid" isn't a genus. See Species.is_hybrid.
            if strain.group("species").strip().lower() == "hybrid":
                return visible.filter(is_hybrid=True, variety__iexact=strain.group("variety")).first()
            return visible.filter(
                scientific_name__iexact=strain.group("species").strip(), variety__iexact=strain.group("variety")
            ).first()
        matches = list(visible.filter(scientific_name__iexact=identifier)[:25])
        if len(matches) > 1:
            # A bare binomial means the plain species, not its strains.
            matches = [species for species in matches if not species.variety]
        return matches[0] if len(matches) == 1 else None

    def post(self, request, slug, identifier):
        club = self.require_club_permission(
            "permission_add_edit",
            "can_look_up_species",
            "You do not have permission to add species for this club.",
        )
        species = self._find_species(identifier, club)
        if not species:
            msg = (
                "No species here with that id or scientific name.  A strain needs its full name, "
                "e.g. Neocaridina davidi 'Blue Dream'."
            )
            raise Http404(msg)
        serializer = SpeciesCommonNameCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        taken = species_carrying_common_name(name, club=club, exclude=species)
        if taken:
            return Response(
                {
                    "error": f"\u201c{name}\u201d is already the name for {taken.label}.",
                    "species": self.serializer_class(taken).data,
                },
                status=409,
            )
        # Normalised, and scoped to names this club can see, so another club's private name isn't ours.
        existing = (
            visible_common_names(None, club)
            .filter(species=species, name_normalized=normalize_species_name(name))
            .first()
        )
        created = existing is None
        if created:
            user = None if self.is_api_key_request() else request.user
            existing = SpeciesCommonName.objects.create(
                species=species,
                name=name[:255],
                language="English",
                # Never preferred: that would demote the source's designated name.
                is_preferred=False,
                source="admin",
                # Superusers add for everyone; anyone else, and every key, for this club.
                approved=bool(user and user.is_superuser),
                added_by=user,
                club=club,
            )
        return Response(
            {
                "created": created,
                "id": existing.pk,
                "name": existing.name,
                "approved": existing.approved,
                "species": self.serializer_class(species).data,
            },
            status=201 if created else 200,
        )
