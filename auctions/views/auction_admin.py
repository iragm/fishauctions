"""Setting an auction up and running the room: pickup locations, users, check-in.

The auction admin's pages, plus ``AuctionStats``; the JSON behind its charts is in
:mod:`auctions.views.auction_stats`.
"""

import json
import logging
from datetime import datetime
from datetime import timezone as date_tz
from zoneinfo import ZoneInfo

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
from django.utils.html import escape, format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.generic import DetailView, ListView, TemplateView, View
from django.views.generic.edit import (
    CreateView,
    DeleteView,
    UpdateView,
)
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions import auction_nav
from auctions.filters import (
    AuctionHistoryFilter,
    AuctionTOSFilter,
    LotAdminFilter,
)
from auctions.form_friction import FormFrictionMixin
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
    AuctionRandomOption,
    AuctionTOS,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PickupLocation,
)
from auctions.services import (
    auction_date_warnings,
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
    browser_timezone,
    check_club_permission,
    close_modal_response,
)
from .invoices import MAX_ADJUSTMENT_AMOUNT

logger = logging.getLogger(__name__)
# return HttpResponse(f"Max bid: ${self.lot.max_bid: .2f}")


class PickupLocations(LoginRequiredMixin, AuctionViewMixin, ListView):
    """Show all pickup locations belonging to the current auction"""

    model = PickupLocation
    template_name = "all_pickup_locations.html"
    ordering = ["name"]

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
        if not self.auction:
            # No auction, so no admin to check against.
            raise Http404
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
        kwargs["user_timezone"] = browser_timezone(self.request)
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


class PickupLocationsUpdate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, PickupLocationForm, UpdateView):
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
        if not self.auction:
            raise Http404
        self.require_auction_admin()
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


class PickupLocationsCreate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, PickupLocationForm, CreateView):
    """Create a new pickup location"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.require_auction_admin()
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
        # New auctions (first location) and copied ones with an inherited club need their club
        # admins to have AuctionTOS records.
        _add_club_admins_as_auction_tos(self.auction, self.request.user)
        return form


class AuctionUpdate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, UpdateView):
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
        kwargs["user_timezone"] = browser_timezone(self.request)
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
        # Only clubs the user administers, or the club already saved.
        new_club = form.cleaned_data.get("club")
        if new_club:
            auction = self.get_object()
            current_club_id = auction.club_id
            if new_club.pk != current_club_id:
                # Changing the club needs permission in the new one.
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
        for warning in auction_date_warnings(updated_auction, ZoneInfo(browser_timezone(self.request))):
            messages.info(self.request, warning)

        # A newly set club gets its admins added as auction admins.
        new_club = self.get_object().club
        if new_club:
            _add_club_admins_as_auction_tos(self.get_object(), self.request.user)

        return form


class AuctionCustomFieldsUpdate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, UpdateView):
    model = Auction
    template_name = "auction_custom_fields_form.html"
    form_class = AuctionCustomFieldsForm

    def get_success_url(self):
        return "/auctions/" + str(self.kwargs["slug"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"{self.auction} - Custom fields"
        context["auction"] = self.auction
        context["option_lists"] = [
            {
                "id": "custom-dropdown-options",
                "title": "Custom dropdown options",
                "url": reverse("auction_custom_dropdown_options", kwargs={"slug": self.auction.slug}),
                "options": AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon"),
            },
            {
                "id": "custom-random-options",
                "title": "Custom random options",
                "url": reverse("auction_custom_random_options", kwargs={"slug": self.auction.slug}),
                "options": AuctionRandomOption.objects.filter(auction=self.auction).order_by("createdon"),
            },
        ]
        context["custom_dropdown_max_length"] = CUSTOM_DROPDOWN_MAX_LENGTH
        return context

    def form_valid(self, form, **kwargs):
        if form.has_changed():
            self.get_object().create_history(applies_to="RULES", user=self.request.user, form=form)
        if getattr(form, "custom_dropdown_auto_disabled", False):
            messages.error(
                self.request, "Custom dropdown requires a name and at least two options. It has been disabled."
            )
        if getattr(form, "custom_random_auto_disabled", False):
            messages.error(
                self.request, "Custom random field requires a name and at least two options. It has been disabled."
            )
        return super().form_valid(form)


class AuctionDropdownOptionsAPI(APIView, AuctionViewMixin):
    """List, add, rename and remove one of an auction's option lists; admins only for writes."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]
    option_model = AuctionDropdown

    def dispatch(self, request, *args, **kwargs):
        # APIView.dispatch skips AuctionViewMixin.dispatch, so set self.auction here.
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug", ""), is_deleted=False)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        options = list(
            self.option_model.objects.filter(auction=self.auction)
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
            if self.option_model.objects.filter(auction=self.auction, value__iexact=value).exists():
                return JsonResponse({"success": False, "error": "That option already exists"})
            option = self.option_model.objects.create(auction=self.auction, user=request.user, value=value)
            return JsonResponse({"success": True, "option": {"id": option.pk, "value": option.value}})

        if not str(option_id or "").isdigit():
            return JsonResponse({"success": False, "error": "Option id is required"})
        option = self.option_model.objects.filter(pk=option_id, auction=self.auction).first()
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
            duplicate = self.option_model.objects.filter(auction=self.auction, value__iexact=value).exclude(
                pk=option.pk
            )
            if duplicate.exists():
                return JsonResponse({"success": False, "error": "That option already exists"})
            option.value = value
            option.save()
            return JsonResponse({"success": True, "option": {"id": option.pk, "value": option.value}})
        if action == "delete":
            option.delete()
            return JsonResponse({"success": True})
        return JsonResponse({"success": False, "error": "Invalid action"})


class AuctionRandomOptionsAPI(AuctionDropdownOptionsAPI):
    """The options ``Lot.custom_random`` is dealt from."""

    option_model = AuctionRandomOption


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


class AuctionPages(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """The ribbon's More tab: every admin page for the auction, a line each. The list is `auctions/auction_nav.py`."""

    template_name = "auctions/auction_pages.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["active_tab"] = "more"
        context["nav_groups"] = auction_nav.groups_for(self.auction)
        return context


class AuctionLotMap(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Admin 2D map of located, unsold lots. The SVG map and search render client-side from the JSON feed."""

    template_name = "auction_lot_map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionLotMapData(LoginRequiredMixin, AuctionViewMixin, View):
    """Admin-only JSON for the lot map: positions and the unsold-lot list, polled every ~10s."""

    def get(self, request, *args, **kwargs):
        from auctions.mobile.services import ar as ar_service

        return JsonResponse(ar_service.positions_payload(self.auction, include_lot_details=True))


class AuctionLotMapClear(LoginRequiredMixin, AuctionViewMixin, View):
    """Admin-only "clear all locations": wipe this auction's AR observations and positions."""

    def post(self, request, *args, **kwargs):
        from auctions.mobile.services import ar as ar_service

        ar_service.clear_positions(self.auction)
        messages.success(request, "Cleared all scanned lot locations for this auction.")
        return redirect(reverse("auction_lot_map", kwargs={"slug": self.auction.slug}))


class AuctionLots(LoginRequiredMixin, AuctionViewMixin, HTMxTableView):
    """Lots in an auction, for admins. The buyer-facing view is ``AllLots``."""

    model = Lot
    table_class = LotHTMxTable
    filterset_class = LotAdminFilter
    template_name = "auctions/auction_lot_admin.html"
    htmx_table_header_template = "auctions/partials/auction_lots_table_header.html"
    # paginate_by = 50

    def get_queryset(self):
        # Every row prints the seller and winner, their invoices, and whether the lot has an image.
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

    def get_possible_filters(self):
        # LotAdminFilter.STATUS_KEYWORDS
        return [
            ("<i class='bi bi-hourglass-split'></i> Active unsold", "active_unsold"),
            ("<i class='bi bi-slash-circle'></i> Ended unsold", "ended_unsold"),
        ]


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
        # Every row renders the Admin badge, which reads the creator and the member row.
        return AuctionTOS.annotate_lot_counts(
            AuctionTOS.objects.filter(auction=self.auction)
            .select_related("clubmember__club", "clubmember__membership_carried_by", "user__userdata")
            .prefetch_related(Prefetch("auctiontos", queryset=Invoice.objects.order_by("-date")))
            # prefetch, not select_related: a join gives every row its own Auction instance, so
            # `self.auction.club` was a query per row.
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
        # Membership status only applies to a club-managed auction that charges dues and uses the
        # club-member split, the only mode where is_club_member tracks paid membership.
        if (
            self.auction.is_club_managed
            and self.auction.alternate_split_mode == "club_member"
            and self.auction.club.charges_dues
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
        # With no rows, show a "Create user" button prefilled from the search, or a first-run
        # empty state.
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
        """An HTML snippet with a 'Create user' button prefilled from the search query."""
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
        # Club-managed auctions redirect new-user creates to clubmember_create, which drops the
        # prefill, so link there directly; ?auction= only in check-in mode.
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
    # TODO: incomplete and broken -- the UI button was removed from auction_users.html. Re-enabling
    # bidding per user after this action isn't wired up. Don't re-expose without finishing it.
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
        # Both typed by people (the name by the participant themselves), so both are escaped.
        bidder_number = escape(tos.bidder_number if tos.bidder_number and tos.bidder_number != "ERROR" else "")
        name = escape(tos.name or "")
        check_in_url = reverse("auction_check_in", kwargs={"pk": tos.pk})
        html = f"""
<div data-htmx-modal-root>
<div id="modal-backdrop" class="modal-backdrop fade show" style="display:block;"></div>
<div class="modal fade show" id="modal" tabindex="-1" aria-labelledby="checkInModalLabel" style="display:block;">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="checkInModalLabel">Check in {name}</h5>
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
        # services.draw_door_prize, so the palette draws from the same pool by the same rule.
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
    """Kiosk page: members scan their own membership card to check in.

    Scans run under the admin's session and post check_in_only, so they never assign bidder numbers or
    touch invoices.
    """

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
    """POST-only API for barcode scans from admin pages (camera or USB scanner).

    check_in_only=1 (the kiosk) accepts only membership cards and ignores bidder number and adjustment
    side effects.
    """

    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people
        if not self.auction.club_id:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _apply_adjustment(self, tos, adjustment_type, adjustment_amount, adjustment_label, acting_user):
        """Apply a pending invoice adjustment to tos's draft invoice.

        Returns (adjustment_desc, error_response); error_response is set when the invoice is closed.
        """
        try:
            amount_val = round(float(adjustment_amount))
        except (ValueError, TypeError, OverflowError):
            return "", None
        if amount_val <= 0:
            return "", None
        if amount_val > MAX_ADJUSTMENT_AMOUNT:
            return "", JsonResponse(
                {"ok": False, "message": f"Adjustments can be at most ${MAX_ADJUSTMENT_AMOUNT:,}."}, status=400
            )
        invoice = Invoice.objects.filter(auctiontos_user=tos).first()
        if invoice and invoice.status != "DRAFT":
            return "", JsonResponse(
                {"ok": False, "message": f"Invoice for {tos.name} is not open and cannot be adjusted."},
                status=400,
            )
        if not invoice:
            invoice = Invoice.for_participant(tos, self.auction)
        InvoiceAdjustment.objects.create(
            invoice=invoice,
            user=acting_user,
            adjustment_type=adjustment_type,
            amount=amount_val,
            notes=adjustment_label[:150],
        )
        invoice.recalculate()
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

        # A bidder number scanned to receive an adjustment: no card, no check-in change.
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
            # In check-in mode a bare card scan re-checks the member in, but a scan applying an
            # adjustment or bidder number to an already checked-in member keeps their original time.
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
                # .update() to skip the ClubMember post_save signal; the TOS is already correct.
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


#: ``<``, ``>`` and ``&`` as JSON string escapes, the set ``django.utils.html.json_script`` uses.
_CHART_JSON_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def _chart_json(data):
    """``data`` as JSON safe to write straight into a ``<script>`` block.

    ``json.dumps`` does not escape ``<``, so a lot name or referrer containing ``</script>`` closed
    the tag and everything after it ran as markup -- and a referrer reaches these charts from the
    unauthenticated page-view beacon. ``ensure_ascii`` (the default) already escapes U+2028/U+2029.
    Returns a ``SafeString``, so the template's ``|safe`` is a no-op rather than the only thing
    standing between a stranger's text and the page.
    """
    return mark_safe(json.dumps(data).translate(_CHART_JSON_ESCAPES))  # noqa: S308 - escaped above


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
                # .first() returns None for a stale ?compare= slug, so guard before permission_check.
                if compare_auction and compare_auction.permission_check(self.request.user):
                    context["compare_auction"] = compare_auction

        # Recalculate stats older than 20 minutes.
        now = timezone.now()
        twenty_minutes_ago = now - timezone.timedelta(minutes=20)

        # Don't recalculate stats for auctions older than 90 days
        auction_too_old = False
        if auction.date_start:
            days_since_start = (now - auction.date_start).days
            if days_since_start > 90:
                auction_too_old = True

        # Is a recalculation already scheduled?
        recalculation_pending = (
            auction.next_update_due
            and auction.next_update_due >= now - timezone.timedelta(minutes=10)
            and auction.next_update_due <= now + timezone.timedelta(hours=1)
        )

        if not auction_too_old and (not auction.last_stats_update or auction.last_stats_update < twenty_minutes_ago):
            if not recalculation_pending:
                # Slightly in the past so the task picks it up immediately.
                auction.next_update_due = now - timezone.timedelta(seconds=30)
                auction.save(update_fields=["next_update_due"])
                # Trigger the self-scheduling Celery task.
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
        context["stats_activity_json"] = _chart_json(auction.get_stat_activity)
        context["stats_attrition_json"] = _chart_json(auction.get_stat_attrition)
        context["stats_auctioneer_speed_json"] = _chart_json(auction.get_stat_auctioneer_speed)
        context["stats_lot_sell_prices_json"] = _chart_json(auction.get_stat_lot_sell_prices)
        context["stats_referrers_json"] = _chart_json(auction.get_stat_referrers)
        context["stats_travel_distance_json"] = _chart_json(auction.get_stat_travel_distance)
        context["stats_previous_auctions_json"] = _chart_json(auction.get_stat_previous_auctions)
        context["stats_lots_submitted_json"] = _chart_json(auction.get_stat_lots_submitted)
        context["stats_location_volume_json"] = _chart_json(auction.get_stat_location_volume)
        context["stats_feature_use_json"] = _chart_json(auction.get_stat_feature_use)

        # Add comparison auction stats if available
        if "compare_auction" in context:
            compare_auction = context["compare_auction"]
            context["compare_stats_activity_json"] = _chart_json(compare_auction.get_stat_activity)
            context["compare_stats_attrition_json"] = _chart_json(compare_auction.get_stat_attrition)
            context["compare_stats_auctioneer_speed_json"] = _chart_json(compare_auction.get_stat_auctioneer_speed)
            context["compare_stats_lot_sell_prices_json"] = _chart_json(compare_auction.get_stat_lot_sell_prices)
            context["compare_stats_referrers_json"] = _chart_json(compare_auction.get_stat_referrers)
            context["compare_stats_travel_distance_json"] = _chart_json(compare_auction.get_stat_travel_distance)
            context["compare_stats_previous_auctions_json"] = _chart_json(compare_auction.get_stat_previous_auctions)
            context["compare_stats_lots_submitted_json"] = _chart_json(compare_auction.get_stat_lots_submitted)
            context["compare_stats_location_volume_json"] = _chart_json(compare_auction.get_stat_location_volume)
            context["compare_stats_feature_use_json"] = _chart_json(compare_auction.get_stat_feature_use)

        return context
