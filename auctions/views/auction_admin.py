"""Setting an auction up, and running the room: pickup locations, users, check-in.

The auction admin's own pages -- editing the auction, its custom fields and dropdowns, the list of
people in it, the barcode scanner and the check-in screens. ``AuctionStats`` is the dashboard those
pages hang off; the JSON behind its charts is in :mod:`auctions.views.auction_stats`.
"""

import logging
from datetime import datetime
from datetime import timezone as date_tz

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    Prefetch,
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import DetailView, ListView, TemplateView, View
from django.views.generic.edit import (
    CreateView,
    DeleteView,
    UpdateView,
)
from pytz import timezone as pytz_timezone
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.filters import (
    AuctionHistoryFilter,
    AuctionTOSFilter,
    LotAdminFilter,
)
from auctions.forms import (
    AuctionCustomFieldsForm,
    AuctionEditForm,
    PickupLocationForm,
)
from auctions.models import (
    CUSTOM_DROPDOWN_MAX_LENGTH,
    Auction,
    AuctionDropdown,
    AuctionHistory,
    AuctionTOS,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PickupLocation,
)
from auctions.services import (
    check_in_auctiontos,
    draw_door_prize,
    promoting_makes_it_the_clubs_current_auction,
)
from auctions.tables import (
    AuctionHistoryHTMxTable,
    AuctionTOSHTMxTable,
    LotHTMxTable,
)

from .auction_pages import _add_club_admins_as_auction_tos
from .base import (
    _UNSET,
    AuctionViewMixin,
    HTMxTableView,
    _upsert_clubmember_shadow_tos,
    check_club_permission,
    close_modal_response,
)

logger = logging.getLogger(__name__)
# return HttpResponse(f"Max bid: ${self.lot.max_bid: .2f}")


class PickupLocations(LoginRequiredMixin, AuctionViewMixin, ListView):
    """Show all pickup locations belonging to the current auction"""

    model = PickupLocation
    template_name = "all_pickup_locations.html"
    ordering = ["name"]

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
    #     if not self.auction:
    #         raise Http404
    #     self.is_auction_admin
    #     return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = PickupLocation.objects.filter(
            auction=self.auction,
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class PickupLocationsDelete(LoginRequiredMixin, AuctionViewMixin, DeleteView):
    model = PickupLocation

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.success_url = reverse("auction_pickup_location", kwargs={"slug": self.auction.slug})
        if self.get_object().auction.location_qs.count() < 2:
            self.success_url = reverse("auction_main", kwargs={"slug": self.auction.slug})
            messages.error(request, "You can't delete the only pickup location in this auction")
            return redirect(self.success_url)
        if self.get_object().number_of_users:
            messages.error(
                request,
                "There are already users that have selected this location, it can't be deleted",
            )
            return redirect(self.success_url)
        if not self.is_auction_admin:
            messages.error(request, "You don't have permission to delete a pickup location")
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return self.success_url

    def form_valid(self, form):
        self.auction.create_history(
            applies_to="RULES", action=f"Deleted location {self.object}", user=self.request.user
        )
        return super().form_valid(form)


class PickupLocationForm:
    """Base form for create and update"""

    model = PickupLocation
    template_name = "location_form.html"
    form_class = PickupLocationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["auction"] = self.auction
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        return kwargs

    def get_success_url(self):
        data = self.request.GET.copy()
        next_url = data.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        if self.auction.is_online:
            return reverse("auction_pickup_location", kwargs={"slug": self.auction.slug})
        else:
            return self.auction.get_absolute_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def form_valid(self, form):
        location = form.save(commit=False)
        location.user = self.request.user
        location.auction = self.auction
        if not location.name:
            location.name = str(location.auction)
        if not location.pickup_time:
            location.users_must_coordinate_pickup = True
        if form.cleaned_data.get("mail_or_not") == "False":
            location.pickup_by_mail = False
        else:
            location.pickup_by_mail = True
        location.save()
        return super().form_valid(form)


class PickupLocationsUpdate(LoginRequiredMixin, AuctionViewMixin, PickupLocationForm, UpdateView):
    """Edit pickup locations"""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_edit_form"] = True
        kwargs["pickup_location"] = self.get_object()
        return kwargs

    def get(self, *args, **kwargs):
        users = AuctionTOS.objects.filter(pickup_location=self.get_object().pk).count()
        if users:
            messages.info(
                self.request,
                f"{users} users have already selected this as a pickup location.  Don't make large changes!",
            )
        return super().get(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        self.auction = self.get_object().auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        if form.has_changed():
            self.auction.create_history(
                applies_to="RULES",
                action=f"Edited location {self.get_object()}",
                user=self.request.user,
            )
        form = super().form_valid(form)
        messages.info(self.request, "Updated location")
        return form


class PickupLocationsCreate(LoginRequiredMixin, AuctionViewMixin, PickupLocationForm, CreateView):
    """Create a new pickup location"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_edit_form"] = False
        kwargs["pickup_location"] = None
        return kwargs

    def form_valid(self, form, **kwargs):
        form = super().form_valid(form)
        self.auction.create_history(
            applies_to="RULES",
            action=f"Added {self.object}",
            user=self.request.user,
        )
        # If this auction is associated with a club, ensure club admin members have AuctionTOS records.
        # This handles new auctions (first location created) and copied auctions with an inherited club.
        _add_club_admins_as_auction_tos(self.auction, self.request.user)
        return form


class AuctionUpdate(LoginRequiredMixin, AuctionViewMixin, UpdateView):
    """The form users fill out to edit an auction"""

    model = Auction
    template_name = "auction_edit_form.html"
    form_class = AuctionEditForm

    def get_success_url(self):
        return "/auctions/" + str(self.kwargs["slug"])

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["user"] = self.request.user
        kwargs["cloned_from"] = None
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        return kwargs

    def get_context_data(self, **kwargs):
        existing_lots = Lot.objects.exclude(is_deleted=True).filter(auction=self.get_object()).count()
        if existing_lots:
            messages.info(
                self.request,
                "Lots have already been added to this auction.  Don't make large changes!",
            )
        context = super().get_context_data(**kwargs)
        context["title"] = f"{self.auction}"
        context["is_online"] = self.auction.is_online
        return context

    def form_valid(self, form, **kwargs):
        # Server-side club permission check: only allow associating with clubs the user
        # has admin/edit/manage_auctions permission in (or the club already saved).
        new_club = form.cleaned_data.get("club")
        if new_club:
            auction = self.get_object()
            current_club_id = auction.club_id
            if new_club.pk != current_club_id:
                # User is changing the club — verify they have permission in the new club
                has_permission = (
                    self.request.user.is_superuser
                    or check_club_permission(self.request.user, new_club, "permission_manage_auctions")
                    or check_club_permission(self.request.user, new_club, "permission_edit_club")
                    or check_club_permission(self.request.user, new_club, "permission_admin")
                )
                if not has_permission:
                    form.add_error("club", "You don't have permission to associate this auction with that club.")
                    return self.form_invalid(form)
        if form.has_changed():
            self.get_object().create_history(applies_to="RULES", user=self.request.user, form=form)
        was_promoted = self.get_object().promote_this_auction
        try:
            form = super().form_valid(form)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        # Promoting a previously-unpromoted auction makes it the club's current auction.
        updated_auction = self.get_object()
        if promoting_makes_it_the_clubs_current_auction(updated_auction, was_promoted):
            messages.info(self.request, f"This is now the current auction for {updated_auction.club.name}.")
        if (
            not self.get_object().is_online
            and self.get_object().online_bidding == "buy_now_only"
            and self.get_object().buy_now == "disable"
        ):
            messages.info(
                self.request,
                "You've enabled online buy now with no bidding, but buy now isn't enabled.  Sellers won't be able to set a buy now price.",
            )
        elif not self.get_object().is_online and self.get_object().online_bidding != "disable" and settings.ENABLE_HELP:
            messages.info(
                self.request,
                format_html(
                    "This auction allows online bidding -- make sure to <a href='{}'>watch the tutorial in the help</a> to see how this works",
                    reverse("auction_help", kwargs={"slug": self.get_object().slug}),
                ),
                extra_tags="safe",
            )
        if (
            self.get_object().buy_now == "allow" or self.get_object().buy_now == "required"
        ) and "buy_now_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                format_html(
                    "Buy now is enabled, but labels are not set to print a buy now price. <a href='{}'>You should enable printing buy now on labels here.</a>",
                    reverse("auction_label_config", kwargs={"slug": self.get_object().slug}),
                ),
                extra_tags="safe",
            )
        if (
            self.get_object().reserve_price == "allow" or self.get_object().reserve_price == "required"
        ) and "min_bid_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                format_html(
                    "Minimum bid is enabled, but labels are not set to print a minimum bid. <a href='{}'>You should enable printing minimum bids on labels here.</a>",
                    reverse("auction_label_config", kwargs={"slug": self.get_object().slug}),
                ),
                extra_tags="safe",
            )
        if (
            self.get_object().use_check_in_mode
            and not self.get_object().is_online
            and self.get_object().online_bidding != "disable"
            and self.get_object().date_online_bidding_starts
            and self.get_object().date_online_bidding_starts < self.get_object().date_start
        ):
            messages.info(
                self.request,
                "This auction uses check-in mode, so users can't bid until they've been checked in at the event.  "
                "Online bidding is set to open before the auction starts, but no one will be able to bid online "
                "until they've been checked in.",
            )

        # some checks to warn if an important time is set for midnight (00:00)
        user_tz = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        try:
            user_tz = pytz_timezone(user_tz)
        except Exception:  # Catch any invalid timezone errors
            user_tz = pytz_timezone(settings.TIME_ZONE)
        if self.get_object().is_online:
            time_value = self.get_object().date_end
        else:
            time_value = self.get_object().date_start
        localized_time = time_value.astimezone(user_tz)
        if localized_time.hour == 0 and localized_time.minute == 0:
            messages.info(
                self.request,
                f"Don't set your {'end' if self.get_object().is_online else 'start'} time to midnight, users will find it confusing.  Use 23:59 instead.",
            )

        # If club was just set (or changed), auto-add club admins as auction TOS admins
        new_club = self.get_object().club
        if new_club:
            _add_club_admins_as_auction_tos(self.get_object(), self.request.user)

        return form


class AuctionCustomFieldsUpdate(LoginRequiredMixin, AuctionViewMixin, UpdateView):
    model = Auction
    template_name = "auction_custom_fields_form.html"
    form_class = AuctionCustomFieldsForm

    def get_success_url(self):
        return "/auctions/" + str(self.kwargs["slug"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"{self.auction} - Custom fields"
        context["auction"] = self.auction
        context["dropdown_options"] = AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon")
        context["custom_dropdown_max_length"] = CUSTOM_DROPDOWN_MAX_LENGTH
        return context

    def form_valid(self, form, **kwargs):
        if form.has_changed():
            self.get_object().create_history(applies_to="RULES", user=self.request.user, form=form)
        if getattr(form, "custom_dropdown_auto_disabled", False):
            messages.error(
                self.request, "Custom dropdown requires a name and at least two options. It has been disabled."
            )
        return super().form_valid(form)


class AuctionDropdownOptionsAPI(APIView, AuctionViewMixin):
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        # APIView.dispatch doesn't call AuctionViewMixin.dispatch, so set self.auction here
        # so self.is_auction_admin is available in the handlers below.
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug", ""), is_deleted=False)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        options = list(
            AuctionDropdown.objects.filter(auction=self.auction)
            .order_by("createdon")
            .values("id", "value", "user_id", "createdon")
        )
        return JsonResponse({"options": options})

    def post(self, request, *args, **kwargs):
        if not self.is_auction_admin:
            return HttpResponseForbidden()
        action = request.POST.get("action")
        value = (request.POST.get("value") or "").strip()
        option_id = request.POST.get("option_id")

        if action == "create":
            if not value:
                return JsonResponse({"success": False, "error": "Option value is required"})
            if len(value) > CUSTOM_DROPDOWN_MAX_LENGTH:
                return JsonResponse(
                    {"success": False, "error": f"Option value must be {CUSTOM_DROPDOWN_MAX_LENGTH} characters or less"}
                )
            if AuctionDropdown.objects.filter(auction=self.auction, value__iexact=value).exists():
                return JsonResponse({"success": False, "error": "That option already exists"})
            option = AuctionDropdown.objects.create(auction=self.auction, user=request.user, value=value)
            return JsonResponse({"success": True, "option": {"id": option.pk, "value": option.value}})

        if not option_id:
            return JsonResponse({"success": False, "error": "Option id is required"})
        option = AuctionDropdown.objects.filter(pk=option_id, auction=self.auction).first()
        if not option:
            return JsonResponse({"success": False, "error": "Option not found"})
        option.user = request.user

        if action == "update":
            if not value:
                return JsonResponse({"success": False, "error": "Option value is required"})
            if len(value) > CUSTOM_DROPDOWN_MAX_LENGTH:
                return JsonResponse(
                    {"success": False, "error": f"Option value must be {CUSTOM_DROPDOWN_MAX_LENGTH} characters or less"}
                )
            duplicate = AuctionDropdown.objects.filter(auction=self.auction, value__iexact=value).exclude(pk=option.pk)
            if duplicate.exists():
                return JsonResponse({"success": False, "error": "That option already exists"})
            option.value = value
            option.save()
            return JsonResponse({"success": True, "option": {"id": option.pk, "value": option.value}})
        if action == "delete":
            option.delete()
            return JsonResponse({"success": True})
        return JsonResponse({"success": False, "error": "Invalid action"})


class AuctionHistoryView(LoginRequiredMixin, AuctionViewMixin, HTMxTableView):
    model = AuctionHistory
    table_class = AuctionHistoryHTMxTable
    filterset_class = AuctionHistoryFilter
    template_name = "auctions/auction_history.html"

    def get_queryset(self):
        # the table prints who did each thing
        return AuctionHistory.objects.filter(auction=self.auction).select_related("user").order_by("-timestamp")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["auction"] = self.auction
        return kwargs


class AuctionLotMap(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Admin 2D map of located, unsold lots (works on desktop too).

    Admin-only via AuctionViewMixin (``allow_non_admins`` defaults False → PermissionDenied for a
    buyer). The SVG map + locate search are rendered client-side from the JSON data endpoint, which
    the page polls; this view only frames it.
    """

    template_name = "auction_lot_map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionLotMapData(LoginRequiredMixin, AuctionViewMixin, View):
    """Admin-only JSON feed for the lot map: positions (+ lot number/name) and the full unsold-lot
    list for the locate search, polled every ~10 s."""

    def get(self, request, *args, **kwargs):
        from auctions.mobile.services import ar as ar_service

        return JsonResponse(ar_service.positions_payload(self.auction, include_lot_details=True))


class AuctionLotMapClear(LoginRequiredMixin, AuctionViewMixin, View):
    """Admin-only "clear all locations": wipe this auction's AR observations + positions (POST)."""

    def post(self, request, *args, **kwargs):
        from auctions.mobile.services import ar as ar_service

        ar_service.clear_positions(self.auction)
        messages.success(request, "Cleared all scanned lot locations for this auction.")
        return redirect(reverse("auction_lot_map", kwargs={"slug": self.auction.slug}))


class AuctionLots(LoginRequiredMixin, AuctionViewMixin, HTMxTableView):
    """List of lots associated with an auction.  This is for admins; don't confuse this with the thumbnail-enhanced lot view `AllLots` for users.

    At some point, it may make sense to subclass AllLots here, but I think the needs of the two views are so different that it doesn't make sense
    """

    model = Lot
    table_class = LotHTMxTable
    filterset_class = LotAdminFilter
    template_name = "auctions/auction_lot_admin.html"
    htmx_table_header_template = "auctions/partials/auction_lots_table_header.html"
    # paginate_by = 50

    def get_queryset(self):
        # Every row of this table prints the seller and the winner (each of which reads the
        # auction and the person's userdata to work out a display name), links to both of their
        # invoices, and asks whether the lot has an image.
        return (
            Lot.objects.exclude(is_deleted=True)
            .filter(auction=self.auction)
            .select_related(
                "auctiontos_seller__user__userdata",
                "auctiontos_winner__user__userdata",
                "user",
            )
            .prefetch_related(
                "auction",
                "auctiontos_seller__auction",
                "auctiontos_winner__auction",
                "auctiontos_seller__auctiontos",
                "auctiontos_winner__auctiontos",
                "lotimage_set",
            )
            .order_by("lot_number")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        custom_dropdown_options_count = AuctionDropdown.objects.filter(auction=self.auction).count()
        context["custom_dropdown_enabled"] = (
            self.auction.use_custom_dropdown_field != "disable"
            and bool(self.auction.custom_dropdown_name)
            and custom_dropdown_options_count >= 2
        )
        context["active_tab"] = "lots"
        context["auction"] = self.auction
        # context['filter'] = LotAdminFilter(auction = self.auction)
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["auction"] = self.auction
        return kwargs


class AuctionHelp(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    template_name = "auction_help.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_HELP:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionUsers(LoginRequiredMixin, AuctionViewMixin, HTMxTableView):
    """List of users (AuctionTOS) associated with an auction"""

    model = AuctionTOS
    table_class = AuctionTOSHTMxTable
    filterset_class = AuctionTOSFilter
    template_name = "auction_users.html"
    htmx_table_header_template = "auctions/partials/auction_users_table_header.html"
    allow_non_admins = True  # gated via can_add_edit_people for finer-grained club permission
    # paginate_by = 100

    def get_queryset(self):
        _ = self.can_add_edit_people  # raises PermissionDenied if not allowed
        # Every row renders the Admin badge, which reads the auction's creator and (in a
        # club-managed auction) the member row behind it, so without this each of the 100-odd
        # rows on a page costs its own handful of queries.
        return AuctionTOS.annotate_lot_counts(
            AuctionTOS.objects.filter(auction=self.auction)
            .select_related("clubmember__club", "user__userdata")
            .prefetch_related(Prefetch("auctiontos", queryset=Invoice.objects.order_by("-date")))
            # prefetch, not select_related, for the auction: a join hands every row its own Auction
            # instance, so `self.auction.club` in actions_dropdown_html was a query per row and no
            # cached_property on Auction survived from one row to the next.
            .prefetch_related("auction__club", "auction__created_by")
            .order_by("name"),
            auction=self.auction,
        )

    def get_table_kwargs(self):
        kwargs = super().get_table_kwargs()
        kwargs["request"] = self.request
        kwargs["can_manage_check_in"] = bool(self.can_add_edit_people) and self.auction.use_check_in_mode
        kwargs["is_managed"] = self.auction.is_club_managed
        return kwargs

    def get_filter_placeholder_text(self):
        return "Filter by bidder number, name, email..."

    def get_possible_filters(self):
        filters = []
        # Membership status only makes sense when this auction is managed through a club that
        # charges dues (a 0 fee means no membership system) AND uses the club-member split, since
        # that is the only mode where is_club_member is kept in sync with paid-membership status.
        if (
            self.auction.is_club_managed
            and self.auction.alternate_split_mode == "club_member"
            and self.auction.club.membership_annual_fee
        ):
            filters.extend(
                [
                    ("<small class='text-muted'>Membership:</small>", ""),
                    ("<i class='bi bi-person-badge'></i> Paid club member", "club_member"),
                    ("<i class='bi bi-person'></i> Unpaid", "unpaid"),
                ]
            )
        if self.auction.online_bidding != "disable":
            filters.extend(
                [
                    ("<i class='bi bi-cash-coin'></i> Can bid", "can_bid"),
                    ("<i class='bi bi-cash-coin'></i> Can't bid", "no_bid"),
                ]
            )
        filters.extend(
            [
                ("<i class='bi bi-exclamation-octagon-fill'></i> Can't sell", "no_sell"),
                ("<i class='bi bi-envelope-exclamation-fill'></i> Only invalid email", "email_bad"),
                ("<i class='bi bi-envelope-check-fill'></i> Only verified email", "email_good"),
                ("<i class='bi bi-people-fill'></i> Possible duplicate", "duplicate"),
                ("<small class='text-muted'>Users with an invoice that is:</small>", ""),
                ("<i class='bi bi-bag'></i> Open", "open"),
                ("<i class='bi bi-bag-check'></i> Ready", "ready"),
                ("<i class='bi bi-bag-heart'></i> Paid", "paid"),
                ("<i class='bi bi-bag-dash'></i> Owes the club", "owes_club"),
                ("<i class='bi bi-bag-plus'></i> Club owes", "club_owes"),
                ("<i class='bi bi-eye-fill'></i> User has seen", "seen"),
                ("<i class='bi bi-eye-slash-fill'></i> User has not seen", "unseen"),
            ]
        )
        if self.auction.is_online:
            filters.extend(
                [
                    ("<small class='text-muted'>Find problematic users:</small>", ""),
                    ("<i class='bi bi-exclamation-circle'></i> Least engagement first", "sus"),
                ]
            )
        filters.append(
            (
                "<i class='bi bi-patch-plus-fill'></i> "
                "<a href='https://github.com/iragm/fishauctions/issues/215'>Suggest a new filter</a>",
                "",
            )
        )
        return filters

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["active_tab"] = "users"
        context["can_manage_check_in"] = bool(self.can_add_edit_people) and self.auction.use_check_in_mode
        context["can_scan_club_barcodes"] = bool(self.can_add_edit_people) and bool(self.auction.club_id)
        # When the table has no rows, replace the bare column headers with a helpful message:
        # a "Create user" button pre-populated from the search when a query was typed, or a
        # first-run empty state explaining how users get added on a brand-new auction.
        query = (self.request.GET.get("query") or "").strip()
        filterset = context.get("filter")
        table_empty = filterset is not None and not filterset.qs.exists()
        if table_empty and self.can_add_edit_people:
            if query:
                context["no_results"] = self._build_no_results_html(query)
            else:
                context["no_results"] = self._build_empty_auction_html()
        return context

    def _build_empty_auction_html(self):
        """Return a first-run empty state shown when the auction has no users yet."""
        return (
            "<div class='text-center text-muted p-4'>"
            "<i class='bi bi-people fs-1 d-block mb-2'></i>"
            "<p class='mb-1'>No users yet.</p>"
            "<p class='mb-0'>Users are added automatically when someone joins. "
            "You can also add one now with the <strong>Add user</strong> button above. "
            "Each user's invoice appears here automatically once they buy or sell a lot.</p>"
            "</div>"
        )

    def _build_no_results_html(self, query):
        """Return an HTML snippet with a 'Create user' button pre-populated from the search query."""
        import re as _re
        from urllib.parse import urlencode

        params = {}
        q = query.strip()
        digits_only = _re.sub(r"\D", "", q)
        if len(digits_only) >= 7:
            params["phone"] = q
        elif "@" in q:
            params["email"] = q
        elif _re.fullmatch(r"[A-Za-z\s\-'.]+", q) and len(q) >= 4:
            params["name"] = q
        param_str = f"?{urlencode(params)}" if params else ""
        auction = self.auction
        # In club-managed auctions, AuctionTOSAdmin redirects new-user creates to clubmember_create
        # — but the redirect drops query-string prefill. Route directly to clubmember_create instead,
        # appending ?auction= only when the auction uses the check-in flow.
        if auction.is_club_managed:
            extra = {}
            if auction.manage_users_through_club == "checkin":
                extra["auction"] = auction.slug
            combined = {**extra, **params}
            qs = f"?{urlencode(combined)}" if combined else ""
            create_url = reverse("clubmember_create", kwargs={"slug": auction.club.slug}) + qs
        else:
            create_url = f"/api/auctiontos/{auction.slug}/{param_str}"
        return format_html(
            '<div class="text-center py-3">'
            '<p class="text-muted mb-2">No users match <strong>{}</strong>.</p>'
            '<button class="btn btn-info btn-sm" '
            'hx-get="{}" '
            'hx-target="#modals-here" '
            'hx-trigger="click" '
            '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
            '<i class="bi bi-person-fill-add"></i> Create user</button>'
            "</div>",
            query,
            create_url,
        )

    def get(self, *args, **kwargs):
        if not self.request.htmx and self.get_queryset().filter(bidder_number="ERROR").count():
            messages.error(
                self.request,
                "Automatic bidder number generation failed, manually set the bidder numbers for these users",
            )
        return super().get(*args, **kwargs)


class AuctionDisableBidding(LoginRequiredMixin, AuctionViewMixin, View):
    # TODO: This feature is incomplete and broken — the UI button has been removed from auction_users.html.
    # The core bulk-update works, but re-enabling bidding per-user after this action is not wired up correctly
    # and the overall UX flow is confusing. Do not re-expose this without a full end-to-end implementation.
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.use_check_in_mode:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        updated = AuctionTOS.objects.filter(auction=self.auction, bidding_allowed=True).update(bidding_allowed=False)
        self.auction.create_history(
            applies_to="USERS",
            action="Turned bidding off for all users",
            user=request.user,
        )
        messages.success(request, f"Turned bidding off for {updated} user{'s' if updated != 1 else ''}.")
        return HttpResponse("<script>location.reload();</script>", status=200)


class AuctionCheckIn(LoginRequiredMixin, AuctionViewMixin, View):
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.auctiontos = get_object_or_404(AuctionTOS, pk=kwargs["pk"])
        self.auction = self.auctiontos.auction
        _ = self.can_add_edit_people
        if not self.auction.use_check_in_mode:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        tos = self.auctiontos
        bidder_number = tos.bidder_number if tos.bidder_number and tos.bidder_number != "ERROR" else ""
        check_in_url = reverse("auction_check_in", kwargs={"pk": tos.pk})
        html = f"""
<div data-htmx-modal-root>
<div id="modal-backdrop" class="modal-backdrop fade show" style="display:block;"></div>
<div class="modal fade show" id="modal" tabindex="-1" aria-labelledby="checkInModalLabel" style="display:block;">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="checkInModalLabel">Check in {tos.name}</h5>
        <button type="button" class="btn-close btn-close-white" data-modal-close-action="none" aria-label="Close"></button>
      </div>
      <form hx-post="{check_in_url}" hx-target="#modals-here" hx-swap="innerHTML">
        <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
        <div class="modal-body">
          <label for="checkin_bidder_number" class="form-label"><small>Bidder number</small></label>
          <input
            type="text"
            class="form-control"
            id="checkin_bidder_number"
            name="bidder_number"
            value="{bidder_number}"
            placeholder="Auto"
          >
          <small class="text-muted mt-1 d-block">
            If the bidder number entered here is in use by another user, it'll be assigned to this user and the other user's bidder number will be changed.
          </small>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-modal-close-action="none">Cancel</button>
          <button type="submit" class="btn btn-success">Save</button>
        </div>
      </form>
    </div>
  </div>
</div>
</div>
<script>
window.mountHtmxModal(document.currentScript.previousElementSibling);
(function () {{
  var input = document.getElementById("checkin_bidder_number");
  if (input) {{ input.focus(); input.select(); }}
}})();
</script>
"""
        return HttpResponse(html)

    def post(self, request, *args, **kwargs):
        tos = self.auctiontos
        check_in_auctiontos(tos, acting_user=request.user, bidder_number=request.POST.get("bidder_number", ""))
        messages.success(request, f"Checked in {tos.name}.")
        return close_modal_response("reload-page")


class AuctionDoorPrizes(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    template_name = "auctions/auction_door_prizes.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.use_check_in_mode:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["can_manage_check_in"] = True
        context["active_tab"] = "users"
        context["door_prize_winners"] = AuctionTOS.objects.filter(
            auction=self.auction, door_prize_called__isnull=False
        ).order_by("-door_prize_called", "name")
        context["door_prize_candidates_remaining"] = AuctionTOS.objects.filter(
            auction=self.auction,
            checked_in__isnull=False,
            door_prize_called__isnull=True,
        ).exists()
        return context

    def post(self, request, *args, **kwargs):
        redirect_url = reverse("auction_door_prizes", kwargs={"slug": self.auction.slug})
        # The draw itself lives in services.draw_door_prize so the palette's draw_door_prize action
        # picks from the same pool, by the same rule, with the same RNG.
        winner = draw_door_prize(self.auction, acting_user=request.user)
        if not winner:
            messages.warning(request, "No checked-in users are left for door prizes.")
            return redirect(redirect_url)
        messages.success(request, f"Picked {winner.name}.")
        return redirect(redirect_url)


class QuickCheckInUsers(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    template_name = "auctions/quick_check_in_users.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.club_id:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["can_manage_check_in"] = self.auction.use_check_in_mode
        context["can_scan_club_barcodes"] = True
        context["active_tab"] = "users"
        return context


class AuctionSelfCheckIn(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Kiosk page: members scan their own membership card to check themselves in.

    This page runs under the signed-in admin's session, so scans are posted with
    check_in_only -- the scan endpoint will only check people in, never assign bidder
    numbers or touch invoices."""

    template_name = "auctions/self_check_in.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.use_check_in_mode:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["can_manage_check_in"] = True
        context["can_scan_club_barcodes"] = True
        context["barcode_check_in_only"] = True
        context["active_tab"] = "users"
        return context


class AuctionBarcodeScan(LoginRequiredMixin, AuctionViewMixin, View):
    """POST-only API for barcode scans from auction admin pages (camera or USB HID scanner).

    Pass check_in_only=1 (used by the self check-in kiosk) to accept only membership card
    barcodes and ignore bidder number / invoice adjustment side effects, no matter what the
    client sends."""

    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.club_id:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _apply_adjustment(self, tos, adjustment_type, adjustment_amount, adjustment_label, acting_user):
        """Apply a pending invoice adjustment to tos's (draft) invoice.

        Returns (adjustment_desc, error_response). error_response is a JsonResponse when the
        invoice can't be adjusted (already closed); otherwise None and adjustment_desc describes
        what was applied (empty string if nothing was)."""
        try:
            amount_val = round(float(adjustment_amount))
        except (ValueError, TypeError):
            return "", None
        if amount_val <= 0:
            return "", None
        invoice = Invoice.objects.filter(auctiontos_user=tos).first()
        if invoice and invoice.status != "DRAFT":
            return "", JsonResponse(
                {"ok": False, "message": f"Invoice for {tos.name} is not open and cannot be adjusted."},
                status=400,
            )
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=tos, auction=self.auction)
        InvoiceAdjustment.objects.create(
            invoice=invoice,
            user=acting_user,
            adjustment_type=adjustment_type,
            amount=amount_val,
            notes=adjustment_label[:150],
        )
        sign = "+" if adjustment_type == "ADD" else "-"
        return f"{sign}${amount_val} {adjustment_label}".strip(), None

    def post(self, request, *args, **kwargs):
        barcode = (request.POST.get("barcode") or "").strip()
        check_in_only = (request.POST.get("check_in_only") or "").strip().lower() in ("1", "true", "on", "yes")
        assign_bidder_number = (request.POST.get("assign_bidder_number") or "").strip()
        apply_to_bidder_number = (request.POST.get("apply_to_bidder_number") or "").strip()
        adjustment_type = (request.POST.get("adjustment_type") or "").strip()
        adjustment_amount = (request.POST.get("adjustment_amount") or "").strip()
        adjustment_label = (request.POST.get("adjustment_label") or "").strip()
        if check_in_only:
            assign_bidder_number = ""
            apply_to_bidder_number = ""
            adjustment_type = ""
            adjustment_amount = ""
            adjustment_label = ""
        has_adjustment = adjustment_type in ("ADD", "DISCOUNT") and bool(adjustment_amount)

        # Paddle-barcode lookup: a bidder number scanned to receive a pending invoice adjustment.
        # Resolves an existing AuctionTOS directly (no membership card, no check-in change).
        if apply_to_bidder_number:
            if not has_adjustment:
                return JsonResponse(
                    {"ok": False, "message": "Scan an invoice adjustment barcode before the bidder number."},
                    status=400,
                )
            tos = (
                AuctionTOS.objects.filter(auction=self.auction, bidder_number=apply_to_bidder_number)
                .order_by("-createdon")
                .first()
            )
            if not tos:
                return JsonResponse(
                    {"ok": False, "message": f"No one is using bidder number {apply_to_bidder_number}."}, status=404
                )
            if self.auction.use_check_in_mode and not tos.checked_in:
                return JsonResponse(
                    {"ok": False, "message": f"{tos.name} (bidder {apply_to_bidder_number}) is not checked in yet."},
                    status=400,
                )
            with transaction.atomic():
                adjustment_desc, error_response = self._apply_adjustment(
                    tos, adjustment_type, adjustment_amount, adjustment_label, request.user
                )
                if error_response:
                    return error_response
                if adjustment_desc:
                    self.auction.create_history(
                        applies_to="USERS",
                        action=f"Applied invoice adjustment {adjustment_desc} to {tos.name} via barcode",
                        user=request.user,
                    )
            return JsonResponse(
                {
                    "ok": True,
                    "message": f"Adjusted {tos.name}",
                    "name": tos.name,
                    "bidder_number": tos.bidder_number,
                    "verb": "Adjusted",
                    "adjustment_desc": adjustment_desc,
                }
            )

        if not barcode:
            return JsonResponse({"ok": False, "message": "Scan a membership card barcode."}, status=400)
        if not barcode.isdigit():
            message = "Unrecognized barcode" if check_in_only else "That barcode is not recognized."
            return JsonResponse({"ok": False, "message": message}, status=404)
        member = ClubMember.objects.filter(
            club=self.auction.club,
            membership_number=int(barcode),
            is_deleted=False,
        ).first()
        if not member:
            message = "Unrecognized barcode" if check_in_only else "No club member matches that barcode."
            return JsonResponse({"ok": False, "message": message}, status=404)
        adjustment_desc = ""
        with transaction.atomic():
            # Decide the check-in timestamp before upserting. In check-in mode a bare card scan
            # (re)checks the member in, but when the scan is really about applying a pending invoice
            # adjustment or bidder number to a member who is *already* checked in, we leave their
            # original check-in time alone rather than clobbering it — the intent was the adjustment,
            # not a fresh check-in.
            checked_in_at = _UNSET
            if self.auction.use_check_in_mode:
                existing_tos = (
                    AuctionTOS.objects.filter(auction=self.auction, clubmember=member).order_by("-createdon").first()
                )
                already_checked_in = bool(existing_tos and existing_tos.checked_in)
                if not (already_checked_in and (has_adjustment or assign_bidder_number)):
                    checked_in_at = timezone.now()
            tos = _upsert_clubmember_shadow_tos(
                self.auction,
                member,
                bidding_allowed=True,
                selling_allowed=member.selling_allowed,
                checked_in_at=checked_in_at,
            )
            if not tos:
                return JsonResponse(
                    {"ok": False, "message": "Add a pickup location before checking users in."}, status=400
                )
            if assign_bidder_number and assign_bidder_number != tos.bidder_number:
                tos.force_set_bidder_number(assign_bidder_number, via_barcode=True, acting_user=request.user)
                # Propagate the assigned number back to the ClubMember so it sticks for next time.
                # Use .update() to skip the ClubMember post_save signal — the TOS is already correct.
                ClubMember.objects.filter(pk=member.pk).update(bidder_number=assign_bidder_number)
            if has_adjustment:
                adjustment_desc, error_response = self._apply_adjustment(
                    tos, adjustment_type, adjustment_amount, adjustment_label, request.user
                )
                if error_response:
                    return error_response
        verb = "Checked in" if self.auction.use_check_in_mode else "Added"
        history_action = f"{verb} {tos.name} via {'self check-in scan' if check_in_only else 'barcode'}"
        if assign_bidder_number:
            history_action += f" and assigned bidder number {assign_bidder_number}"
        if adjustment_desc:
            history_action += f" with invoice adjustment {adjustment_desc}"
        self.auction.create_history(applies_to="USERS", action=history_action, user=request.user)
        return JsonResponse(
            {
                "ok": True,
                "message": f"{verb} {tos.name}",
                "name": tos.name,
                "bidder_number": tos.bidder_number,
                "verb": verb,
                "adjustment_desc": adjustment_desc,
            }
        )


class AuctionStats(LoginRequiredMixin, AuctionViewMixin, DetailView):
    """Fun facts about an auction"""

    model = Auction
    template_name = "auction_stats.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        auction = self.get_object()

        # Get list of auctions user is admin of for comparison
        if self.request.user.is_authenticated:
            admin_auctions = (
                Auction.objects.filter(
                    Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                    is_deleted=False,
                )
                .exclude(pk=auction.pk)
                .distinct()
                .order_by("-date_start")[:20]
            )
            context["admin_auctions"] = admin_auctions

            # Get comparison auction from GET parameters
            compare_slug = self.request.GET.get("compare")
            if compare_slug:
                compare_auction = Auction.objects.filter(slug=compare_slug, is_deleted=False).first()
                # Verify user has access to this auction. .first() returns None for a bad/stale
                # slug, so guard before permission_check to avoid a 500 on an invalid ?compare=.
                if compare_auction and compare_auction.permission_check(self.request.user):
                    context["compare_auction"] = compare_auction

        # Check if stats need recalculation (older than 20 minutes or missing)
        now = timezone.now()
        twenty_minutes_ago = now - timezone.timedelta(minutes=20)

        # Don't recalculate stats for auctions older than 90 days
        auction_too_old = False
        if auction.date_start:
            days_since_start = (now - auction.date_start).days
            if days_since_start > 90:
                auction_too_old = True

        # Check if recalculation is already scheduled (next_update_due is recent/in near future)
        recalculation_pending = (
            auction.next_update_due
            and auction.next_update_due >= now - timezone.timedelta(minutes=10)
            and auction.next_update_due <= now + timezone.timedelta(hours=1)
        )

        if not auction_too_old and (not auction.last_stats_update or auction.last_stats_update < twenty_minutes_ago):
            if not recalculation_pending:
                # Schedule immediate recalculation by setting next_update_due to slightly in the past
                # This ensures the task will pick it up immediately (avoids timing issues with next_update_due__lte=now)
                auction.next_update_due = now - timezone.timedelta(seconds=30)
                auction.save(update_fields=["next_update_due"])
                # Trigger the self-scheduling Celery task to process this auction immediately
                from auctions.tasks import schedule_auction_stats_update

                schedule_auction_stats_update()
                context["stats_being_recalculated"] = True
            else:
                # Recalculation already scheduled
                context["stats_being_recalculated"] = True

            # Calculate last update time for display
            if auction.last_stats_update:
                time_since_update = now - auction.last_stats_update
                hours = int(time_since_update.total_seconds() // 3600)
                minutes = int((time_since_update.total_seconds() % 3600) // 60)

                if hours > 0:
                    context["stats_age"] = f"{hours} hour{'s' if hours != 1 else ''} ago"
                else:
                    context["stats_age"] = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                context["stats_age"] = "Never updated"

        if not auction.closed and auction.is_online:
            messages.info(
                self.request,
                "This auction is still in progress, check back once it's finished for more complete stats",
            )
        if auction.date_posted < datetime(year=2024, month=1, day=1, tzinfo=date_tz.utc):
            messages.info(self.request, "Not all stats are available for old auctions.")

        # Add all stat data to context for template rendering
        import json

        context["stats_activity_json"] = json.dumps(auction.get_stat_activity)
        context["stats_attrition_json"] = json.dumps(auction.get_stat_attrition)
        context["stats_auctioneer_speed_json"] = json.dumps(auction.get_stat_auctioneer_speed)
        context["stats_lot_sell_prices_json"] = json.dumps(auction.get_stat_lot_sell_prices)
        context["stats_referrers_json"] = json.dumps(auction.get_stat_referrers)
        context["stats_images_json"] = json.dumps(auction.get_stat_images)
        context["stats_travel_distance_json"] = json.dumps(auction.get_stat_travel_distance)
        context["stats_previous_auctions_json"] = json.dumps(auction.get_stat_previous_auctions)
        context["stats_lots_submitted_json"] = json.dumps(auction.get_stat_lots_submitted)
        context["stats_location_volume_json"] = json.dumps(auction.get_stat_location_volume)
        context["stats_feature_use_json"] = json.dumps(auction.get_stat_feature_use)

        # Add comparison auction stats if available
        if "compare_auction" in context:
            compare_auction = context["compare_auction"]
            context["compare_stats_activity_json"] = json.dumps(compare_auction.get_stat_activity)
            context["compare_stats_attrition_json"] = json.dumps(compare_auction.get_stat_attrition)
            context["compare_stats_auctioneer_speed_json"] = json.dumps(compare_auction.get_stat_auctioneer_speed)
            context["compare_stats_lot_sell_prices_json"] = json.dumps(compare_auction.get_stat_lot_sell_prices)
            context["compare_stats_referrers_json"] = json.dumps(compare_auction.get_stat_referrers)
            context["compare_stats_images_json"] = json.dumps(compare_auction.get_stat_images)
            context["compare_stats_travel_distance_json"] = json.dumps(compare_auction.get_stat_travel_distance)
            context["compare_stats_previous_auctions_json"] = json.dumps(compare_auction.get_stat_previous_auctions)
            context["compare_stats_lots_submitted_json"] = json.dumps(compare_auction.get_stat_lots_submitted)
            context["compare_stats_location_volume_json"] = json.dumps(compare_auction.get_stat_location_volume)
            context["compare_stats_feature_use_json"] = json.dumps(compare_auction.get_stat_feature_use)

        return context
