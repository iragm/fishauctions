"""The club REST API: ``/api/v1/clubs/<slug>/…``.

Authenticated by a ``ClubAPIKey`` or a signed-in club admin, both through
``ClubAPIViewMixin.require_club_permission`` so the two callers go through one gate. Three rules
run through the whole module: everything naming a person is inside a ``private`` block that is
absent without the privacy flag, ``?filter=`` searches public columns only whoever sends it, and
``?ordering=`` is an allowlist rather than a pass-through to ``order_by``.
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
            # Log the failed attempt (with raw POST data) to club history so admins can diagnose it.
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
    """Renew a membership from an external system (a club's own website, a payment form, ...).

    POST the member's info; the member is looked up by email within the club and created if they
    are new, then their membership is renewed exactly as the Renew button on the member list does
    (same expiration math, club history, ledger entry, and confirmation email).  Responds with the
    full member record, including the new ``membership_expiration_date``.

    Any club member field the key is allowed to write may be sent along and is applied before the
    renewal, so a renewal doubles as a details refresh.  Blank values are ignored rather than
    wiping details already on file.
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
        # Blanks would otherwise overwrite details already on file (the CSV import's merge rule).
        data = {key: value for key, value in data.items() if value not in ("", None)}
        member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).order_by("pk").first()
        created = member is None
        serializer = ClubMemberAPIKeySerializer(instance=member, data=data, partial=not created)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception:
            # Same diagnostics as member create: an admin can see the rejected payload later.
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
    """Resolve the ``start``/``end``/``days`` query params into an aware datetime range.

    Bare dates are inclusive at both ends, so ``end=2026-08-08`` covers all of August 8th.
    Explicit ``start``/``end`` win over ``days``; with nothing given the range is the last
    ``BAP_LOT_DEFAULT_DAYS`` days.  Raises ValueError with a caller-facing message.
    """
    now = timezone.now()

    def parse_bound(name, *, end_of_day):
        raw = (params.get(name) or "").strip()
        if not raw:
            return None
        # Bare dates are checked first: parse_datetime() also accepts "2026-03-31" and would silently
        # turn an inclusive end date into midnight, dropping everything that happened that day.
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
    """List the lots from this club's auctions that ended in a date range.

    Feeds an external breeder award program: it gets the lot, who sold it, who bought it, and
    everything this site knows about whether the lot earns points, and does its own matching on the
    email addresses.  Unsold lots are included with empty winner fields, so the caller can tell
    "nobody bought it" from "not in this club's auctions".

    ``lot_id`` is the site's permanent id for the lot and never changes or gets reused, so a caller
    can key on it to avoid awarding points twice for the same lot across overlapping pulls.

    GET /api/v1/clubs/<slug>/bap-lots/?days=30
        ?days=N                   the last N days (default 30)
        ?start=YYYY-MM-DD         from this date/timestamp (inclusive)
        ?end=YYYY-MM-DD           through this date/timestamp (inclusive to end of day)
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
            # Don't echo the exception back: it can carry internal detail from the datetime parsers,
            # and the caller only needs to know the accepted shape of the params.
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


#: How far back to look for the auction a club would call its "current" one.  An auction that
#: started before this and still isn't wound down is not what anybody means by the question.
CURRENT_AUCTION_WINDOW_DAYS = 90

#: Default and ceiling for ``?limit=`` on the lot list.  A club auction of 400 lots is ordinary, so
#: the default is big enough that most callers never page at all.
LOT_PAGE_SIZE = 100
MAX_LOT_PAGE_SIZE = 500

#: What ``?ordering=`` accepts on the lot list, and the columns each name sorts on.  An allowlist
#: rather than a pass-through to ``order_by``: a caller that can name any column can order by
#: ``auctiontos_winner__email`` and read the auction's email list off the sort order, one binary
#: search at a time, without ever holding the private permission.
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

#: ``?lot_name=``, ``?description=`` and friends: one parameter, one column, substring match.  A
#: parameter named after a column matches that column and nothing else; ``?filter=`` is the one
#: that looks everywhere.
LOT_TEXT_FILTERS = {
    "lot_name": "lot_name__icontains",
    "description": "summernote_description__icontains",
    "custom_field_1": "custom_field_1__icontains",
    # The whole value rather than part of one: a dropdown is a controlled vocabulary, and the
    # auction publishes it as ``lot_fields.custom_dropdown_options``.
    "custom_dropdown": "custom_dropdown__iexact",
}

#: ``?donation=true``.  Every one of these is a plain column on Lot; ``sold`` is not, which is why
#: it is handled on its own.
LOT_BOOLEAN_FILTERS = {
    "donation": "donation",
    "i_bred_this_fish": "i_bred_this_fish",
    "custom_checkbox": "custom_checkbox",
}

#: Where ``?filter=`` looks.  **Public columns only, for every caller.**
#:
#: The admin's own lot filter (:class:`~auctions.filters.LotAdminFilter`) searches seller name,
#: username and bidder number too, and copying that list here would hand a public key a way to
#: confirm a name one character at a time without ever holding the private permission.  Seller and
#: winner get their own parameters instead (:data:`LOT_PERSON_FILTERS`), which *refuse* a key that
#: cannot read private information rather than quietly matching nothing -- so the same ``?filter=``
#: means the same thing whoever sends it.
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

#: ``?seller=`` / ``?winner=``: a name, a bidder number or an email address.  Behind the privacy
#: flag, because each one is a way of asking "is this person in this auction".
LOT_PERSON_FILTERS = {"seller": "auctiontos_seller", "winner": "auctiontos_winner"}


def _api_bool(value, name):
    """``?sold=true``.  Returns ``(True/False/None, error)``; None means the caller didn't ask.

    Spellings rather than Python truthiness, because ``bool("false")`` is True and a filter that
    reads "false" as "yes" is a bug nobody reports -- they just quietly get the wrong lots.
    """
    if value is None or str(value).strip() == "":
        return None, None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True, None
    if text in ("false", "0", "no"):
        return False, None
    return None, f"{name} must be true or false."


def _lot_fields_param(value):
    """``?fields=lot_number,lot_name,thumbnail``.  Returns ``(set or None, error)``.

    None means every field.  A name we don't have is an error rather than an omission: a typo that
    silently drops a column produces a page with a blank space in it and no clue why.
    """
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
    """Every auction filed under this club, promoted or not.

    Not filtered by ``promote_this_auction``: that flag says "this one is ready for strangers", and
    a key issued by the club's own admins is not a stranger.
    """
    return Auction.objects.filter(club=club, is_deleted=False)


def club_api_current_auction(club):
    """The auction this club is running or about to run, or ``None``.

    The pinned ``current_auction`` first, because an admin chose it on purpose; otherwise the
    soonest one that hasn't wound down.  Deliberately looser than
    :func:`_club_current_auction`, which the public website embed uses and which will only ever
    offer a *promoted* auction: this answers the club's own software, which has to be able to build
    a page for an auction before it is announced.
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
    """An auction slug, or one of the two words ``current`` and ``latest``.

    A real slug wins, so a club that manages to call an auction "Latest" can still reach it; the
    two words are only ever a fallback.
    """
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
    """Every image belonging to a page of lots, in one query, keyed on the lot that owns it.

    Keyed on the owner rather than the lot because of ``use_images_from``: a lot can borrow another
    lot's pictures, and both then read the same list.
    """
    owners = {lot.use_images_from_id or lot.pk for lot in lots}
    images = {}
    if not owners:
        return images
    for image in LotImage.objects.filter(lot_number__in=owners).order_by("-is_primary", "createdon"):
        images.setdefault(image.lot_number_id, []).append(image)
    return images


def _auto_images_by_lot_name(auction, lots, images_by_owner):
    """The pictures this site would auto-add, for the lots on this page that have none of their own.

    :attr:`~auctions.models.Lot.auto_image` does this one lot at a time and costs several queries
    each; a lot list cannot afford that, so this is the same rule -- the newest primary image on a
    lot of the same name in an auction run by this auction's admins, from a seller who shares -- run
    once for the whole page.
    """
    if not auction or not auction.auto_add_images:
        return {}
    names = set()
    for lot in lots:
        if any(image.is_primary for image in images_by_owner.get(lot.use_images_from_id or lot.pk, [])):
            continue
        # The seller's own "don't put other people's pictures on my lots" setting.
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
    """Shared permission and context plumbing for the read-only auction and lot endpoints.

    Three separate permissions, because they are three different decisions: reading the auction's
    dates and rules, reading the lots in it, and reading who bought and sold them.  The third is
    the privacy flag -- without it the ``private`` object is not in the response at all, so a key
    handed to a public web page has nothing to leak.
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
            # The name of the key doing the reading rides on every lot link as ?src=, which is the
            # parameter this site's own page-view tracking reads: a club that publishes this feed
            # then sees the traffic its website sent in its auction stats.
            "src": self.request.api_key.name if self.is_api_key_request() else "",
            **extra,
        }

    def get_auction_or_404(self, identifier):
        auction = _resolve_club_api_auction(self.get_club(), identifier)
        if not auction:
            raise Http404
        return auction


class ClubAuctionListAPIView(ClubAuctionReadMixin, APIView):
    """This club's auctions, newest first.

    ``GET /api/v1/clubs/<slug>/auctions/``
        ``?limit=`` / ``?offset=``

    ``current`` and ``latest`` name the two auctions worth asking for by name, and are the words
    the detail and lot endpoints take in place of a slug.
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
    """One auction: dates, rules, fees, pickup locations and which lot fields it uses.

    ``GET /api/v1/clubs/<slug>/auctions/<auction slug, or current, or latest>/``
    """

    def get(self, request, slug, identifier):
        self.auction_info_club()
        auction = self.get_auction_or_404(identifier)
        return Response(ClubApiAuctionSerializer(auction, context=self.serializer_context()).data)


class ClubAuctionLotListAPIView(ClubAuctionReadMixin, APIView):
    """The lots in one auction, in lot number order.

    ``GET /api/v1/clubs/<slug>/auctions/<auction slug, or current, or latest>/lots/``
        ``?limit=`` / ``?offset=``
        ``?filter=`` -- one box that searches every public column
        ``?lot_name=`` / ``?description=`` / ``?custom_field_1=`` / ``?custom_dropdown=``
        ``?lot_number=`` / ``?category=`` / ``?category_id=`` / ``?species_id=``
        ``?sold=`` / ``?donation=`` / ``?i_bred_this_fish=`` / ``?custom_checkbox=``
        ``?seller=`` / ``?winner=`` -- needs the privacy flag
        ``?ordering=`` -- see :data:`LOT_ORDERING`, ``-`` for descending
        ``?fields=`` -- only these keys on each lot

    Removed lots are left out, unless the key can read private information -- a club republishing
    this feed is publishing the lot list, and a lot an admin pulled is not on it.
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
        """The queryset with ``?filter=`` and friends applied.  Returns ``(lots, error)``.

        A parameter we don't recognise the *value* of is an error rather than a shrug: a filter
        that silently does nothing shows up as a page that quietly lists every lot in the auction,
        which is exactly the mistake nobody notices until it is on the club's front page.
        """
        lots = self.lot_queryset(auction)
        for name, column in LOT_TEXT_FILTERS.items():
            value = (params.get(name) or "").strip()
            if value:
                lots = lots.filter(**{column: value})
        generic = (params.get("filter") or "").strip()
        if generic:
            if generic.isdigit():
                # A number typed into a search box is a lot number.  Running it through the text
                # columns as well is what makes a generic search useless: "1" appears in "10
                # gallon" and in half the descriptions in the auction, so the one lot the person
                # was looking for arrives buried in sixty others.  ?description=1 is still there
                # for somebody who really does want digits in the prose.
                match = Q(lot_number_int=int(generic)) | Q(custom_lot_number__iexact=generic)
            else:
                match = Q()
                for column in LOT_GENERIC_FILTER_COLUMNS:
                    match |= Q(**{column: generic})
            lots = lots.filter(match)
        lot_number = (params.get("lot_number") or "").strip()
        if lot_number:
            # Both spellings, like the single-lot endpoint: lot_number_int is what almost every
            # auction numbers with, custom_lot_number is what seller-dash numbering writes.
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
            # Lot.sold is a property (a winner *and* a price), so it is spelled out here rather
            # than filtered on one column -- a lot with a winner and no price is not sold.
            has_winner = Q(winning_price__isnull=False) & (Q(auctiontos_winner__isnull=False) | Q(winner__isnull=False))
            lots = lots.filter(has_winner) if sold else lots.exclude(has_winner)
        lots, error = self._filter_by_person(lots, params)
        if error:
            return None, error
        return self._ordered(lots, params.get("ordering"))

    def _filter_by_person(self, lots, params):
        """``?seller=`` / ``?winner=`` -- a name, a bidder number or an email address.

        Refused outright without the privacy flag rather than quietly matching nothing: "no lots"
        and "you may not ask" are different answers, and a caller that cannot tell them apart will
        read the first as the second.
        """
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
    """One lot, by the number people read off its label.

    ``GET /api/v1/clubs/<slug>/auctions/<auction slug, or current, or latest>/lots/<lot number>/``
    """

    def get(self, request, slug, identifier, lot_number):
        self.lot_info_club()
        auction = self.get_auction_or_404(identifier)
        fields, error = _lot_fields_param(request.query_params.get("fields"))
        if error:
            return Response({"error": error}, status=400)
        lots = self.lot_queryset(auction)
        # Both spellings of the number: lot_number_int is the one almost every auction uses, and
        # custom_lot_number is what seller-dash numbering ("101-1") writes instead.
        lot = lots.filter(custom_lot_number=lot_number).first()
        if not lot and str(lot_number).isdigit():
            lot = lots.filter(lot_number_int=int(lot_number)).first()
        if not lot:
            raise Http404
        context = self.lot_context(auction, [lot], fields)
        return Response(ClubApiLotSerializer(lot, context=context).data)


#: Daily ceiling on species lookups from one club that are allowed to reach the language model.
#:
#: A club rather than a key: a club that issues three keys still gets one bill, and one busy
#: integration must not be able to switch the model off for the club's other software.
#:
#: Large on purpose, because almost nothing spends it.  A lookup only reaches the model after the
#: exact, cache and search steps have all failed, and every model answer -- including "this is not
#: a species" -- is written to a cache every club reads, so a name costs one call ever, site-wide.
#: A thousand a day is therefore a thousand *names nobody on this site has ever looked up*, which
#: is not a number a club reaches twice.
SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY = 1000


def _species_llm_budget_headers(budget):
    """What is left of this club's daily model allowance, for a species-lookup response.

    Headers rather than a field in the body, so a caller can back off without parsing the answer,
    and on every response rather than only the ones that spent something: an integration that
    first reads the number when it is already being refused has read it too late.
    """
    return {
        "X-Species-LLM-Limit": str(budget.limit),
        "X-Species-LLM-Remaining": str(budget.remaining),
        "X-Species-LLM-Reset": budget.resets_at.isoformat(),
    }


def _resolve_category(name, raw_id):
    """``category=cichlids`` (by name, any case) or ``category_id=10``.  Returns ``(category, error)``.

    Both may be None, which is what "the caller didn't mention a category" looks like.

    Two parameters rather than one that works out which was meant: "2024" is a perfectly good name
    for a category, and anything deciding by looking at the characters will one day decide wrong.
    A category we cannot find is an error rather than a shrug -- it only ever re-orders candidates,
    so ignoring a typo in it would go unnoticed for months.
    """
    # Both are str()ed rather than trusted: a query parameter is always a string, but a JSON body
    # can perfectly well send {"category": 12}, and that must be a 400 about a category rather
    # than a 500 about .strip().
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
    """Turn free text into a species from this site's list, and add the ones it is missing.

    ``GET  /api/v1/clubs/<slug>/species-lookup/?q=yellow%20lab``
    ``POST /api/v1/clubs/<slug>/species-lookup/`` -- add a species

    One permission, ``can_look_up_species``, covers both, and the write is safe to hand out with
    the read because of what it cannot do: it only ever creates, and what it creates is this
    club's until a site admin approves it.

    The same matcher the add-lot form runs -- :func:`auctions.species_matching.suggest_species`,
    called exactly as ``SpeciesSuggestions`` calls it -- so a club's own website, membership system
    or breeder-award program files a name the way this site would file it, and the two agree about
    what a lot is.  Nothing the matcher returns is invented: every answer is a row in the species
    table, and the way to add a row is the POST rather than a cleverer matcher.

    Be as conservative reading the answer as the matcher is producing it.  ``results`` is a
    shortlist, not a decision.  ``unambiguous`` is true only when the matcher came back with
    exactly one species, which is the same signal the site itself trusts: the lot form fills the
    field in for the user only on one answer, and ``backfill_lot_species`` writes to old lots only
    on one answer.  ``source`` says how it was found, most trustworthy first -- ``exact`` (the text
    *is* a scientific or common name), ``cache`` (a remembered answer), ``search`` (token/phrase
    matching), ``llm`` (a language model picked from a shortlist we built), ``none``.

    **What this club can see** is everything on the shared list plus its own unapproved rows --
    the ones its admins added at a check-in table and the ones its keys POSTed here, which stay
    the club's until a site admin approves them for everybody.  That is
    :func:`~auctions.species_matching.visible_species` with this club passed in, and ``approved``
    on each result says which kind a row is.  A *signed-in* admin browsing the same URL is also a
    person, so they additionally see anything they added themselves or that belongs to another
    club of theirs -- the site-wide rule, and the one thing that can make a browser's answer
    slightly wider than the key's.

    **No match is a normal answer**, not an error: 200 with an empty ``results``.  Most lots are
    not a species -- "sponge filter", "assorted plants", "10 gallon tank" -- and a matcher that
    always finds something would be putting wrong species on labels and wrong points in a breeder
    award program.

    Params:
        ``q``            the text to match.  Required; blank is the one 400.
        ``category``     a category *name*, matched case-insensitively.
        ``category_id``  a category id, as the lot form has one to hand.  Either form only breaks
                         a tie between candidates that already matched -- neither can filter --
                         and a name or id this site doesn't have is a 400 rather than a shrug.

    At most :data:`~auctions.species_matching.MAX_SUGGESTIONS` candidates come back; a bare genus
    can match more than that and ``total_matches`` says so, which is the number to look at before
    trusting a picklist.

    The language model runs on every lookup the database could not answer, which is the whole
    point of asking a matcher rather than querying the species table yourself.  It is bounded by
    what it costs rather than by asking permission per request: the request has to get past the
    exact, cache and search steps to reach it, and the club spends one of
    :data:`SPECIES_LOOKUP_LLM_CALLS_PER_CLUB_PER_DAY` when it does.  Every answer, "this is not a
    species" included, goes to ``SpeciesSearchCache``, so a name costs one call ever for the whole
    site.  ``X-Species-LLM-Remaining`` on every response is the number to back off on; a lookup
    that needed the model with nothing left is the one 429, because answering it "no species"
    would be a lie that then gets cached.
    """

    serializer_class = SpeciesMatchSerializer

    def get(self, request, slug):
        club = self.require_club_permission(
            "permission_view",
            "can_look_up_species",
            "You do not have permission to look up species for this club.",
        )
        # Built before anything can fail, so the allowance is on the 400s too: a caller reading
        # the header on every response should not have to make a *valid* request to see it.
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
            # An API key authenticates a script, not a person: request.user is anonymous, and
            # LLMUsage rows and the matcher's own per-user budget both want a real user or none.
            user=None if self.is_api_key_request() else request.user,
            # The club whose key or admin is asking, so a species one of its admins added but
            # nobody has approved is visible to its own software.
            club=club,
            use_llm=True,
            category=category,
            budget=budget,
        )
        # Rebuilt: the lookup may just have spent one of these.
        headers = _species_llm_budget_headers(budget)
        if not matches and budget.blocked:
            # Out of budget *and* nothing to show.  Not 200-with-no-results: this lookup was never
            # actually answered, and a caller that wrote down "no species" would be writing down
            # something the site never said.  Lookups the database can answer keep working.
            retry_after = max(1, int((budget.resets_at - timezone.now()).total_seconds()))
            return Response(
                {
                    "error": (
                        f"This club has used its {budget.limit} language-model species lookups for today, "
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
                # Exactly one answer is the only case the site itself acts on unprompted.
                "unambiguous": len(matches) == 1,
                "total_matches": len(matches),
                "count": len(serializer.data),
                # Whether this request cost a model call, not whether the model found something:
                # "not a species" is an answer the model was paid for too.
                "llm": bool(budget.spent),
                "results": serializer.data,
            },
            headers=headers,
        )

    def post(self, request, slug):
        """Add a species that isn't on the list yet.  Create only -- see :class:`SpeciesCreateSerializer`.

        The club API's half of ``/species/new/``, which is the same job for a person: somebody is
        selling a fish this site has never heard of, and "email the site owner" ends in a lot with
        no scientific name on its label.  What a key adds is ``approved=False`` and stamped with
        this club, so it is offered to this club and to nobody else until a site admin approves it.

        A name that is already on the list is a 409 carrying the row that already has it, because
        the answer to "add *Poecilia reticulata*" is always "use the one that exists", never a
        second copy of it -- two rows for one fish is how breeder points end up split in half.
        """
        club = self.require_club_permission(
            "permission_add_edit",
            "can_look_up_species",
            "You do not have permission to add species for this club.",
        )
        # A body that isn't an object at all -- a bare list, a string -- has no fields to read, so
        # it goes to the serializer as nothing and comes back as "scientific_name is required"
        # rather than as a 500 about .get().
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
            # A key is a script; there is no person to credit.  The club is what the row is stamped
            # with, and what a superuser sees when approving it.
            added_by=None if self.is_api_key_request() else request.user,
            category=category,
        )
        return Response(self.serializer_class(species).data, status=201)


class ClubSpeciesCommonNameAPIView(ClubAPIViewMixin, APIView):
    """Add a common name to a species that is already on the list.

    ``POST /api/v1/clubs/<slug>/species-lookup/<id or scientific name>/common-names/``

    This is the table the hobby's own vocabulary lives in.  FishBase is an ichthyology database:
    it is authoritative about which species exist and has no reason to know that *Labidochromis
    caeruleus* is a "yellow lab", so that name has to be ours.  It is stamped ``source="admin"``,
    which is what makes it survive the next FishBase re-import -- every importer deletes only the
    names it wrote itself -- and scoped to this club until a site admin approves it, exactly like
    a species.

    Named by **id or by scientific name**, because a caller matching free text has a name and not
    an id, and making them look the id up first would be two calls to do one thing.  A strain
    needs its full name ("Neocaridina davidi 'Blue Dream'") or its id: the plain species and all
    thirteen of its colour strains carry the same ``scientific_name``.

    Create only.  It never edits or removes a name that is already there, never touches
    ``Species.common_name``, and never claims another source's ``is_preferred``.  Sending a name
    the species already has is not an error -- 200 with the row that exists, so a club can re-run
    its import without thinking about it.  A name that already names a *different* species is a
    409: one name on two species turns an unambiguous lookup into a picklist, so it is the loss of
    a name rather than the gain of one.
    """

    serializer_class = SpeciesMatchSerializer

    #: "Neocaridina davidi 'Blue Dream'" -- a strain as ``full_scientific_name`` writes it, which
    #: is the string every response shows and therefore the one a caller has to hand.
    _STRAIN_NAME = re.compile(r"""^(?P<species>.*?)\s*['"\u2018\u2019](?P<variety>.+?)['"\u2018\u2019]$""")

    def _find_species(self, identifier, club):
        """The species this URL names, or None.  Scoped exactly like the lookup."""
        visible = visible_species(None, club)
        identifier = (identifier or "").strip()
        if identifier.isdigit():
            return visible.filter(pk=int(identifier)).first()
        strain = self._STRAIN_NAME.match(identifier)
        if strain:
            # "Hybrid 'Tibee'" is what full_scientific_name prints for a cross, so it is what a
            # caller has to hand -- but "Hybrid" is not a genus and there is no such scientific
            # name to match on.  See Species.is_hybrid.
            if strain.group("species").strip().lower() == "hybrid":
                return visible.filter(is_hybrid=True, variety__iexact=strain.group("variety")).first()
            return visible.filter(
                scientific_name__iexact=strain.group("species").strip(), variety__iexact=strain.group("variety")
            ).first()
        matches = list(visible.filter(scientific_name__iexact=identifier)[:25])
        if len(matches) > 1:
            # A strain carries its parent's name, so a bare "Neocaridina davidi" is the plain
            # species and its strains all at once.  The plain species is what was meant.
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
        # Matched on the normalised column, because that is what every lookup matches on:
        # "Adolf's catfish" and "adolfs catfish" are the same name here.  Scoped to the names this
        # club can see, so another club's private name for the same fish is not mistaken for ours.
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
                # Never preferred: that would demote the name the source designates, which is an
                # edit to somebody else's row rather than a name of our own.
                is_preferred=False,
                source="admin",
                # A superuser is adding to everybody's vocabulary and knows it.  Anyone else --
                # and every key -- is adding this club's word for it.  Same rule as a species.
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
