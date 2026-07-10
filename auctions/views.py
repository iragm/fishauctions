import ast
import base64
import collections
import csv
import json
import logging
import re
import secrets
import uuid
from datetime import date as date_type
from datetime import datetime, timedelta
from datetime import timezone as date_tz
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from pathlib import Path
from random import randint, sample, uniform
from time import time
from urllib.parse import quote_plus, unquote, urlencode, urlparse

import channels.layers
import qr_code
import requests
from asgiref.sync import async_to_sync
from chartjs.colors import next_color
from chartjs.views.columns import BaseColumnsHighChartsView
from chartjs.views.lines import BaseLineChartView
from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import (
    Avg,
    BooleanField,
    Case,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.base import Model as Model
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDay, TruncMonth
from django.forms import modelformset_factory
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView, ListView, RedirectView, TemplateView, View
from django.views.generic.base import ContextMixin
from django.views.generic.edit import (
    CreateView,
    DeleteView,
    FormMixin,
    FormView,
    UpdateView,
)
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin
from django_weasyprint import WeasyTemplateResponseMixin
from easy_thumbnails.files import get_thumbnailer
from el_pagination.views import AjaxListView
from PIL import Image
from pytz import timezone as pytz_timezone
from pywebpush import WebPushException
from qr_code.qrcode.utils import QRCodeOptions
from reportlab.platypus import (
    Image as PImage,
)
from rest_framework import generics
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from user_agents import parse
from webpush import send_user_notification
from webpush.models import PushInformation

from .authentication import ApiKeyThrottle, OptionalAPIKeyAuthentication
from .bidding import place_bid_and_broadcast
from .filters import (
    AuctionFilter,
    AuctionHistoryFilter,
    AuctionTOSFilter,
    BapAwardFilter,
    ClubBapLotFilter,
    ClubHistoryFilter,
    ClubMemberFilter,
    LotAdminFilter,
    LotFilter,
    UserBidLotFilter,
    UserLotFilter,
    UserWatchLotFilter,
    UserWonLotFilter,
    get_recommended_lots,
    rhyming_name_q,
)
from .forms import (
    IMAGE_PROCESSING_EXCEPTIONS,
    AuctionCustomFieldsForm,
    AuctionEditForm,
    AuctionJoin,
    AuctionNoShowForm,
    AuctionTOSMergeReviewForm,
    AuctionTOSMergeTargetForm,
    BapAwardForm,
    BulkSellLotsToOnlineHighBidder,
    ChangeInvoiceStatusForm,
    ChangeUsernameForm,
    ChangeUserPreferencesForm,
    ClubBapCategoryOverrideForm,
    ClubBapSettingsForm,
    ClubEditForm,
    ClubEmailSettingsForm,
    ClubMemberAdminForm,
    ClubMemberDiscordForm,
    ClubMemberMergeReviewForm,
    ClubMemberMergeTargetForm,
    ClubMemberPermissionsForm,
    ClubMemberSelfServiceForm,
    ClubMembershipSettingsForm,
    ClubMoneyBalanceForm,
    ClubMoneyForm,
    ClubPayPalCredentialsForm,
    ClubTreasurerReportForm,
    CreateAuctionForm,
    CreateEditAuctionTOS,
    CreateImageForm,
    CreateLotForm,
    DeleteAuctionTOS,
    EditLot,
    InvoiceAdjustmentForm,
    InvoiceAdjustmentFormSetHelper,
    LabelPrintFieldsForm,
    LotCategoryForm,
    LotFormSetHelper,
    LotRefundForm,
    MultiAuctionTOSPrintLabelForm,
    PickupLocationForm,
    QuickAddLot,
    QuickAddTOS,
    TOSFormSetHelper,
    UserLabelPrefsForm,
    UserLocation,
    validate_image_url,
)
from .helper_functions import bin_data, get_currency_symbol
from .models import (
    CUSTOM_DROPDOWN_MAX_LENGTH,
    FAQ,
    SQUARE_OAUTH_SCOPES,
    AdCampaign,
    AdCampaignResponse,
    Auction,
    AuctionCampaign,
    AuctionDropdown,
    AuctionHistory,
    AuctionIgnore,
    AuctionTOS,
    BapAward,
    Bid,
    BlogPost,
    Category,
    ChatSubscription,
    Club,
    ClubAPIKey,
    ClubAPIKeyFieldMap,
    ClubBapCategoryOverride,
    ClubDiscordRole,
    ClubHistory,
    ClubMember,
    ClubMoney,
    CommandPaletteSearch,
    Invoice,
    InvoiceAdjustment,
    InvoicePayment,
    Lot,
    LotHistory,
    LotImage,
    MobileDevice,
    PageView,
    PayPalSeller,
    PickupLocation,
    SearchHistory,
    SquareSeller,
    UserBan,
    UserData,
    UserIgnoreCategory,
    UserInterestCategory,
    UserLabelPrefs,
    Watch,
    add_price_info,
    distance_to,
    find_image,
    guess_category,
    median_value,
    nearby_auctions,
    normalize_email,
)
from .serializers import (
    CLUB_MEMBER_API_KEY_MAPPING_FIELDS,
    BapAwardAPIKeyCreateSerializer,
    ClubMemberAPIKeySerializer,
    ClubMemberSerializer,
)
from .services import map_fields
from .site_setup import get_server_public_ip
from .tables import (
    AuctionHistoryHTMxTable,
    AuctionHTMxTable,
    AuctionTOSHTMxTable,
    BapAwardHTMxTable,
    ClubBapLotHTMxTable,
    ClubHistoryHTMxTable,
    ClubMemberHTMxTable,
    LotHTMxTable,
    LotHTMxTableForUsers,
)
from .tasks import (
    cancel_invoice_notification,
    maybe_send_membership_renewal_confirmation,
    schedule_invoice_notification,
)

# Distance conversion constant
MILES_TO_KM = 1.60934

# Invoice notification delay in seconds (allows for undo before email is sent)
INVOICE_NOTIFICATION_DELAY_SECONDS = 15

# Maximum length for feedback text fields
FEEDBACK_TEXT_MAX_LENGTH = 500

logger = logging.getLogger(__name__)
UNASSIGNED_BIDDER_NUMBER_LABEL = "not assigned"


class AdminEmailMixin:
    """Add an admin_email value from settings to the context of a request"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["admin_email"] = settings.ADMINS[0][1]
        return context


class HTMxTableView(SingleTableMixin, FilterView):
    """Shared behavior for list views that render a full page plus an HTMX table partial."""

    htmx_template_name = "tables/table_generic.html"
    filter_placeholder_text = None
    possible_filters = ()
    htmx_table_header_template = None

    def get_template_names(self):
        if self.request.htmx:
            return self.htmx_template_name
        template_name = getattr(self, "template_name", None)
        if not template_name:
            msg = f"{self.__class__.__name__} must define 'template_name' when not using htmx requests"
            raise ImproperlyConfigured(msg)
        return template_name

    def get_filter_placeholder_text(self):
        return self.filter_placeholder_text

    def get_possible_filters(self):
        return list(self.possible_filters)

    def get_htmx_table_header_template(self):
        return self.htmx_table_header_template

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filter_placeholder_text = self.get_filter_placeholder_text()
        context["filter_placeholder_text"] = filter_placeholder_text
        context["possible_filters"] = self.get_possible_filters()
        context["htmx_table_header_template"] = self.get_htmx_table_header_template()
        filterset = context.get("filter")
        if filterset and filter_placeholder_text:
            query_field = filterset.form.fields.get("query")
            if query_field:
                query_field.widget.attrs["placeholder"] = filter_placeholder_text
        return context


class AuctionViewMixin:
    """For auction permissions, this will try to set self.auction based on the url's slug,
    then see if the user has permission or not
    """

    # this can be set to true for views that are shared between admins and regular users, while providing a different view to each.
    # often used in get_context_data, as: context['is_auction_admin'] = self.is_auction_admin
    allow_non_admins = False

    # set automatically in dispatch, unless you manually set it
    auction = None

    def get_auction(self, slug):
        if not self.auction and slug:
            self.auction = get_object_or_404(Auction, slug=slug, is_deleted=False)
            self.is_auction_admin

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.pop("slug", ""))
        return super().dispatch(request, *args, **kwargs)

    @property
    def is_auction_admin(self):
        """Helper function used to check and see if request.user is the creator of the auction or is someone who has been made an admin of the auction.
        Returns False on no permission or True if the user has permission to access the auction"""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise requests.HTTPError(msg) from None
        result = self.auction.permission_check(self.request.user)
        if not result:
            if self.allow_non_admins:
                # logger.debug("non-admins allowed")
                pass
            else:
                raise PermissionDenied()
        else:
            # logger.debug("allowing user %s to view %s", self.request.user, self.auction)
            pass
        return result

    @property
    def can_add_edit_people(self):
        """For club-managed auctions, gate people-management actions behind the club's
        permission_add_edit (or permission_admin). Otherwise falls back to is_auction_admin.
        Always allows the auction creator, superusers, and AuctionTOS admins through is_auction_admin.
        Raises PermissionDenied when neither path grants access (matching is_auction_admin)."""
        prev_allow_non_admins = self.allow_non_admins
        self.allow_non_admins = True
        try:
            is_admin = self.is_auction_admin
        finally:
            self.allow_non_admins = prev_allow_non_admins
        if is_admin:
            return True
        if self.auction and self.auction.is_club_managed:
            if check_club_permission(self.request.user, self.auction.club, "permission_add_edit"):
                return True
        raise PermissionDenied()

    @property
    def club_sidebar_club(self):
        """The club whose nav sidebar should render on this auction page (or None)."""
        return self.auction.club if self.auction else None

    @property
    def club_sidebar_can_view(self):
        """Whether the current user may see the club sidebar on an auction page.
        Non-raising (unlike is_auction_admin) so it's safe to call from templates."""
        if not self.auction or not self.auction.club_id:
            return False
        return bool(self.auction.permission_check(self.request.user))


def check_club_permission(user, club, permission_name):
    """Check if a user has a specific permission for a club.

    Returns True if the user is a superuser or has the named permission (or permission_admin,
    which acts as a wildcard granting all permissions).
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).first()
    if not member:
        return False
    if member.permission_admin:
        return True
    return bool(getattr(member, permission_name, False))


_UNSET = object()


def _default_pickup_location_for_auction(auction):
    return PickupLocation.objects.filter(auction=auction).order_by("-is_default", "pk").first()


def _upsert_clubmember_shadow_tos(
    auction,
    member,
    *,
    pickup_location=None,
    is_club_member=_UNSET,
    bidding_allowed=_UNSET,
    selling_allowed=_UNSET,
    checked_in_at=_UNSET,
):
    if not member.bidder_number:
        member.generate_bidder_number(save=True)
    pickup_location = pickup_location or _default_pickup_location_for_auction(auction)
    if not pickup_location:
        return None
    tos = AuctionTOS.objects.filter(auction=auction, clubmember=member).order_by("-createdon").first()
    if not tos:
        tos = AuctionTOS(
            user=member.user,
            auction=auction,
            pickup_location=pickup_location,
            clubmember=member,
            bidder_number=member.bidder_number,
            manually_added=True,
        )
    tos.user = member.user
    tos.pickup_location = pickup_location
    tos.clubmember = member
    tos.bidder_number = member.bidder_number
    tos.name = member.name or ""
    tos.email = member.email or ""
    tos.phone_number = member.phone_number or ""
    tos.address = member.address or ""
    if is_club_member is not _UNSET:
        tos.is_club_member = is_club_member
    if bidding_allowed is not _UNSET:
        tos.bidding_allowed = bidding_allowed
    if selling_allowed is not _UNSET:
        tos.selling_allowed = selling_allowed
    if checked_in_at is not _UNSET:
        tos.checked_in = checked_in_at
    tos.save()
    return tos


def close_modal_response(action=None, *, event_name=None, redirect_url=None, table_selector=None, extra_triggers=None):
    """Ask the active HtmxModal to close (with optional action) after a successful POST.

    The response body is a tiny ``<script>`` that calls ``window.closeModal`` — HTMX evaluates
    inline scripts in swapped content, which gives us a single, reliable invocation point that
    works regardless of whether an HX-Trigger response-header listener is attached.

    Pass ``extra_triggers={"event_name": detail, ...}`` to fire additional HTMX triggers (e.g.
    a separate table-refresh event) in the same response via the ``HX-Trigger`` header.
    """
    detail = {"action": action} if action else {}
    if event_name is not None:
        detail["eventName"] = event_name
    if redirect_url is not None:
        detail["redirectUrl"] = redirect_url
    if table_selector is not None:
        detail["tableSelector"] = table_selector
    body = f"<script>window.closeModal({json.dumps(detail)});</script>"
    headers = {}
    if extra_triggers:
        headers["HX-Trigger"] = json.dumps(extra_triggers)
    return HttpResponse(body, headers=headers)


class IsAuthenticatedOrAPIKey(BasePermission):
    """Allow requests authenticated either as a user or with a club API key."""

    def has_permission(self, request, view):
        return request.user.is_authenticated or hasattr(request, "api_key")


def _invoice_membership_lookup_email(invoice):
    """Return a usable lookup email for the buyer on this invoice, or empty string."""
    if not invoice:
        return ""
    tos = invoice.auctiontos_user
    candidates = []
    if tos:
        candidates.append(tos.email)
        if tos.user:
            candidates.append(tos.user.email)
    if invoice.buyer:
        candidates.append(invoice.buyer.email)
    for email in candidates:
        if email and email.strip():
            return email.strip()
    return ""


def _find_club_member(club, user, email):
    """Match an existing (non-deleted) ClubMember by user link first, then by email."""
    if not club:
        return None
    member = None
    if user:
        member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).first()
    if not member and email:
        member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).first()
    return member


def _invoice_membership_candidate(invoice):
    if not invoice or not invoice.auction or not invoice.auction.club or not invoice.auctiontos_user:
        return None
    if not invoice.auction.manage_users_through_club:
        return None
    user = invoice.auctiontos_user.user
    email = _invoice_membership_lookup_email(invoice)
    if not user and not email:
        # Fall back to the ClubMember directly linked on the TOS (no email/user needed).
        return getattr(invoice.auctiontos_user, "clubmember", None)
    return _find_club_member(invoice.auction.club, user, email)


def _compute_member_renewal_expiration(club, member, today):
    """Compute the new membership expiration date when renewing.

    - Rolling clubs: extend one year from the current expiration if it is
      still in the future; otherwise extend from today (same month/day).
    - January-1st clubs: extend one year from the current expiration if it is
      still in the future; otherwise extend from today.  Either way the result
      always lands on January 1 so the whole-club calendar stays aligned.
    """
    import datetime as _dt

    current_exp = member.membership_expiration_date
    if club.membership_system == "rolling":
        if current_exp and current_exp > today:
            base = current_exp
        else:
            base = today
        try:
            return base.replace(year=base.year + 1)
        except ValueError:
            # Feb 29 → Feb 28 in a non-leap-year target
            return base.replace(month=2, day=28, year=base.year + 1)
    else:  # january_first
        if current_exp and current_exp > today:
            base = current_exp
        else:
            base = today
        return _dt.date(base.year + 1, 1, 1)


def _should_mark_invoice_renewal_needed(invoice):
    if not invoice or not invoice.auction or not invoice.auction.club:
        return False
    auction = invoice.auction
    club = auction.club
    if not auction.manage_users_through_club:
        return False
    if not auction.add_membership_fee_to_invoices_for_expired_members:
        return False
    if not club.membership_annual_fee:
        return False
    # Without a usable email or user we cannot reliably look up or create a ClubMember;
    # don't auto-add the fee in that case (an admin can still toggle it on manually).
    # Exception: if the TOS already has a directly linked ClubMember, proceed.
    if not _invoice_membership_lookup_email(invoice) and not (invoice.auctiontos_user and invoice.auctiontos_user.user):
        if not (invoice.auctiontos_user and invoice.auctiontos_user.clubmember_id):
            return False
    member = _invoice_membership_candidate(invoice)
    if not member:
        return True
    expiration_date = member.membership_expiration_date
    if not expiration_date:
        return True
    return expiration_date <= timezone.now().date() + timedelta(days=30)


def _sync_tos_alternate_split(tos, invoice=None):
    """See AuctionTOS.update_alternate_split_from_membership; this just adds a None guard."""
    if tos:
        tos.update_alternate_split_from_membership(invoice)


def _ensure_invoice_renewal_state(invoice):
    if not invoice:
        return
    if not invoice.auction:
        # Club-only renewal invoices have renewal_needed set explicitly at creation; don't override.
        return
    # Skip processed renewals, and respect a checkbox an admin has explicitly set.
    if not invoice.renewal_processed and not invoice.renewal_manually_set:
        should_need = _should_mark_invoice_renewal_needed(invoice)
        if invoice.renewal_needed != should_need:
            invoice.renewal_needed = should_need
            invoice.save(update_fields=["renewal_needed"])
    _sync_tos_alternate_split(invoice.auctiontos_user, invoice)


def _process_invoice_membership_renewal(invoice, acting_user=None, payment_method="Invoice", external_id=None):
    """Process a membership renewal triggered by an invoice payment.

    Wrapped in a try/except + atomic block so a failure (e.g. Discord API outage)
    cannot bubble out and break the caller that just marked the invoice paid.
    """
    if not invoice or not invoice.renewal_needed:
        return
    try:
        with transaction.atomic():
            # Re-fetch under the row lock so concurrent webhooks can't double-process.
            locked = Invoice.objects.select_for_update().filter(pk=invoice.pk).first()
            if not locked or not locked.renewal_needed or locked.renewal_processed:
                return
            club = locked.club or (locked.auction.club if locked.auction else None)
            if not club:
                return
            # For membership invoices, prefer the direct club_member reference
            user = locked.buyer or (locked.auctiontos_user.user if locked.auctiontos_user else None)
            email = _invoice_membership_lookup_email(locked)
            tos_member = getattr(locked.auctiontos_user, "clubmember", None) if locked.auctiontos_user else None
            if locked.club_member:
                member = locked.club_member
            elif tos_member:
                # AuctionTOS has a linked ClubMember even without user/email — use it.
                member = tos_member
            elif not user and not email and not locked.auctiontos_user:
                # Nothing reliable to identify the buyer by; do not create a junk member.
                logger.warning(
                    "Skipping renewal on invoice %s: no linked user and no email available",
                    locked.pk,
                )
                return
            else:
                member = _find_club_member(club, user, email)
            if not member:
                if locked.auctiontos_user:
                    name = locked.auctiontos_user.name or (
                        f"{user.first_name} {user.last_name}".strip() if user else ""
                    )
                    member_email = locked.auctiontos_user.email or (user.email if user else "") or email
                    source = "auction_invoice"
                else:
                    name = f"{user.first_name} {user.last_name}".strip() if user else ""
                    member_email = (user.email if user else "") or email
                    source = "membership_payment"
                member = ClubMember.objects.create(
                    club=club,
                    user=user,
                    name=name,
                    email=member_email,
                    source=source,
                )
            elif user and not member.user:
                # Link the existing email-only member to the user now that we know them.
                member.user = user
                member.save(update_fields=["user"])
            today = timezone.now().date()
            old_expiration = member.membership_expiration_date
            member.membership_expiration_date = _compute_member_renewal_expiration(club, member, today)
            new_expiration = member.membership_expiration_date
            member.membership_last_paid = today
            if member.email:
                member.email_address_status = "VALID"
            member.save(
                update_fields=[
                    "membership_last_paid",
                    "membership_expiration_date",
                    "membership_expiration_reminder_30_days_due",
                    "membership_expiration_reminder_due",
                    "email_address_status",
                ]
            )
            InvoicePayment.objects.create(
                invoice=None,
                club_member=member,
                payment_target="CLUB_MEMBER",
                amount=Decimal(club.membership_annual_fee or 0),
                amount_available_to_refund=Decimal("0.00"),
                currency=locked.currency,
                payment_method=payment_method,
                memo=f"Renewal via {payment_method} ({external_id})"
                if external_id
                else f"Renewal from invoice #{locked.pk}",
            )
            if club.membership_annual_fee and not locked.auction:
                # Club-only invoices: create the membership ClubMoney here.
                # Auction invoices: Invoice.sync_club_money (via Invoice.save) already books the
                # membership dues, including the reversal when the invoice is un-paid. Booking it
                # here too would double-count membership revenue.
                ClubMoney.objects.create(
                    club=club,
                    created_by=acting_user,
                    date=today,
                    amount=Decimal(club.membership_annual_fee),
                    invoice=locked,
                    description=f"Membership renewal via {payment_method} ({external_id})"
                    if external_id
                    else f"Membership renewal via {payment_method} (invoice #{locked.pk})",
                    category=ClubMoney.CATEGORY_MEMBERSHIP,
                )
            locked.renewal_processed = True
            locked.save(update_fields=["renewal_processed"])
            # Keep the in-memory invoice in sync for the caller.
            invoice.renewal_processed = True
            member.update_last_club_activity()
    except Exception:
        logger.exception("Failed to process membership renewal for invoice %s", invoice.pk)
        return
    # Discord role assignment is best-effort: a network/API failure must not
    # roll back the renewal nor crash the caller.
    try:
        member.maybe_assign_discord_role()
    except Exception:
        logger.exception("Failed to assign Discord role for club member %s", getattr(member, "pk", None))
    old_exp_str = old_expiration.strftime("%-m/%-d/%Y") if old_expiration else "none"
    new_exp_str = new_expiration.strftime("%-m/%-d/%Y") if new_expiration else "unknown"
    auction = invoice.auction if invoice else None
    auction_suffix = f" for {auction}" if auction else ""
    id_suffix = f" (ID: {external_id})" if external_id else ""
    action = (
        f"{member.display_name} renewed via {payment_method}{auction_suffix}; "
        f"expiration {old_exp_str} → {new_exp_str}{id_suffix}"
    )
    try:
        ClubHistory.objects.create(
            club=club,
            user=acting_user,
            action=action,
            applies_to="MEMBERSHIP",
        )
    except Exception:
        logger.exception("Failed to record ClubHistory for renewal of invoice %s", invoice.pk)
    try:
        maybe_send_membership_renewal_confirmation(member)
    except Exception:
        logger.exception(
            "Failed to send membership renewal confirmation for club member %s", getattr(member, "pk", None)
        )


PAYMENT_OAUTH_CLUB_SESSION_KEY = "payment_oauth_club_slug"


def _user_can_manage_club_payments(user, club):
    """Mirror the dispatch-level permission gate used by ClubMembershipSettingsView."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return (
        ClubMember.objects.filter(
            user=user,
            club=club,
            is_deleted=False,
        )
        .filter(Q(permission_admin=True) | Q(permission_edit_club=True) | Q(permission_money=True))
        .exists()
    )


def _stash_club_for_payment_oauth(request):
    """If the request includes ?club=<slug> and the user can manage that club's payments,
    store the slug in the session so the OAuth callback can link the new seller to it.
    Returns the Club instance for callers that need it, or None.
    """
    slug = request.GET.get("club")
    if not slug:
        request.session.pop(PAYMENT_OAUTH_CLUB_SESSION_KEY, None)
        return None
    club = Club.objects.filter(slug=slug).first()
    if not club or not _user_can_manage_club_payments(request.user, club):
        request.session.pop(PAYMENT_OAUTH_CLUB_SESSION_KEY, None)
        return None
    request.session[PAYMENT_OAUTH_CLUB_SESSION_KEY] = slug
    return club


def _pop_club_for_payment_oauth(request):
    """Retrieve and clear the pending club context from session. Returns Club or None."""
    slug = request.session.pop(PAYMENT_OAUTH_CLUB_SESSION_KEY, None)
    if not slug:
        return None
    club = Club.objects.filter(slug=slug).first()
    if not club or not _user_can_manage_club_payments(request.user, club):
        return None
    return club


def club_ids_available_for_contact_autofill(user):
    """Return club IDs whose member and auction contact data may be used for autofill."""
    if not user.is_authenticated:
        return ClubMember.objects.none().values_list("club_id", flat=True)

    return (
        ClubMember.objects.filter(user=user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_add_edit=True) | Q(permission_manage_auctions=True))
        .values_list("club_id", flat=True)
    )


def auctions_available_for_contact_autofill(user, extra_created_by=None):
    """Return auctions whose participant history can be used to auto-fill contact details.

    extra_created_by lets callers include auctions created by another user, even if the
    authenticated user would not otherwise have that auction in their own access scope.
    """
    if not user.is_authenticated:
        return Auction.objects.none()

    club_ids = club_ids_available_for_contact_autofill(user)
    filters = Q(created_by=user) | Q(auctiontos__is_admin=True, auctiontos__user=user) | Q(club_id__in=club_ids)
    if extra_created_by:
        filters |= Q(created_by=extra_created_by)
    return Auction.objects.filter(filters).distinct()


def _bap_leaderboard(club, field, current_member):
    """Return a leaderboard list for display on the club detail page.

    Each entry is a (rank, member, is_current_user) tuple.
    Top 10 are always included; if current_member is not in the top 10,
    they are appended at the end with their actual rank.
    Only members with points > 0 are ranked.
    """
    qs = ClubMember.objects.filter(club=club, is_deleted=False, **{f"{field}__gt": 0}).order_by(f"-{field}")
    top10 = list(qs[:10])
    result = [(i + 1, m, m == current_member) for i, m in enumerate(top10)]
    if current_member and current_member not in top10 and getattr(current_member, field, 0) > 0:
        rank = qs.filter(**{f"{field}__gt": getattr(current_member, field)}).count() + 1
        result.append((rank, current_member, True))
    return result


def _last_n_month_starts(count):
    month = timezone.now().date().replace(day=1)
    months = []
    for _ in range(count):
        months.append(month)
        if month.month == 1:
            month = month.replace(year=month.year - 1, month=12)
        else:
            month = month.replace(month=month.month - 1)
    return list(reversed(months))


def _ytd_month_starts():
    """Months from Jan 1 of the current year through the current month."""
    today = timezone.now().date()
    months = []
    month = today.replace(month=1, day=1)
    current = today.replace(day=1)
    while month <= current:
        months.append(month)
        if month.month == 12:
            month = month.replace(year=month.year + 1, month=1)
        else:
            month = month.replace(month=month.month + 1)
    return months


def _club_top10_chart_data(club, rank_field, award_field, current_member, months, is_ytd=False):
    """Cumulative points-over-time chart for the top 10 members.

    Color scheme: green = current_member, red = first place (when not current), blue = everyone else.
    Returns a Chart.js-compatible dict or None if no members have points.
    """
    top10 = list(
        ClubMember.objects.filter(club=club, is_deleted=False, **{f"{rank_field}__gt": 0}).order_by(f"-{rank_field}")[
            :10
        ]
    )
    if not top10:
        return None

    start_month = months[0]
    member_ids = [m.pk for m in top10]

    # Mirror BapAward.recalculate_member_points: awards tied to a deleted or banned lot
    # do not count toward a member's standings, so the chart must drop them too or its
    # running totals will disagree with the leaderboard numbers shown alongside it.
    awards = (
        BapAward.objects.filter(club_member_id__in=member_ids).exclude(lot__is_deleted=True).exclude(lot__banned=True)
    )

    monthly_awards = (
        awards.filter(date__gte=start_month)
        .annotate(month=TruncMonth("date"))
        .values("club_member_id", "month")
        .annotate(total=Sum(award_field))
        .order_by("club_member_id", "month")
    )
    member_monthly = collections.defaultdict(dict)
    for item in monthly_awards:
        month_key = (item["month"] if isinstance(item["month"], date_type) else item["month"].date()).replace(day=1)
        member_monthly[item["club_member_id"]][month_key] = item["total"] or 0

    if is_ytd:
        initial_totals = {}
    else:
        initial_qs = awards.filter(date__lt=start_month).values("club_member_id").annotate(total=Sum(award_field))
        initial_totals = {item["club_member_id"]: item["total"] or 0 for item in initial_qs}

    first_place = top10[0]
    datasets = []
    for m in top10:
        is_current = m == current_member
        is_first = m == first_place
        if is_current:
            color, width = "#198754", 2.5
        elif is_first:
            color, width = "#dc3545", 1.5
        else:
            color, width = "#0d6efd", 1.0

        running = initial_totals.get(m.pk, 0)
        data = []
        for month in months:
            running += member_monthly[m.pk].get(month, 0)
            data.append(running)

        datasets.append(
            {
                "label": str(m),
                "borderColor": color,
                "backgroundColor": "transparent",
                "fill": False,
                "data": data,
                "borderWidth": width,
                "pointRadius": 0,
            }
        )

    return {"labels": [m.strftime("%b %Y") for m in months], "datasets": datasets}


def _club_points_chart_data(club, member):
    if not member:
        return None

    months = _last_n_month_starts(60)
    start_month = months[0]
    monthly_awards = (
        BapAward.objects.filter(club_member=member, date__gte=start_month)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(
            bap_total=Sum("points"),
            hap_total=Sum("hap_points"),
            culture_total=Sum("cap_points"),
        )
        .order_by("month")
    )
    monthly_totals = {
        (item["month"] if isinstance(item["month"], date_type) else item["month"].date()).replace(day=1): {
            "bap": item["bap_total"] or 0,
            "hap": item["hap_total"] or 0,
            "culture": item["culture_total"] or 0,
        }
        for item in monthly_awards
    }
    initial_totals = BapAward.objects.filter(club_member=member, date__lt=start_month).aggregate(
        bap_total=Sum("points"),
        hap_total=Sum("hap_points"),
        culture_total=Sum("cap_points"),
    )

    running_bap = initial_totals["bap_total"] or 0
    running_hap = initial_totals["hap_total"] or 0
    running_culture = initial_totals["culture_total"] or 0
    bap_data = []
    hap_data = []
    culture_data = []

    for month in months:
        totals = monthly_totals.get(month, {})
        running_bap += totals.get("bap", 0)
        running_hap += totals.get("hap", 0)
        running_culture += totals.get("culture", 0)
        bap_data.append(running_bap)
        hap_data.append(running_hap)
        culture_data.append(running_culture)

    datasets = [
        {
            "label": "BAP",
            "borderColor": "#198754",
            "backgroundColor": "rgba(25, 135, 84, 0.15)",
            "fill": False,
            "data": bap_data,
        }
    ]
    if club.separate_hap:
        datasets.append(
            {
                "label": "HAP",
                "borderColor": "#0d6efd",
                "backgroundColor": "rgba(13, 110, 253, 0.15)",
                "fill": False,
                "data": hap_data,
            }
        )
    if club.separate_cap:
        datasets.append(
            {
                "label": "Culture",
                "borderColor": "#fd7e14",
                "backgroundColor": "rgba(253, 126, 20, 0.15)",
                "fill": False,
                "data": culture_data,
            }
        )

    return {"labels": [month.strftime("%b %Y") for month in months], "datasets": datasets}


CLUB_DETAIL_AUCTION_LIMIT = 10


class ClubViewMixin:
    """For club permissions, similar to AuctionViewMixin"""

    allow_non_admins = False
    club = None
    active_tab = None

    def get_club(self, slug):
        if not self.club and slug:
            self.club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
            if not self.club:
                raise Http404

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        self.record_last_club_used(request)
        return super().dispatch(request, *args, **kwargs)

    def record_last_club_used(self, request):
        """Remember which club this member most recently looked at so the command palette
        can scope its club shortcuts to it. Only members get tracked, and we only write when
        the value actually changes to keep this dispatch hook cheap."""
        user = request.user
        if not user.is_authenticated or not self.club:
            return
        if not ClubMember.objects.filter(club=self.club, user=user, is_deleted=False).exists():
            return
        UserData.objects.filter(user=user).exclude(last_club_used=self.club).update(last_club_used=self.club)

    def user_has_club_permission(self, permission_name):
        """Check if the current user has a specific permission for self.club"""
        return check_club_permission(self.request.user, self.club, permission_name)

    @property
    def can_edit_settings(self):
        return self.user_has_club_permission("permission_edit_club")

    @property
    def email_routing_enabled(self):
        return settings.SES_ROUTE_EMAILS_ENABLED

    @property
    def can_manage_bap(self):
        return self.user_has_club_permission("permission_manage_bap")

    @property
    def can_manage_money(self):
        return self.user_has_club_permission("permission_money") or self.user_has_club_permission(
            "permission_edit_club"
        )

    @property
    def can_access_admin(self):
        return self.user_has_club_permission("permission_admin") or self.user_has_club_permission("permission_view")

    @property
    def can_add_edit(self):
        return self.user_has_club_permission("permission_add_edit")

    @property
    def club_sidebar_club(self):
        """The club whose nav sidebar should render on this page (or None)."""
        return self.club

    @property
    def club_sidebar_can_view(self):
        """Whether the current user may see the club sidebar on a club page.
        Mirrors the union of permissions that gated the old club_ribbon tabs."""
        if not self.club:
            return False
        return bool(self.can_access_admin or self.can_edit_settings or self.can_manage_bap or self.can_manage_money)


class AdminOnlyViewMixin:
    """Include to make this view only visible to super users on the website
    Despite the name, this has nothing to do with auction admins"""

    permission_denied_message = "Only admins can view this page"
    redirect_url = "/"

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser):
            messages.error(request, self.permission_denied_message)
            return redirect(self.redirect_url)
        return super().dispatch(request, *args, **kwargs)


class AuctionStatsPermissionsMixin:
    """For graph classes"""

    @property
    def is_auction_admin(self):
        """Helper function used to check and see if request.user is the creator of the auction or is someone who has been made an admin of the auction.
        Returns False on no permission or True if the user has permission to access the auction"""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise Exception(msg)
        result = self.auction.permission_check(self.request.user)
        if not result:
            if not self.auction.make_stats_public:
                logger.debug("non-admins allowed")
                pass

            else:
                raise PermissionDenied()
        else:
            logger.debug("allowing user %s to view %s", self.request.user, self.auction)
            pass
        return result


class LocationMixin:
    """For location aware views, adds a `get_coordinates()` function which returns a tuple of `latitude, longitude` based on self.request.cookies or userdata

    get_coordinates() should be called before get_context_data
    make sure to set `view.no_location_message`"""

    # override this message in your view, it'll be shown to users without a location
    no_location_message = "Click here to set your location"

    # don't set this, it'll get set automatically by get_coordinates() if the user does not have a cookie
    _location_message = None

    def get_coordinates(self):
        try:
            latitude = float(self.request.COOKIES.get("latitude", 0))
            longitude = float(self.request.COOKIES.get("longitude", 0))
        except (ValueError, TypeError):
            latitude, longitude = 0, 0

        if latitude == 0 and longitude == 0:
            self._location_message = self.no_location_message

            if self.request.user.is_authenticated:
                latitude = self.request.user.userdata.latitude
                longitude = self.request.user.userdata.longitude
        return latitude, longitude

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["location_message"] = self._location_message
        return context


class ClickAd(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        try:
            campaignResponse = AdCampaignResponse.objects.get(responseid=self.kwargs["uuid"])
            campaignResponse.clicked = True
            campaignResponse.save()
            return campaignResponse.campaign.external_url
        except AdCampaignResponse.DoesNotExist:
            return None


class RenderAd(DetailView):
    """
    loaded async with js on ad.html, this view will spit out some raw html (with no css) suitable for displaying as part of a template
    """

    template_name = "ad_internal.html"
    model = AdCampaignResponse

    def get_object(self, *args, **kwargs):
        data = self.request.GET.copy()
        # request: user, category, auction
        category = None
        auction = None
        if self.request.user.is_authenticated:
            user = self.request.user
        else:
            user = None
        auction_slug = data.get("auction")
        if auction_slug:
            try:
                auction = Auction.objects.get(slug=auction_slug, is_deleted=False)
            except Auction.DoesNotExist:
                pass
        category_pk = data.get("category")
        if category_pk:
            try:
                category = Category.objects.get(pk=category_pk)
            except Category.DoesNotExist:
                pass
        if user and not category:
            # there wasn't a category on this page, pick one of the user's interests instead
            try:
                categories = UserInterestCategory.objects.filter(user=user).order_by("-as_percent")[:5]
                category = sample(categories, 1)
            except (IndexError, ValueError):
                pass
        adCampaigns = (
            AdCampaign.objects.filter(begin_date__lte=timezone.now())
            .filter(Q(end_date__gte=timezone.now()) | Q(end_date__isnull=True))
            .order_by("-bid")
        )
        if auction:
            adCampaigns = adCampaigns.filter(Q(auction__isnull=True) | Q(auction=auction.pk))
        total = adCampaigns.count()
        chanceOfGoogleAd = 50
        if uniform(0, 100) < chanceOfGoogleAd:
            return None
        for campaign in adCampaigns:
            if campaign.category == category:
                campaign.bid = campaign.bid * 2  # Better chance for matching category.  Don't save after this
            if campaign.bid > uniform(0, total - 1):
                if campaign.number_of_clicks > campaign.max_clicks or campaign.number_of_impressions > campaign.max_ads:
                    logger.debug("not selected -- limit exceeded")
                    pass
                else:
                    return AdCampaignResponse.objects.create(
                        user=user, campaign=campaign
                    )  # fixme, session here: request.session.session_key


class LotListView(AjaxListView):
    """This is a base class that shows lots, with a filter.  This class is never used directly, but it's a parent for several other classes.
    The context is overridden to set the view type"""

    model = Lot
    template_name = "all_lots.html"
    auction = None
    # to display the banner telling users why they are not seeing lots for all auctions
    routeByLastAuction = False

    def get_page_template(self):
        if self.request.user.is_authenticated and self.request.user.userdata.use_list_view:
            return "lot_list_page.html"
        return "lot_tile_page.html"  # tile view as default
        # return 'lot_list_page.html' # list view as default

    def get_context_data(self, **kwargs):
        # set default values
        data = self.request.GET.copy()
        # if len(data) == 0:
        #    data['status'] = "open" # this would show only open lots by default
        context = super().get_context_data(**kwargs)
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        # gotta check to make sure we're not trying to filter by an auction, or no auction
        if "auction" in data.keys():
            # now we have tried to search for something, so we should not override the auction
            self.auction = None
        context["routeByLastAuction"] = self.routeByLastAuction
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        context["embed"] = "all_lots"
        if self.request.user.is_authenticated:
            context["lotsAreHidden"] = len(UserIgnoreCategory.objects.filter(user=self.request.user))
        else:
            # probably not signed in
            context["lotsAreHidden"] = -1
        if self.request.user.is_authenticated:
            try:
                context["lastView"] = (
                    PageView.objects.filter(user=self.request.user, lot_number__isnull=False)
                    .order_by("-date_start")[0]
                    .date_start
                )
            except IndexError:
                context["lastView"] = timezone.now()
        else:
            context["lastView"] = timezone.now()
        auction_slug = data.get("auction")
        if auction_slug:
            try:
                context["auction"] = Auction.objects.get(slug=auction_slug, is_deleted=False)
            except Auction.DoesNotExist:
                a_slug = data.get("a")
                if a_slug:
                    try:
                        context["auction"] = Auction.objects.get(slug=a_slug, is_deleted=False)
                    except Auction.DoesNotExist:
                        context["auction"] = self.auction
                        context["no_filters"] = True
                else:
                    context["auction"] = self.auction
                    context["no_filters"] = True
        else:
            context["auction"] = self.auction
            if not auction_slug:
                context["no_filters"] = True
        if context["auction"]:
            if self.request.user.is_authenticated:
                context["auction_tos"] = AuctionTOS.objects.filter(
                    auction=context["auction"].pk, user=self.request.user.pk
                ).first()
            #     # this message gets added to every scroll event.  Also, it's just noise
            #     messages.error(self.request, f"Please <a href='/auctions/{context['auction'].slug}/'>read the auction's rules and confirm your pickup location</a> to bid")
        else:
            # this will be a mix of auction and non-auction lots
            context["display_auction_on_lots"] = True
        if not self.request.COOKIES.get("longitude"):
            context["location_message"] = "Set your location to see lots near you"
        context["src"] = "lot_list"
        return context


class LotAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        if result.high_bidder:
            return format_html(
                '<b>{}</b>: {}<br><small>High bidder:<span class="text-warning">{} (${})</span></small>',
                result.lot_number_display,
                result.lot_name,
                result.high_bidder_for_admins,
                result.high_bid,
            )
        else:
            return format_html("<b>{}</b>: {}", result.lot_number_display, result.lot_name)

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except Auction.DoesNotExist:
            return Lot.objects.none()
        if not auction.permission_check(self.request.user):
            return Lot.objects.none()
        # only this auction
        qs = Lot.objects.exclude(is_deleted=True).filter(auction=auction)
        # winner not alrady set
        qs = qs.filter(auctiontos_winner__isnull=True)
        # not removed
        qs = qs.filter(banned=False)
        if self.q:
            qs = LotAdminFilter.generic(self, qs, self.q)
        return qs


class AuctionTOSAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    def get_result_label(self, result):
        return format_html("<b>{}</b>: {}", result.bidder_number, result.name)

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        auction = self.forwarded.get("auction")
        invoice = self.forwarded.get("invoice")
        exclude_auctiontos = self.forwarded.get("exclude_auctiontos")
        try:
            auction = Auction.objects.get(pk=auction, is_deleted=False)
        except Auction.DoesNotExist:
            return AuctionTOS.objects.none()
        if not auction.permission_check(self.request.user):
            return AuctionTOS.objects.none()
        qs = AuctionTOS.objects.filter(auction=auction)
        if exclude_auctiontos:
            try:
                qs = qs.exclude(pk=int(exclude_auctiontos))
            except (ValueError, TypeError):
                pass
        if invoice:
            qs = qs.exclude(
                Exists(
                    Invoice.objects.filter(
                        Q(status="PAID") | Q(status="READY"),
                        auctiontos_user=OuterRef("pk"),
                    )
                )
            )
        if self.q:
            qs = AuctionTOSFilter.generic(self, qs, self.q)
        return qs.order_by("-name")


class ClubMemberAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for ClubMember — scoped to a forwarded club slug, BAP admins only."""

    def get_result_label(self, result):
        email = f" ({result.email})" if result.email else ""
        return format_html("{}{}", str(result), email)

    def get_queryset(self):
        slug = self.forwarded.get("club_slug", "")
        if not slug:
            return ClubMember.objects.none()
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).first()
        if not club or not check_club_permission(self.request.user, club, "permission_manage_bap"):
            return ClubMember.objects.none()
        qs = ClubMember.objects.filter(club=club, is_deleted=False).order_by("name")
        if self.forwarded.get("require_membership_number"):
            qs = qs.filter(membership_number__isnull=False)
        if self.q:
            qs = qs.filter(Q(name__icontains=self.q) | Q(email__icontains=self.q))
        return qs


class ClubMemberMergeAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for the club-member merge target selector.

    Forwards: club_slug, exclude_member (pk of the source being merged away).
    Includes both active and deactivated members; labels deactivated ones.
    Requires permission_add_edit on the club.
    """

    def get_result_label(self, result):
        label = str(result)
        email = f" ({result.email})" if result.email else ""
        suffix = " (Deactivated)" if result.is_deleted else ""
        return format_html("{}{}{}", label, email, suffix)

    def get_queryset(self):
        slug = self.forwarded.get("club_slug", "")
        exclude_pk = self.forwarded.get("exclude_member")
        if not slug:
            return ClubMember.objects.none()
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).first()
        if not club or not check_club_permission(self.request.user, club, "permission_add_edit"):
            return ClubMember.objects.none()
        qs = ClubMember.objects.filter(club=club).order_by("is_deleted", "name")
        if exclude_pk:
            try:
                qs = qs.exclude(pk=int(exclude_pk))
            except (ValueError, TypeError):
                pass
        if self.q:
            qs = qs.filter(Q(name__icontains=self.q) | Q(email__icontains=self.q))
        return qs


class CategoryAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for all categories (used in BAP category override form)."""

    def get_queryset(self):
        qs = Category.objects.all().order_by("name")
        if self.q:
            qs = qs.filter(name__icontains=self.q)
        return qs


class AuctionAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Autocomplete for auctions that the current user is an admin of"""

    def get_result_label(self, result):
        return format_html("{}", result.title)

    def get_result_value(self, result):
        """Return slug instead of PK for the value"""
        return result.slug

    def get_queryset(self):
        # Base: auctions where user is creator or admin
        qs = (
            Auction.objects.filter(
                Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                is_deleted=False,
            )
            .distinct()
            .order_by("-date_start")
        )

        # Exclude the current auction if provided (via DAL forwarded params or plain query params)
        current_slug = (
            self.forwarded.get("current_slug")
            or self.request.GET.get("current")
            or self.request.GET.get("exclude")
            or self.request.GET.get("slug")
        )
        current_pk = self.forwarded.get("current_pk") or self.request.GET.get("current_pk")

        if current_slug:
            qs = qs.exclude(slug=current_slug)
        if current_pk:
            try:
                qs = qs.exclude(pk=int(current_pk))
            except (TypeError, ValueError):
                pass

        if self.q:
            qs = qs.filter(Q(title__icontains=self.q) | Q(slug__icontains=self.q))

        return qs


class LotQRView(RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        lot = Lot.objects.filter(pk=self.kwargs["pk"]).first()
        if lot:
            return f"{lot.lot_link}?src=qr"
        return None


class AllRecommendedLots(TemplateView):
    """
    Show all recommended lots as a standalone page
    Lots are loaded async on the template via javascript
    """

    template_name = "recommended_lots.html"


class RecommendedLots(ListView):
    """
    Return a somewhat random list of lots that have not been seen by the current user.
    This is rendered html ready to embed in another view
    It shouldn't really be called directly as there's no CSS in the templates
    """

    model = Lot

    def get_template_names(self):
        try:
            userData = UserData.objects.get(user=self.request.user.pk)
            if userData.use_list_view:
                return "lot_list_page.html"
            else:
                return "lot_tile_page.html"
        except (UserData.DoesNotExist, AttributeError):
            pass
        return "lot_tile_page.html"  # tile view as default

    def get_queryset(self):
        data = self.request.GET.copy()
        auction = data.get("auction")
        try:
            qty = int(data.get("qty", 10))
        except (ValueError, TypeError):
            qty = 10
        keywords = []
        keywords_string = data.get("keywords", "")
        if keywords_string:
            keywords_string = keywords_string.lower()
            lotWords = re.findall("[A-Z|a-z]{3,}", keywords_string)
            for word in lotWords:
                if word not in settings.IGNORE_WORDS:
                    keywords.append(word)
        try:
            exclude_pk = int(data.get("exclude")) if data.get("exclude") else None
        except (ValueError, TypeError):
            exclude_pk = None
        return get_recommended_lots(
            user=self.request.user, auction=auction, qty=qty, keywords=keywords, exclude_pk=exclude_pk
        )

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context["embed"] = data.get("embed", "standalone_page")
        if self.request.user.is_authenticated:
            try:
                context["lastView"] = (
                    PageView.objects.filter(user=self.request.user).order_by("-date_start")[0].date_start
                )
            except IndexError:
                context["lastView"] = timezone.now()
        else:
            context["lastView"] = timezone.now()
        context["src"] = "recommended"
        return context


class MyWonLots(LotListView):
    """Show all lots won by the current user"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["status"] = "closed"
        context = super().get_context_data(**kwargs)
        context["filter"] = UserWonLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context["lot_view_type"] = "mywonlots"
        context["lotsAreHidden"] = -1
        return context


class MyBids(LotListView):
    """Show all lots the current user has bid on"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        context["filter"] = UserBidLotFilter(data, queryset=self.get_queryset(), request=self.request, ignore=False)
        context["lot_view_type"] = "mybids"
        context["lotsAreHidden"] = -1
        return context


class MyLots(HTMxTableView):
    """Selling dashboard.  List of lots added by this user."""

    model = Lot
    table_class = LotHTMxTableForUsers
    filterset_class = LotAdminFilter
    template_name = "auctions/lot_user.html"
    htmx_table_header_template = "auctions/partials/lot_user_table_header.html"
    # paginate_by = 100

    def dispatch(self, request, *args, **kwargs):
        # Legacy ?filter=X bookmarks: canonicalize to ?query=X so the shared HTMX
        # template's input pre-populates and its URL-sync stays consistent.
        if "query" not in request.GET and request.GET.get("filter") and not request.htmx:
            params = request.GET.copy()
            params["query"] = params.pop("filter")[0]
            return HttpResponseRedirect(f"{request.path}?{params.urlencode()}")
        filter_value = request.GET.get("query", "").strip().lower()
        qs = UserLotFilter(request=request).qs
        if filter_value == "bap":
            qs = qs.select_related("bap_award__club_member__club").annotate(
                show_bap_badge=Value(True, output_field=BooleanField())
            )
        self.queryset = qs
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["userdata"] = self.request.user.userdata
        context["website_focus"] = settings.WEBSITE_FOCUS
        context["filter_bap"] = self.request.GET.get("query", "").strip().lower() == "bap"
        return context

    def get(self, *args, **kwargs):
        if not self.request.htmx:
            if self.request.user.userdata.unnotified_subscriptions_count:
                msg = f"You've got {self.request.user.userdata.unnotified_subscriptions_count} lot"
                if self.request.user.userdata.unnotified_subscriptions_count > 1:
                    msg += "s"
                msg += (
                    f""" with new messages.  <a href="{reverse("messages")}">Go to your messages page to see them</a>"""
                )
                messages.info(self.request, msg)
        return super().get(*args, **kwargs)


class MyWatched(LotListView):
    """Show all lots watched by the current user"""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter"] = UserWatchLotFilter(
            self.request.GET,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=False,
        )
        context["lot_view_type"] = "watch"
        context["lotsAreHidden"] = -1
        return context


class LotsByUser(LotListView):
    """Show all lots for the user specified in the filter"""

    def get_context_data(self, **kwargs):
        data = self.request.GET.copy()
        context = super().get_context_data(**kwargs)
        username = data.get("user")
        if username:
            try:
                context["user"] = User.objects.get(username=username)
                context["lot_view_type"] = "user"
            except User.DoesNotExist:
                context["user"] = None
        else:
            context["user"] = None
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingUser=context["user"],
        )

        return context


class WatchOrUnwatch(APIView):
    """Watch or unwatch a lot - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        watch = request.POST.get("watch", "")
        user = request.user
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return HttpResponse("Failure")
        obj = Watch.objects.filter(lot_number=lot, user=user).first()
        if not obj:
            obj = Watch.objects.create(lot_number=lot, user=user)
        if watch == "false":  # string not bool...
            obj.delete()
        if obj:
            return HttpResponse("Success")
        else:
            return HttpResponse("Failure")


class PlaceBid(APIView):
    """Place a bid over HTTP - POST only.

    Bidding used to happen entirely over the lot websocket, which meant a dropped
    or stalled socket could silently lose a bid. This endpoint persists the bid via
    a normal request and then broadcasts the result over the websocket as before, so
    the user experience is unchanged but the bid no longer depends on the socket.

    The client does not need to parse this response -- it keeps listening on the
    websocket for the broadcast -- but we return the result for robustness/tests.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if not lot:
            return JsonResponse({"type": "ERROR", "message": "Lot not found"}, status=404)
        # Persist the bid first (best-effort websocket broadcast happens inside).
        result = place_bid_and_broadcast(lot, request.user, request.POST.get("bid"))
        high_bid = result.get("current_high_bid")
        if isinstance(high_bid, Decimal):
            high_bid = float(high_bid)
        # Always 200 for a processed bid (including validation errors like "bid too
        # low"): those are surfaced to the user via the websocket broadcast, so a
        # non-2xx here would make the client show a second, generic error toast.
        return JsonResponse(
            {
                "type": result["type"],
                "message": result["message"],
                "current_high_bid": high_bid,
                "high_bidder_pk": result["high_bidder_pk"],
            }
        )


class LotNotifications(APIView):
    """Get count of new lot notifications - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        new = (
            LotHistory.objects.filter(lot__user=user.pk, seen=False, changed_price=False)
            .exclude(user=request.user)
            .count()
        )
        if not new:
            new = ""
        return JsonResponse(data={"new": new})


class IgnoreAuction(APIView):
    """Ignore an auction - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        auction = request.POST.get("auction", "")
        user = request.user
        if not auction:
            return HttpResponse("Failure: auction parameter required")
        try:
            auction = Auction.objects.get(slug=auction, is_deleted=False)
            obj, created = AuctionIgnore.objects.update_or_create(
                auction=auction,
                user=user,
                defaults={},
            )
            return HttpResponse("Success")
        except Exception as e:
            return HttpResponse(f"Failure: {e}")


class NoLotAuctions(APIView):
    """POST-only method that returns an empty string if most recent auction you've used accepts lots
    or the name of the auction and the end date
    Used on the lot creation form"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        result = ""
        auction = request.user.userdata.last_auction_used
        now = timezone.now()
        if auction:
            if auction.lot_submission_start_date > now:
                result = f"Lot submission is not yet open for {auction}"
            if auction.lot_submission_end_date < now:
                result = f"Lot submission has ended for {auction}"
            if auction.date_end:
                if auction.date_end < now:
                    result = f"{auction} has ended"
            if not result:
                tos = AuctionTOS.objects.filter(user=request.user, auction=auction).first()
                if tos:
                    if not tos.selling_allowed:
                        result = f"You don't have permission to add lots to {auction}"
            if not result:
                if auction.max_lots_per_user:
                    lot_list = Lot.objects.filter(
                        user=request.user,
                        banned=False,
                        deactivated=False,
                        auction=auction,
                        is_deleted=False,
                    )
                    if auction.allow_additional_lots_as_donation:
                        lot_list = lot_list.filter(donation=False)
                    lot_list = lot_list.count()
                    result = f"You've added {lot_list} of {auction.max_lots_per_user} lots to {auction}"
        if result:
            result += "<br>"
        return JsonResponse(
            data={
                "result": result,
            }
        )


class AuctionNotifications(APIView):
    """
    POST-only method that will return a count of auctions as well as some info about the closest one.
    This is mostly a wrapper to go around models.nearby_auctions so that all info isn't accessible to anyone
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        new = 0
        name = ""
        link = ""
        slug = ""
        distance = 0
        latitude = request.COOKIES.get("latitude")
        longitude = request.COOKIES.get("longitude")
        if not latitude or not longitude:
            if request.user.is_authenticated:
                if request.user.userdata.latitude:
                    latitude = request.user.userdata.latitude
                    longitude = request.user.userdata.longitude
        try:
            distance = 100
            if request.user.is_authenticated:
                distance = request.user.userdata.email_me_about_new_auctions_distance
            if not distance:
                distance = 100
            auctions, distances = nearby_auctions(latitude, longitude, distance, user=request.user)
            new = len(auctions)
            if auctions:
                name = str(auctions[0])
                link = auctions[0].get_absolute_url()
                slug = auctions[0].slug
                distance = distances[0]
        except Exception:
            pass
        if not new:
            new = ""
        # Convert distance to user's preferred unit
        distance_value = distance
        distance_unit = "miles"
        if request.user.is_authenticated:
            try:
                user_unit = request.user.userdata.distance_unit
                if user_unit == "km":
                    distance_value = round(distance * MILES_TO_KM)
                    distance_unit = "km"
                else:
                    distance_value = round(distance)
            except AttributeError:
                distance_value = round(distance)
        else:
            distance_value = round(distance)
        return JsonResponse(
            data={
                "new": new,
                "name": name,
                "link": link,
                "slug": slug,
                "distance": distance_value,
                "distance_unit": distance_unit,
            }
        )


class SetCoordinates(APIView):
    """Set user location coordinates - POST only.  I don't think this is used anywhere any more"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.userdata.location_coordinates = f"{request.POST['latitude']},{request.POST['longitude']}"
        request.user.userdata.save()
        return HttpResponse("Success")


class CreateUserBan(APIView):
    """Ban a user - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        auctionsList = Auction.objects.exclude(is_deleted=True).filter(created_by=user.pk)
        # delete all bids the banned user has made on active lots or in active auctions created by the request user
        bids = Bid.objects.exclude(is_deleted=True).filter(user=bannedUser)
        for bid in bids:
            lot = Lot.objects.get(pk=bid.lot_number.pk, is_deleted=False)
            if lot.user == user or lot.auction in auctionsList:
                if not lot.ended:
                    logger.info("Deleting bid %s", str(bid))
                    bid.delete()
        # ban all lots added by the banned user.  These are not deleted, just removed from the auction
        for auction in auctionsList:
            buy_now_lots = Lot.objects.exclude(is_deleted=True).filter(winner=bannedUser, auction=auction.pk)
            for lot in buy_now_lots:
                lot.winner = None
                lot.winning_price = None
                lot.save()
            lots = Lot.objects.exclude(is_deleted=True).filter(user=bannedUser, auction=auction.pk)
            for lot in lots:
                if not lot.ended:
                    logger.info("User %s has banned lot %s", str(user), lot)
                    lot.banned = True
                    lot.ban_reason = "The seller of this lot has been banned from this auction"
                    lot.save()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class LotDeactivate(APIView):
    """Deactivate or activate a lot - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        lot = Lot.objects.get(pk=pk, is_deleted=False)

        # Check permissions: lot owner or superuser can deactivate
        # Lots in auctions cannot be deactivated
        if lot.auction:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect(reverse("home"))

        if lot.user.pk != request.user.pk and not request.user.is_superuser:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect(reverse("home"))

        if lot.deactivated:
            lot.deactivated = False
        else:
            bids = Bid.objects.exclude(is_deleted=True).filter(lot_number=lot.lot_number)
            for bid in bids:
                bid.delete()
            lot.deactivated = True
        lot.save()
        return HttpResponse("success")


class UserUnban(APIView):
    """Unban a user - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        obj.delete()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class ImagesPrimary(APIView):
    """Make the specified image the default image for the lot
    Takes pk of image as post param
    this does not check lot.can_add_images, which is deliberate (who cares if you rotate...)
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            lotImage = LotImage.objects.get(pk=int(request.POST["pk"]))
        except (LotImage.DoesNotExist, ValueError, KeyError):
            return HttpResponse("Image not found, specify a valid pk")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can change images")
            return redirect(reverse("home"))
        LotImage.objects.filter(lot_number=lotImage.lot_number.pk).update(is_primary=False)
        lotImage.is_primary = True
        lotImage.save()
        return HttpResponse("Success")


class ImagesRotate(APIView):
    """Rotate an image associated with a lot
    Takes pk of image and angle as post params
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            pk = int(request.POST["pk"])
            angle = int(request.POST["angle"])
        except (KeyError, ValueError):
            return HttpResponse("user, pk, and angle are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except LotImage.DoesNotExist:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can rotate images")
            return redirect(reverse("home"))
        if not lotImage.image:
            return HttpResponse("No image")
        temp_image = Image.open(BytesIO(lotImage.image.read()))
        temp_image = temp_image.rotate(angle, expand=True)
        if temp_image.mode in ("RGBA", "P"):
            temp_image = temp_image.convert("RGB")
        output = BytesIO()
        temp_image.save(output, format="JPEG", quality=85)
        output.seek(0)
        # Overwrite the original image
        lotImage.image.save(
            lotImage.image.name.replace("images/", ""),
            ContentFile(output.read()),
            save=True,
        )
        return HttpResponse("Success")


class Feedback(APIView):
    """Leave feedback on a lot
    This can be done as a buyer or a seller
    api/feedback/lot_number/buyer
    api/feedback/lot_number/seller
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, leave_as):
        data = request.POST
        try:
            lot = Lot.objects.get(pk=pk, is_deleted=False)
        except Lot.DoesNotExist:
            msg = f"No lot found with key {pk}"
            raise Http404(msg)
        winner_checks_pass = False
        seller_checks_pass = False
        if leave_as == "winner":
            if lot.winner:
                if lot.winner.pk == request.user.pk:
                    winner_checks_pass = True
            if lot.auctiontos_winner:
                if lot.auctiontos_winner.user:
                    if (lot.auctiontos_winner.user.pk == request.user.pk) or (
                        lot.auctiontos_winner.email == request.user.email
                    ):
                        winner_checks_pass = True
        if winner_checks_pass:
            rating = data.get("rating")
            if rating:
                lot.feedback_rating = rating
                lot.save()
            text = data.get("text")
            if text:
                # Truncate text to max length to prevent database errors
                lot.feedback_text = text[:FEEDBACK_TEXT_MAX_LENGTH]
                lot.save()
        if leave_as == "seller":
            if lot.user:
                if lot.user.pk == request.user.pk:
                    seller_checks_pass = True
            if lot.auctiontos_seller:
                if lot.auctiontos_seller.user:
                    if lot.auctiontos_seller.user.pk == request.user.pk:
                        seller_checks_pass = True
        if seller_checks_pass:
            rating = data.get("rating")
            if rating:
                lot.winner_feedback_rating = rating
                lot.save()
            text = data.get("text")
            if text:
                # Truncate text to max length to prevent database errors
                lot.winner_feedback_text = text[:FEEDBACK_TEXT_MAX_LENGTH]
                lot.save()
        if not winner_checks_pass and not seller_checks_pass:
            messages.error(request, "Only the seller or winner of a lot can leave feedback")
            return redirect(reverse("home"))
        return HttpResponse("Success")


def clean_referrer(url):
    """Make a URL more human readable"""
    if not url:
        url = ""
    url = re.sub(r"^https?://", "", url)  # no http/s at the beginning
    if Site.objects.get_current().domain not in url:
        url = re.sub(r"\?.*", "", url)  # remove get params
    url = re.sub(r"^www\.", "", url)  # www
    url = re.sub(r"/+$", "", url)  # trailing /
    # if someone has facebook.example.com, it would be recorded as FB...
    # can update this if it becomes an issue
    if re.search(r"(facebook)\.", url):
        url = "Facebook"
    if re.search(r"(google)\.", url):
        url = "Google"
    return url


class PageViewCreate(APIView):
    """Record page views"""

    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.POST
        auction = data.get("auction", None)
        if auction:
            auction = Auction.objects.filter(pk=auction).first()
        lot_number = data.get("lot", None)
        if lot_number:
            lot_number = Lot.objects.filter(pk=lot_number, is_deleted=False).first()
        url = data.get("url", None)
        url_without_params = re.sub(r"\?.*", "", url)
        url_without_params = url_without_params[:600]
        first_view = data.get("first_view", False)
        if request.user.is_authenticated:
            user = request.user
            session_id = None
        else:
            # anonymous users go by session
            user = None
            # saving the session will force key generation
            if not request.session.session_key:
                request.session.save()
            session_id = request.session.session_key
        if first_view == "true":  # good ol Javascript
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            # platform = 'UNKNOWN'
            os = "UNKNOWN"
            parsed_ua = parse(user_agent)
            # if parsed_ua.is_mobile:
            #     platform = 'MOBILE'
            # if parsed_ua.is_tablet:
            #     platform = 'TABLET'
            # elif parsed_ua.is_pc:
            #     platform = 'DESKTOP'
            user_agent = user_agent[:200]
            referrer = clean_referrer(data.get("referrer", None)[:600])
            source = data.get("src", None)
            uid = data.get("uid", None)
            # mark auction campaign results if applicable present
            ip = ""
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]
            else:
                ip = request.META.get("REMOTE_ADDR")
            if uid:  # and not request.user.is_authenticated:
                userdata = UserData.objects.filter(unsubscribe_link=uid).first()
                if userdata:
                    userdata.last_activity = timezone.now()
                    userdata.save()
            if source:
                campaign = AuctionCampaign.objects.filter(uuid=source).first()
                if campaign and campaign.result == "NONE":
                    campaign.result = "VIEWED"
                    campaign.save()
                if campaign and campaign.user is not None and campaign.auction is not None:
                    tos = AuctionTOS.objects.filter(user=campaign.user, auction=campaign.auction).first()
                    if tos:
                        campaign.result = "JOINED"
                        campaign.save()
            if "Googlebot" not in user_agent and "Baiduspider" not in user_agent:
                PageView.objects.create(
                    lot_number=lot_number,
                    url=url_without_params,
                    auction=auction,
                    session_id=session_id,
                    user=user,
                    user_agent=user_agent,
                    ip_address=ip[:100],
                    platform=parsed_ua.os.family,
                    os=os,
                    referrer=referrer[:600],
                    title=data.get("title", "")[:600],
                    source=source,
                )
                if user:
                    UserData.objects.filter(user=user).update(last_activity=timezone.now())
            if user and lot_number and lot_number.species_category:
                # create/increment interest in this category for this view
                UserInterestCategory.add_interest(user, lot_number.species_category, settings.VIEW_WEIGHT)
            if auction and user:
                if not source:
                    source = referrer
                try:
                    campaign = AuctionCampaign.objects.create(
                        auction=auction,
                        user=user,
                        email=user.email,
                        source=source[:200],
                    )
                except ValidationError:
                    # campaign already exists
                    pass
        # code below would run on subsequent pageviews.  Not worth the extra server effort for an update every 10 seconds.
        # some corresponding js on base_page_view.html is also commented out
        # else:
        #     pageview = PageView.objects.filter(
        #         url = url_without_params,
        #         session_id = session_id,
        #         user = user,
        #     ).order_by('-date_start').first()
        #     if pageview:
        #         # this is the second (or more) time this user has viewed this page
        #         pageview.total_time += 10
        #         pageview.date_end = timezone.now()
        #         pageview.save()
        return HttpResponse("Success")


class InvoicePaid(APIView):
    """Mark an invoice as paid/ready/open - POST only

    Accessible via two URL patterns:
    - /api/payinvoice/<int:pk>/<str:status>: requires an authenticated auction admin
    - /api/payinvoice/<uuid:uuid>/<str:status>: accepts the invoice's no-login UUID, no login required
    """

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [AllowAny]  # Auth is enforced manually in post()

    def post(self, request, *args, **kwargs):
        if "uuid" in kwargs:
            # UUID-based access: anyone with the invoice's no-login link may update status
            invoice = get_object_or_404(Invoice, no_login_link=kwargs["uuid"])
            auction = invoice.auction
        else:
            # PK-based access: requires an authenticated admin
            if not request.user.is_authenticated:
                raise NotAuthenticated()
            invoice = get_object_or_404(Invoice, pk=kwargs["pk"])
            auction = invoice.auction
            if auction:
                if not auction.permission_check(request.user):
                    raise PermissionDenied()
            elif invoice.club:
                # Renewal-only invoice (no auction): check club permission
                if not check_club_permission(request.user, invoice.club, "permission_add_edit"):
                    raise PermissionDenied()
            else:
                raise PermissionDenied()
        new_status = kwargs["status"]
        if new_status in ("PAID", "UNPAID") and not invoice.renewal_needed:
            _ensure_invoice_renewal_state(invoice)
        # Core: persist the new invoice status. Everything else is "extra"
        # and must not be allowed to block the status change.
        invoice.status = new_status
        run_at = None
        if new_status in ("UNPAID", "PAID"):
            run_at = timezone.now() + timedelta(seconds=INVOICE_NOTIFICATION_DELAY_SECONDS)
            invoice.invoice_notification_due = run_at
        elif new_status == "DRAFT":
            invoice.invoice_notification_due = None
        invoice.save()
        try:
            if run_at:
                schedule_invoice_notification(invoice.pk, run_at)
            elif new_status == "DRAFT":
                cancel_invoice_notification(invoice.pk)
        except Exception:
            logger.exception("schedule/cancel invoice notification failed for invoice %s", invoice.pk)
        if new_status == "PAID":
            try:
                _process_invoice_membership_renewal(
                    invoice, acting_user=request.user if request.user.is_authenticated else None
                )
            except Exception:
                logger.exception("invoice membership renewal failed for invoice %s", invoice.pk)
            try:
                buyer_tos = getattr(invoice, "auctiontos_user", None)
                if buyer_tos and buyer_tos.clubmember_id:
                    buyer_tos.clubmember.update_last_club_activity()
            except Exception:
                logger.exception("last_club_activity update failed for invoice %s buyer", invoice.pk)
        user = request.user if request.user.is_authenticated else None
        # Club-only renewal invoices have no auction (and no auctiontos_user); skip the
        # auction history entry rather than raising/logging an AttributeError every time.
        if auction and invoice.auctiontos_user:
            try:
                auction.create_history(
                    applies_to="INVOICES",
                    action=f"Set invoice for {invoice.auctiontos_user.name} to {invoice.get_status_display()}",
                    user=user,
                )
            except Exception:
                logger.exception("create_history failed for invoice %s", invoice.pk)
        is_admin = "pk" in kwargs  # UUID-based access is member-facing, not admin
        buttons_html = render_to_string("invoice_buttons.html", {"invoice": invoice})
        renewal_ctx = {"invoice": invoice, "is_admin": is_admin}
        renewal_html = render_to_string("auctions/partials/invoice_membership_renewal.html", renewal_ctx)
        # Include the renewal section as an OOB swap so the locked/unlocked visual
        # state and the "already processed" warning reflect the new invoice state
        # immediately without requiring a page reload.
        renewal_oob = ""
        if 'id="invoice-membership-renewal"' in renewal_html:
            renewal_oob = renewal_html.replace(
                '<div id="invoice-membership-renewal"',
                '<div hx-swap-oob="outerHTML" id="invoice-membership-renewal"',
                1,
            )
        return HttpResponse(buttons_html + renewal_oob, status=200)


class APIPostView(APIView):
    """POST only method to do stuff, logged in users only"""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        raise NotImplementedError()


class InvoiceRenewalNeededToggleView(APIView):
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        invoice = get_object_or_404(Invoice, pk=pk)
        if invoice.auction:
            if not invoice.auction.permission_check(request.user):
                raise PermissionDenied()
        elif invoice.club:
            if not check_club_permission(request.user, invoice.club, "permission_add_edit"):
                raise PermissionDenied()
        else:
            raise PermissionDenied()
        if invoice.renewal_processed:
            return HttpResponseBadRequest("Renewal already processed for this invoice.")
        renewal_needed = str(request.POST.get("renewal_needed", "")).lower() in ("1", "true", "on", "yes")
        invoice.renewal_needed = renewal_needed
        invoice.renewal_manually_set = True
        invoice.save(update_fields=["renewal_needed", "renewal_manually_set"])
        # Checking the box makes the user a club member for this invoice: apply the club
        # member discount and (in club member discount split mode) the alternate split.
        _sync_tos_alternate_split(invoice.auctiontos_user, invoice)
        invoice.recalculate()
        ctx = {"invoice": invoice, "is_admin": True, "csrf_token": get_token(request)}
        body = render_to_string("auctions/partials/invoice_membership_renewal.html", ctx, request=request)
        # OOB swaps so the invoice fee row, discount row, tax row, final total, and quick-checkout
        # summary all update in real time when the box is toggled.
        fee_row = render_to_string("auctions/partials/invoice_membership_fee_row.html", ctx, request=request)
        discount_row = render_to_string("auctions/partials/invoice_club_member_discount_row.html", ctx, request=request)
        tax_row = render_to_string("auctions/partials/invoice_tax_row.html", ctx, request=request)
        total_row = render_to_string("auctions/partials/invoice_final_total_row.html", ctx, request=request)
        oob_fee = fee_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_discount = discount_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_tax = tax_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_total = total_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        # Wrap <tr> OOB swaps in <table> so the browser's HTML parser does not discard
        # them when they appear outside a table context, while still letting htmx find
        # and process the hx-swap-oob attribute (unlike <template>, whose content is
        # inert and not reachable by querySelectorAll).
        oob_fee = f"<table>{oob_fee}</table>"
        oob_discount = f"<table>{oob_discount}</table>"
        oob_tax = f"<table>{oob_tax}</table>"
        oob_total = f"<table>{oob_total}</table>"
        oob_summary_checkout = (
            f'<span id="quick-checkout-invoice-summary" hx-swap-oob="outerHTML">{invoice.invoice_summary_short}</span>'
        )
        # Also update the invoice-summary-short span on the full invoice page (invoice.html)
        oob_summary_invoice = (
            f'<span id="invoice-summary-short" hx-swap-oob="outerHTML">{invoice.invoice_summary_short}</span>'
        )
        # Update the modal title (generic_admin_form.html) when the renewal checkbox is toggled
        # while the auctiontos/clubmember admin modal is open.
        modal_name = invoice.invoice_summary
        oob_modal_title = f'<h5 class="modal-title" id="modal-invoice-title" hx-swap-oob="outerHTML">{modal_name}</h5>'
        response = HttpResponse(
            body
            + oob_fee
            + oob_discount
            + oob_tax
            + oob_total
            + oob_summary_checkout
            + oob_summary_invoice
            + oob_modal_title
        )
        # Signal the quick-checkout page to regenerate QR codes now that the total has changed.
        response["HX-Trigger"] = "renewalToggled"
        return response


class UpdateLotPushNotificationsView(APIPostView):
    def post(self, request, *args, **kwargs):
        userdata = request.user.userdata
        userdata.push_notifications_when_lots_sell = True
        userdata.save()
        return JsonResponse({"result": "success"})


class LotPushTestNotificationView(APIPostView):
    def post(self, request, *args, **kwargs):
        lot = get_object_or_404(Lot, pk=kwargs["pk"], is_deleted=False)
        if not Watch.objects.filter(lot_number=lot, user=request.user).exists():
            return JsonResponse({"result": "error", "message": "You must watch this lot first."}, status=403)
        if not PushInformation.objects.filter(user=request.user).exists():
            return JsonResponse({"result": "error", "message": "No push subscription found."}, status=400)

        payload = {
            "head": f"{lot.lot_name} test notification",
            "body": f"Lot {lot.lot_number_display} test notification for this watched lot.",
            "url": f"https://{lot.full_lot_link}",
            "tag": f"lot_sell_notification_test_{lot.pk}",
        }
        if lot.thumbnail:
            payload["icon"] = lot.thumbnail.display_url
        send_user_notification(user=request.user, payload=payload, ttl=10000)
        return JsonResponse({"result": "success"})


class CheckUsernameAvailability(APIView):
    """GET /check-username/?username=foo — returns JSON for real-time signup validation.
    No authentication required (used on the public signup form).
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        username = request.GET.get("username", "").strip()
        if not username:
            return JsonResponse({"available": False, "error": "No username provided"})
        taken = User.objects.filter(username__iexact=username).exists()
        return JsonResponse({"available": not taken})


class AuctionTOSValidation(AuctionViewMixin, APIPostView):
    """For real time validation on the auctiontos admin create form
    See views.AuctionTOSAdmin for the corresponding js and view
    """

    def post(self, request, *args, **kwargs):
        pk = request.POST.get("pk", None)
        try:
            pk = int(pk) if pk is not None else None
        except ValueError:
            pk = None
        name = request.POST.get("name", None)
        bidder_number = request.POST.get("bidder_number", None)
        email = request.POST.get("email", None)
        # note: be careful what you dump in result
        # javascript will fill out any id on the form with this info
        result = {
            "id_bidder_number": "",
            "id_name": "",
            "id_email": "",
            "id_address": "",
            "id_is_club_member": "",
            "id_phone_number": "",
            "id_memo": "",
            "name_tooltip": "",
            "bidder_number_tooltip": "",
            "email_tooltip": "",
        }
        base_qs = self.auction.tos_qs
        if pk:
            base_qs = base_qs.exclude(pk=pk)
        if name and not email and not pk:
            old_auctions = auctions_available_for_contact_autofill(
                self.request.user, extra_created_by=self.auction.created_by
            )
            qs = AuctionTOS.objects.filter(auction__in=old_auctions, email__isnull=False).order_by("-createdon")
            old_tos = AuctionTOSFilter.generic(self, qs, name, match_names_only=True).first()
            if old_tos:
                result["id_name"] = old_tos.name
                result["id_email"] = old_tos.email
                result["id_address"] = old_tos.address
                result["id_is_club_member"] = old_tos.is_club_member
                result["id_phone_number"] = old_tos.phone_number
                result["id_memo"] = old_tos.memo
            else:
                logger.info("no user found in older auctions with name %s", name)
        if name:
            existing_tos_in_this_auction = AuctionTOSFilter.generic(self, base_qs, name, match_names_only=True).first()
            if existing_tos_in_this_auction:
                existing_bidder_number = existing_tos_in_this_auction.bidder_number or UNASSIGNED_BIDDER_NUMBER_LABEL
                result["name_tooltip"] = (
                    f"There's already a user in this auction named {existing_tos_in_this_auction.name} "
                    f"(bidder number: {existing_bidder_number})"
                )
            else:
                logger.info("no user found in older auctions with name %s", name)
        if email:
            existing_tos_in_this_auction = base_qs.filter(email=email).first()
            if existing_tos_in_this_auction:
                result["email_tooltip"] = "Email is already in this auction"
            else:
                logger.info("no user found in this auction with email %s", email)
        if bidder_number:
            existing_tos_in_this_auction = base_qs.filter(bidder_number=bidder_number).first()
            if existing_tos_in_this_auction:
                result["bidder_number_tooltip"] = "Bidder number in use"
            elif self.auction.is_club_managed:
                clash = ClubMember.objects.filter(
                    club=self.auction.club, bidder_number=bidder_number, is_deleted=False
                ).first()
                if clash:
                    result["bidder_number_tooltip"] = f"Bidder number in use by {clash.name}"
            else:
                logger.info("no user found in this auction with email %s", email)
        return JsonResponse(result)


class MyWonLotCSV(LoginRequiredMixin, View):
    """CSV file showing won lots"""

    def get(self, request):
        lots = add_price_info(
            Lot.objects.filter(Q(winner=request.user) | Q(auctiontos_winner__email=request.user.email)).exclude(
                is_deleted=True
            )
        )
        current_site = Site.objects.get_current()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="my_won_lots_from_{current_site.domain.replace(".", "_")}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(["Lot number", "Name", "Auction", "Winning price", "Link"])
        for lot in lots:
            writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.auction,
                    f"{lot.currency_symbol}{lot.winning_price}",
                    "https://" + lot.full_lot_link,
                ]
            )
        return response


class MyLotReportView(LoginRequiredMixin, View):
    """CSV file showing sold lots"""

    def get(self, request):
        lots = add_price_info(
            Lot.objects.filter(Q(user=request.user) | Q(auctiontos_seller__email=request.user.email))
            .exclude(is_deleted=True)
            .select_related("bap_award__club_member__club")
        )
        current_site = Site.objects.get_current()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="my_lots_from_{current_site.domain.replace(".", "_")}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Lot number",
                "Name",
                "Auction",
                "Status",
                "Winning price",
                "My cut",
                "BAP points",
                "HAP points",
                "Culture points",
                "Points reason",
                "Points club",
                "Lot URL",
            ]
        )
        for lot in lots:
            status = "Unsold"
            if lot.banned:
                status = "Removed"
            elif lot.deactivated:
                status = "Deactivated"
            elif lot.winner or lot.auctiontos_winner:
                status = "Sold"
            bap_pts = hap_pts = cap_pts = points_reason = points_club = ""
            try:
                award = lot.bap_award
                bap_pts = award.points or ""
                hap_pts = award.hap_points or ""
                cap_pts = award.cap_points or ""
                points_reason = award.notes or ""
                points_club = award.club_member.club.name if award.club_member_id and award.club_member.club_id else ""
            except Exception:
                pass
            writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.auction,
                    status,
                    lot.winning_price,
                    lot.your_cut,
                    bap_pts,
                    hap_pts,
                    cap_pts,
                    points_reason,
                    points_club,
                    "https://" + lot.full_lot_link,
                ]
            )
        return response


class AuctionReportView(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file showing all users who are participating in this auction"""

    def get(self, request):
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        end = timezone.now().strftime("%Y-%m-%d")
        if not query:
            filename = self.auction.slug + "-report-" + end
        else:
            filename = self.auction.slug + "-report-" + query + "-" + end
        response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Join date",
                "Bidder number",
                "Username",
                "Name",
                "Email",
                "Phone",
                "Address",
                "Location",
                "Miles to pickup location",
                "Club",
                "Lots viewed",
                "Lots bid",
                "Lots submitted",
                "Lots sold",
                "Lots won",
                "Invoice",
                "Total bought",
                "Gross sold",
                "Total payout",
                "Total club cut",
                "Invoice total due",
                "Breeder points",
                "Number of lots sold outside auction",
                "Total value of lots sold outside auction",
                "Seconds spent reading rules",
                "Other auctions joined",
                "Users who have banned this user",
                "Account created on",
                "Memo",
                self.auction.alternative_split_label.capitalize(),
                "Bidding allowed",
                "Added auction to their calendar",
            ]
        )
        # Use the auction's tos_qs property to get the has_ever_granted_permission annotation
        users = self.auction.tos_qs.select_related("user__userdata").select_related("pickup_location")
        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)
        # .annotate(distance_traveled=distance_to(\
        # '`auctions_userdata`.`latitude`', '`auctions_userdata`.`longitude`', \
        # lat_field_name='`auctions_pickuplocation`.`latitude`',\
        # lng_field_name="`auctions_pickuplocation`.`longitude`",\
        # approximate_distance_to=1)\
        # )
        for data in users:
            distance = ""
            club = ""
            if data.user and data.has_ever_granted_permission:
                # these things will only be written out if the user wants you to have it
                lotsViewed = PageView.objects.filter(lot_number__auction=self.auction, user=data.user)
                lotsBid = Bid.objects.exclude(is_deleted=True).filter(lot_number__auction=self.auction, user=data.user)
                lot_qs = Lot.objects.exclude(is_deleted=True).filter(
                    user=data.user,
                    auction__isnull=True,
                    date_posted__gte=self.auction.date_start - timedelta(days=2),
                )
                if self.auction.is_online:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=self.auction.date_end + timedelta(days=2))
                else:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=self.auction.date_start + timedelta(days=5))
                numberLotsOutsideAuction = lotsOutsideAuction.count()
                profitOutsideAuction = lotsOutsideAuction.aggregate(total=Sum("winning_price"))["total"]
                if not profitOutsideAuction:
                    profitOutsideAuction = 0
                distance = data.distance_traveled or ""
                club = getattr(data.user.userdata, "club", None)
                username = data.user.username
                previous_auctions = AuctionTOS.objects.filter(user=data.user).exclude(pk=data.pk).count()
                number_of_userbans = data.number_of_userbans
                account_age = data.user.date_joined
                add_to_calendar = "Yes" if data.add_to_calendar else ""
            else:
                add_to_calendar = ""
                previous_auctions = ""
                lotsViewed = ""
                lotsBid = ""
                numberLotsOutsideAuction = ""
                profitOutsideAuction = ""
                username = ""
                number_of_userbans = 0
                account_age = ""
            lotsSumbitted = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=data, auction=self.auction)
            lotsSold = lotsSumbitted.filter(winning_price__isnull=False)
            lotsWon = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=data, auction=self.auction)
            breederPoints = Lot.objects.exclude(is_deleted=True).filter(
                auctiontos_seller=data, auction=self.auction, i_bred_this_fish=True
            )
            address = data.address or ""
            try:
                invoice = Invoice.objects.get(auction=self.auction, auctiontos_user=data)
                invoiceStatus = invoice.get_status_display()
                totalSpent = invoice.total_bought
                totalPaid = invoice.total_sold
                invoiceTotal = invoice.rounded_net
            except Invoice.DoesNotExist:
                invoiceStatus = ""
                totalSpent = 0
                totalPaid = 0
                invoiceTotal = 0
            writer.writerow(
                [
                    data.createdon.strftime("%m-%d-%Y"),
                    data.bidder_number,
                    username,
                    data.name,
                    data.email,
                    data.phone_as_string,
                    address,
                    data.pickup_location,
                    distance,
                    club,
                    len(lotsViewed),
                    len(lotsBid),
                    len(lotsSumbitted),
                    len(lotsSold),
                    len(lotsWon),
                    invoiceStatus,
                    f"{totalSpent:.2f}",
                    f"{data.gross_sold:.2f}",
                    f"{totalPaid:.2f}",
                    f"{data.total_club_cut:.2f}",
                    f"{invoiceTotal:.2f}",
                    len(breederPoints),
                    numberLotsOutsideAuction,
                    profitOutsideAuction,
                    data.time_spent_reading_rules,
                    previous_auctions,
                    number_of_userbans,
                    account_age,
                    data.memo,
                    "Yes" if data.is_club_member else "",
                    "No" if not data.bidding_allowed else "",
                    add_to_calendar,
                ]
            )
        self.auction.create_history(
            applies_to="USERS",
            action="Exported user CSV",
            user=request.user,
        )
        return response


class AddAuctionUsersToClub(LoginRequiredMixin, AuctionViewMixin, View):
    """Add all auction participants (with email) to the auction's associated club.

    Only creates new ClubMember records — never updates existing ones.
    Skips participants without an email address.
    """

    def post(self, request, *args, **kwargs):
        auction = self.auction
        club = auction.club
        if not club:
            messages.error(request, "This auction is not associated with a club.")
            return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))

        # Permission check: must have add_edit permission on the club or be the auction creator
        if (
            not request.user.is_superuser
            and not check_club_permission(request.user, club, "permission_add_edit")
            and not check_club_permission(request.user, club, "permission_manage_auctions")
        ):
            messages.error(request, "You don't have permission to add members to that club.")
            return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))

        tos_qs = AuctionTOS.objects.filter(auction=auction).exclude(email="").filter(email__isnull=False)
        added_count = 0
        skipped_count = 0
        for tos in tos_qs:
            existing = None
            if tos.email:
                existing = ClubMember.objects.filter(club=club, email__iexact=tos.email).first()
            if not existing and tos.user:
                existing = ClubMember.objects.filter(club=club, user=tos.user).first()
            if existing:
                skipped_count += 1
                continue
            ClubMember.objects.create(
                club=club,
                user=tos.user,
                name=tos.name or "",
                email=tos.email,
                phone_number=tos.phone_number or "",
                address=tos.address or "",
                source=str(auction.title)[:200],
                added_by=request.user,
            )
            added_count += 1

        if added_count:
            messages.success(
                request,
                f"Added {added_count} user{'s' if added_count != 1 else ''} to {club.name}."
                + (f"  {skipped_count} already in club." if skipped_count else ""),
            )
            auction.create_history(
                applies_to="USERS",
                action=f"Added {added_count} auction participants to club '{club.name}' ({skipped_count} already members).",
                user=request.user,
            )
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Added {added_count} participant{'s' if added_count != 1 else ''} from auction '{auction}' ({skipped_count} already members)",
                applies_to="MEMBERS",
            )
        else:
            messages.info(
                request,
                f"No new users to add — all {skipped_count} participant{'s' if skipped_count != 1 else ''} with an email are already in {club.name}."
                if skipped_count
                else "No participants with email addresses found.",
            )
        return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))


class AddSingleAuctionTOSToClub(LoginRequiredMixin, View):
    """Add a single AuctionTOS participant to the auction's associated club."""

    def post(self, request, pk):
        tos = get_object_or_404(AuctionTOS, pk=pk)
        auction = tos.auction
        club = auction.club
        if not club:
            return HttpResponse("No club associated with this auction.", status=400)

        if (
            not request.user.is_superuser
            and not check_club_permission(request.user, club, "permission_add_edit")
            and not check_club_permission(request.user, club, "permission_manage_auctions")
        ):
            raise PermissionDenied()

        existing = None
        if tos.email:
            existing = ClubMember.objects.filter(club=club, email__iexact=tos.email, is_deleted=False).first()
        if not existing and tos.user:
            existing = ClubMember.objects.filter(club=club, user=tos.user, is_deleted=False).first()

        if not existing:
            ClubMember.objects.create(
                club=club,
                user=tos.user,
                name=tos.name or "",
                email=tos.email or "",
                phone_number=tos.phone_number or "",
                address=tos.address or "",
                source=str(auction.title)[:200],
                added_by=request.user,
            )
            messages.success(request, f"Added {tos.name} to {club.name}.")
            auction.create_history(
                applies_to="USERS",
                action=f"Added {tos.name} to club '{club.name}'.",
                user=request.user,
            )
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Added {tos.name} from auction '{auction}'",
                applies_to="MEMBERS",
            )

        if request.headers.get("HX-Request"):
            return HttpResponse("", headers={"HX-Refresh": "true"})
        return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))


class ComposeEmailToUsers(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Generate a mailto: link with BCC for filtered users - HTMX endpoint"""

    template_name = "email_users_button.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get query parameter
        query = self.request.GET.get("query", "")
        # Get all users for the auction
        users = AuctionTOS.objects.filter(auction=self.auction).select_related("user")

        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)

        # Collect valid emails (non-null and non-empty)
        emails = list(users.filter(email__isnull=False).exclude(email="").values_list("email", flat=True))
        # Default values
        mailto_url = "#"
        email_count = 0

        if emails:
            # Limit to avoid overly long URLs (conservative cap)
            max_emails = 60
            if len(emails) > max_emails:
                emails = emails[:max_emails]

            bcc = ",".join(emails)
            subject = f"{self.auction.title}"
            body = f"Hello,\n\nThis message is being sent to participants in {self.auction.title}.\n\n"

            if "open" in query or "ready" in query:
                url = reverse("my_auction_invoice", kwargs={"slug": self.auction.slug})
                body += f"You can view your invoice here: https://{Site.objects.get_current().domain}{url}\n\n"
            mailto_url = f"mailto:?bcc={quote_plus(bcc)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
            email_count = len(emails)

        context.update(
            {
                "mailto_url": mailto_url,
                "email_count": email_count,
            }
        )
        return context


class MarketingList(LoginRequiredMixin, View):
    """Get a CSV file showing all users from all auctions you're an admin for"""

    def get(self, request):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = "attachment; filename=all_auction_contacts.csv"
        writer = csv.writer(response)
        found = []
        writer.writerow(["Name", "Email", "Phone"])
        auctions = Auction.objects.filter(
            Q(created_by=request.user) | Q(auctiontos__is_admin=True, auctiontos__user=request.user)
        ).distinct()
        users = AuctionTOS.objects.filter(auction__in=auctions).exclude(email_address_status="BAD")
        for user in users:
            if user.email not in found:
                writer.writerow([user.name, user.email, user.phone_as_string])
                found.append(user.email)
        for auction in auctions:
            auction.create_history(
                applies_to="USERS",
                action="Exported marketing list CSV for all their auctions (including this one)",
                user=request.user,
            )
        return response


class AuctionInvoicesPayPalCSV(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file of all unpaid invoices that owe the club money"""

    def get(self, request, chunk):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type="text/csv")
        due_date = timezone.now().strftime("%m/%d/%Y")
        current_site = Site.objects.get_current()
        response["Content-Disposition"] = f'attachment; filename="{self.auction.slug}-paypal-{chunk}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Recipient Email",
                "Recipient First Name",
                "Recipient Last Name",
                "Invoice Number",
                "Due Date",
                "Reference",
                "Item Name",
                "Description",
                "Item Amount",
                "Shipping Amount",
                "Discount Amount",
                "Currency Code",
                "Note to Customer",
                "Terms and Conditions",
                "Memo to Self",
            ]
        )
        count = 0
        chunkSize = 150  # attention: this is also set in models.auction.paypal_invoice_chunks
        no_email_count = 0
        for invoice in self.auction.paypal_invoices:
            invoice.recalculate()
            # we loop through everything regardless of which chunk
            if not invoice.user_should_be_paid:
                count += 1
                if count <= chunkSize * chunk and count > chunkSize * (chunk - 1):
                    reference = ""
                    itemName = "Auction total"
                    description = ""
                    shippingAmount = 0
                    discountAmount = 0
                    currencyCode = self.auction.created_by.userdata.currency
                    noteToCustomer = f"https://{current_site.domain}/invoices/{invoice.pk}/"
                    termsAndConditions = ""
                    memoToSelf = invoice.auctiontos_user.memo
                    # Bill the rounded balance so the PayPal invoice matches the invoice total the
                    # buyer sees (and skip anyone who only owes a rounded-away residual).
                    if invoice.rounded_net_after_payments < 0:
                        if invoice.auctiontos_user.email:
                            name_parts = (invoice.auctiontos_user.name or "").split()
                            if len(name_parts) >= 2:
                                first_name = name_parts[0][:20]
                                last_name = name_parts[-1][:20]
                            else:
                                first_name = ""
                                last_name = name_parts[0][:20] if name_parts else ""
                            writer.writerow(
                                [
                                    invoice.auctiontos_user.email,
                                    first_name,
                                    last_name,
                                    invoice.pk,
                                    due_date,
                                    reference,
                                    itemName,
                                    description,
                                    abs(invoice.rounded_net_after_payments),
                                    shippingAmount,
                                    discountAmount,
                                    currencyCode,
                                    noteToCustomer,
                                    termsAndConditions,
                                    memoToSelf,
                                ]
                            )
                        else:
                            no_email_count += 1
        self.auction.create_history(
            applies_to="USERS",
            action=f"Exported PayPal invoices CSV.  {no_email_count} users had no email address and were not included in the CSV.",
            user=request.user,
        )
        return response


class AuctionLotsCSV(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file showing all sold lots, who bought/sold them, and the winner's location"""

    def get(self, request):
        # Create the HttpResponse object with the appropriate CSV header.
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        if not query:
            filename = "all-lot-list"
        else:
            filename = "lot-list-" + query
            query = unquote(query)
        response["Content-Disposition"] = f'attachment; filename="{self.auction.slug}-{filename}.csv"'
        writer = csv.writer(response)
        custom_dropdown_enabled = (
            self.auction.use_custom_dropdown_field != "disable"
            and bool(self.auction.custom_dropdown_name)
            and AuctionDropdown.objects.filter(auction=self.auction).count() >= 2
        )
        first_row_fields = [
            "Lot number",
            "Lot",
            "Seller",
            "Seller email",
            "Seller phone",
            "Seller location",
            "Winner",
            "Winner email",
            "Winner phone",
            "Winner location",
            "Breeder points",
            "Donation",
            "Sell price",
            "Club Cut",
            "Seller cut",
        ]
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            first_row_fields.append(self.auction.custom_checkbox_name)
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            first_row_fields.append(self.auction.custom_field_1_name)
        if custom_dropdown_enabled:
            first_row_fields.append(self.auction.custom_dropdown_name)
        writer.writerow(first_row_fields)
        lots = self.auction.lots_qs
        lots = add_price_info(lots)
        if query:
            lots = LotAdminFilter.generic(None, lots, query)
        for lot in lots:
            row = [
                lot.lot_number_display,
                lot.lot_name,
                lot.auctiontos_seller.name,
                lot.auctiontos_seller.email,
                lot.auctiontos_seller.phone_as_string,
                lot.location,
                lot.auctiontos_winner.name if lot.auctiontos_winner else "",
                lot.auctiontos_winner.email if lot.auctiontos_winner else "",
                lot.auctiontos_winner.phone_as_string if lot.auctiontos_winner else "",
                lot.winner_location,
                lot.i_bred_this_fish_display,
                lot.donation,
                f"{lot.winning_price:.2f}" if lot.winning_price else "",
                f"{lot.club_cut:.2f}" if lot.winning_price else "",
                f"{lot.your_cut:.2f}" if lot.winning_price else "",
            ]
            if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
                row.append(lot.custom_checkbox_label)
            if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
                row.append(lot.custom_field_1)
            if custom_dropdown_enabled:
                row.append(lot.custom_dropdown)
            writer.writerow(row)
        self.auction.create_history(
            applies_to="LOTS",
            action=f"Exported lot list CSV for {query or 'all lots'}",
            user=request.user,
        )
        return response


class LeaveFeedbackView(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""

    model = Lot
    template_name = "leave_feedback.html"
    ordering = ["-date_posted"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cutoffDate = timezone.now() - timedelta(days=90)
        context["won_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(winner=self.request.user) | Q(auctiontos_winner__user=self.request.user),
                date_posted__gte=cutoffDate,
            )
            .order_by("-date_posted")
        )
        context["sold_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(user=self.request.user) | Q(auctiontos_seller__user=self.request.user),
                date_posted__gte=cutoffDate,
                winning_price__isnull=False,
            )
            .order_by("-date_posted")
        )
        return context


class FindImageIcon(APIView):
    """Return a handy little icon if the lot name will have an image associated with it"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        name = request.POST["name"]
        result = find_image(name, None, self.auction)
        if result:
            return HttpResponse("image available")
        return HttpResponse("")


class AuctionChats(AuctionViewMixin, LoginRequiredMixin, ListView):
    """Auction admins view to show and delete all chats for an auction"""

    model = LotHistory
    template_name = "chats.html"

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
    #     if not self.auction:
    #         raise Http404
    #     self.is_auction_admin
    #     return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # get related auctiontos if the user has joined the auction
        auctiontos_subquery = AuctionTOS.objects.filter(user=OuterRef("user"), auction=self.auction).values("pk")[:1]
        qs = (
            LotHistory.objects.filter(
                lot__auction=self.auction,
                changed_price=False,
            )
            .annotate(auctiontos_pk=Subquery(auctiontos_subquery, output_field=IntegerField(), null=True))
            .order_by("-timestamp")
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionChatDeleteUndelete(APIView, AuctionViewMixin):
    """HTMX for auction admins only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.history = get_object_or_404(LotHistory, pk=pk, lot__auction__is_deleted=False)
        self.auction = self.history.lot.auction
        if not self.auction:
            raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Toggle the removed field
        self.history.removed = not self.history.removed
        self.history.save()
        if not self.history.removed:
            result = f'<span id="message_{self.history.pk}" class="badge bg-info">Delete</span>'
        else:
            result = f'<span id="message_{self.history.pk}" class="badge bg-danger">Deleted</span>'
            self.auction.create_history(
                applies_to="USERS",
                action="Deleted chat message",
                user=self.request.user,
            )
        return HttpResponse(result)


class AuctionShowHighBidder(APIView, AuctionViewMixin):
    """HTMX for auction admins only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.lot = get_object_or_404(Lot, pk=pk, is_deleted=False, auction__isnull=False)
        self.auction = self.lot.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if not self.lot.max_bid_revealed_by:
            self.lot.max_bid_revealed_by = request.user
            self.lot.save()
            LotHistory.objects.create(
                lot=self.lot,
                user=self.request.user,
                message=f"{self.request.user} has looked at the max bid on this lot",
                changed_price=True,
            )
        return HttpResponse(f"Max bid: ${self.lot.max_bid}")
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
        if next_url:
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
        if not was_promoted and updated_auction.promote_this_auction and updated_auction.club_id:
            club = updated_auction.club
            if club.current_auction_id != updated_auction.pk:
                club.current_auction = updated_auction
                club.save(update_fields=["current_auction"])
                messages.info(self.request, f"This is now the current auction for {club.name}.")
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
                f"This auction allows online bidding -- make sure to <a href='{reverse('auction_help', kwargs={'slug': self.get_object().slug})}'>watch the tutorial in the help</a> to see how this works",
            )
        if (
            self.get_object().buy_now == "allow" or self.get_object().buy_now == "required"
        ) and "buy_now_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                f"Buy now is enabled, but labels are not set to print a buy now price. <a href='{reverse('auction_label_config', kwargs={'slug': self.get_object().slug})}'>You should enable printing buy now on labels here.</a>",
            )
        if (
            self.get_object().reserve_price == "allow" or self.get_object().reserve_price == "required"
        ) and "min_bid_label" not in self.get_object().label_print_fields:
            messages.info(
                self.request,
                f"Minimum bid is enabled, but labels are not set to print a minimum bid. <a href='{reverse('auction_label_config', kwargs={'slug': self.get_object().slug})}'>You should enable printing minimum bids on labels here.</a>",
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
        return AuctionHistory.objects.filter(auction=self.auction).order_by("-timestamp")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["auction"] = self.auction
        return kwargs


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
        return Lot.objects.exclude(is_deleted=True).filter(auction=self.auction).order_by("lot_number")

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


class AuctionHelp(LoginRequiredMixin, AdminEmailMixin, AuctionViewMixin, TemplateView):
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
        return AuctionTOS.objects.filter(auction=self.auction).order_by("name")

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
        # When the filtered table has no results and a query was typed, show a "Create user" button
        # pre-populated intelligently based on what the user typed.
        query = (self.request.GET.get("query") or "").strip()
        filterset = context.get("filter")
        filtered_empty = filterset is not None and query and not filterset.qs.exists()
        if filtered_empty and self.can_add_edit_people:
            context["no_results"] = self._build_no_results_html(query)
        return context

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
        return (
            f'<div class="text-center py-3">'
            f'<p class="text-muted mb-2">No users match <strong>{query}</strong>.</p>'
            f'<button class="btn btn-info btn-sm" '
            f'hx-get="{create_url}" '
            f'hx-target="#modals-here" '
            f'hx-trigger="click" '
            f'_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
            f'<i class="bi bi-person-fill-add"></i> Create user</button>'
            f"</div>"
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
        <button type="button" class="btn-close" data-modal-close-action="none" aria-label="Close"></button>
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
        bidder_number = (request.POST.get("bidder_number") or "").strip()
        update_fields = []
        if not tos.checked_in:
            tos.checked_in = timezone.now()
            update_fields.append("checked_in")
        if not tos.bidding_allowed:
            tos.bidding_allowed = True
            update_fields.append("bidding_allowed")
        if update_fields:
            tos.save(update_fields=update_fields)
        if bidder_number and bidder_number != tos.bidder_number:
            tos.force_set_bidder_number(bidder_number, acting_user=request.user)
        self.auction.create_history(
            applies_to="USERS",
            action=f"Checked in {tos.name}",
            user=request.user,
        )
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
        candidate_ids = list(
            AuctionTOS.objects.filter(
                auction=self.auction,
                checked_in__isnull=False,
                door_prize_called__isnull=True,
            ).values_list("pk", flat=True)
        )
        if not candidate_ids:
            messages.warning(request, "No checked-in users are left for door prizes.")
            return redirect(redirect_url)
        winner = AuctionTOS.objects.get(pk=secrets.choice(candidate_ids))
        winner.door_prize_called = timezone.now()
        winner.save(update_fields=["door_prize_called"])
        self.auction.create_history(
            applies_to="USERS",
            action=f"Picked door prize winner {winner.name}",
            user=request.user,
        )
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
                # Verify user has access to this auction
                if compare_auction.permission_check(self.request.user):
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


class DynamicSetLotWinner(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """A form to set lot winners.  Totally async with no page loads, just POST"""

    template_name = "auctions/dynamic_set_lot_winner.html"
    club_sidebar_can_view = False  # full-screen tool; sidebar would waste space

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # Don't want notifications to show up on the projector
        # context['disable_websocket'] = True
        return context

    def validate_lot(self, lot, action):
        """Returns (Lot or None, error or None)"""
        error = None
        result_lot = None
        if not lot and action != "validate":
            error = "Enter a lot number"
        else:
            # this next line makes it so you cannot search by custom_lot_number in a use_seller_dash_lot_numbering auction
            # if custom lot numbers are ever reenabled, change this
            result_lot_qs = Lot.objects.none()
            if self.auction.use_seller_dash_lot_numbering:
                result_lot_qs = self.auction.lots_qs.filter(custom_lot_number=lot)
            else:
                try:
                    lot = int(lot)
                except ValueError:
                    error = "Lot number must be a number"
                if not error and lot:
                    result_lot_qs = self.auction.lots_qs.filter(lot_number_int=lot)
                if error and not lot and action == "validate":
                    error = ""
            # This can happen if two people are submitting lots at the exact same millisecond.  It seems very unlikely but an easy enough edge case to catch.
            if result_lot_qs.count() > 1:
                error = "Multiple lots with this lot number.  Go to the lot's page and set the winner there."
            else:
                result_lot = result_lot_qs.first()
            if not result_lot and lot and not error:
                error = "No lot found"
        if (
            result_lot
            and result_lot.auctiontos_seller
            and result_lot.auctiontos_seller.invoice
            and result_lot.auctiontos_seller.invoice.status != "DRAFT"
        ):
            if action != "force_save":
                error = "The seller's invoice is not open"
        if result_lot and result_lot.auctiontos_winner and result_lot.winning_price and action != "force_save":
            error = "This lot has already been sold"
        return result_lot, error

    def validate_price(self, price, action):
        """Returns (Decimal or None, error or None)"""
        result_price = None
        error = None
        try:
            result_price = Decimal(str(price)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            if action == "save":
                error = "Enter the winning price"
            if action == "force_save":
                error = "You can skip some errors, but you still need to enter a price"
        if result_price is not None and self.auction.only_whole_dollar_bids:
            if result_price != result_price.to_integral_value():
                error = "This auction only allows whole dollar amounts"
                result_price = None
        return result_price, error

    def validate_winner(self, winner, action):
        """Returns (AuctionTOS or None, error or None)"""
        error = None
        tos = None
        if not winner and (action == "force_save" or action == "save"):
            error = "Enter the winning bidder's number"
        else:
            tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=winner).order_by("-createdon").first()
            if not tos and winner and self.auction.is_club_managed:
                # In club-managed mode, the source of truth for bidder numbers is ClubMember.
                # Look up the member by bidder number; if found, ensure a shadow AuctionTOS exists.
                cm = ClubMember.objects.filter(club=self.auction.club, bidder_number=winner, is_deleted=False).first()
                if cm:
                    tos = _upsert_clubmember_shadow_tos(
                        self.auction,
                        cm,
                        bidding_allowed=cm.bidding_allowed,
                        selling_allowed=cm.selling_allowed,
                    )
            if not tos and winner:
                error = "No bidder found"
            else:
                if tos and tos.invoice and tos.invoice.status != "DRAFT" and action != "force_save":
                    error = "This user's invoice is not open"
                if tos and tos.requires_check_in_before_bidding and action != "force_save":
                    error = "This bidder has not been checked in yet"
        return tos, error

    def end_unsold(self, lot):
        """Mark lot unsold"""
        lot.date_end = timezone.now()
        lot.winner = None
        lot.auctiontos_winner = None
        lot.winning_price = None
        lot.active = False
        lot.save()
        message = f"{self.request.user} has marked lot {lot.lot_number_display} as not sold"
        LotHistory.objects.create(
            lot=lot,
            user=self.request.user,
            message=message,
            changed_price=True,
        )
        lot.send_websocket_message(
            {
                "type": "chat_message",
                "info": "ENDED_NO_WINNER",
                "message": message,
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
            }
        )
        return message

    def set_winner(self, lot, winning_tos, winning_price):
        lot.auctiontos_winner = winning_tos
        lot.winning_price = winning_price
        lot.date_end = timezone.now()
        lot.active = False
        lot.save()
        if (
            lot.auction
            and lot.auction.use_check_in_mode
            and lot.auctiontos_seller
            and not lot.auctiontos_seller.checked_in
        ):
            seller = lot.auctiontos_seller
            seller.checked_in = timezone.now()
            update_fields = ["checked_in"]
            if not seller.bidding_allowed:
                seller.bidding_allowed = True
                update_fields.append("bidding_allowed")
            seller.save(update_fields=update_fields)
            lot.auction.create_history(
                applies_to="USERS",
                action=f"Checked in {seller.name} (lot sold)",
                user=self.request.user,
            )
        try:
            lot.add_winner_message(self.request.user, winning_tos, winning_price)
        except Exception:
            logger.exception("add_winner_message failed for lot %s", lot.pk)
        if lot.auction and lot.auction.club and not lot.bap_points_awarded and not lot.manually_approved:
            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
        return f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.lot_number_display}"

    def post(self, request, *args, **kwargs):
        """All lot validation checks called from here"""
        lot = request.POST.get("lot", None)
        price = request.POST.get("price", None)
        winner = request.POST.get("winner", None)
        action = request.POST.get("action", "validate")

        result = {
            "price": None,
            "winner": None,
            "lot": None,
            "last_sold_lot_number": None,
            "success_message": None,
            "online_high_bidder_message": None,
            "auction_minutes_to_end": None,
        }
        lot, lot_error = self.validate_lot(lot, action)
        if lot and not lot_error and action == "to_online_high_bidder":
            result["success_message"] = lot.sell_to_online_high_bidder
            result["last_sold_lot_number"] = lot.lot_number_display
            try:
                lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
            except Exception:
                logger.exception("add_winner_message failed for lot %s", lot.pk)
            try:
                lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Sold lot {lot.lot_number_display} to online high bidder",
                    user=self.request.user,
                )
            except Exception:
                logger.exception("create_history failed for lot %s", lot.pk)
            return JsonResponse(result)
        price, price_error = self.validate_price(price, action)
        winner, winner_error = self.validate_winner(winner, action)
        if lot and not lot_error and action == "end_unsold":
            result["success_message"] = self.end_unsold(lot)
            result["last_sold_lot_number"] = lot.lot_number_display
            try:
                lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Marked lot {lot.lot_number_display} as ended without being sold",
                    user=self.request.user,
                )
            except Exception:
                logger.exception("create_history failed for lot %s", lot.pk)
            return JsonResponse(result)
        if (
            not price_error
            and lot
            and winner
            and lot.high_bidder
            and lot.auction.online_bidding == "allow"
            and action != "force_save"
        ):
            if price and price <= lot.max_bid and f"{winner}" != f"{lot.high_bidder_for_admins}":
                price_error = "Lower than an online bid"
                winner_error = f"Bidder {lot.high_bidder_for_admins} has bid more than this"
        if not price_error and price and lot and not lot_error and action != "force_save":
            if lot.reserve_price and price < lot.reserve_price:
                price_error = f"This lot's minimum bid is ${lot.reserve_price}"
            if price < self.auction.minimum_bid:
                price_error = f"Minimum bid is ${self.auction.minimum_bid}"
        if not lot_error and not price_error and not winner_error:
            if action != "validate":
                result["last_sold_lot_number"] = lot.lot_number_display
            if action == "force_save" or action == "save":
                result["success_message"] = self.set_winner(lot, winner, price)
                if action == "force_save" and lot.auction and lot.auction.use_check_in_mode and not winner.checked_in:
                    winner.checked_in = timezone.now()
                    update_fields = ["checked_in"]
                    if not winner.bidding_allowed:
                        winner.bidding_allowed = True
                        update_fields.append("bidding_allowed")
                    winner.save(update_fields=update_fields)
                    lot.auction.create_history(
                        applies_to="USERS",
                        action=f"Checked in {winner.name} (ignored errors, lot sold)",
                        user=self.request.user,
                    )
                try:
                    lot.auction.create_history(
                        applies_to="LOTS",
                        action=f"{'Ignored errors and set ' if action == 'force_save' else 'Set'} lot {lot.lot_number_display} as sold",
                        user=self.request.user,
                    )
                except Exception:
                    logger.exception("create_history failed for lot %s", lot.pk)
        # if two people are recording bids, we can validate whether or not a lot was sold
        if (
            lot
            and winner
            and price
            and not price_error
            and not winner_error
            and lot_error == "This lot has already been sold"
            and (action == "force_save" or action == "save")
        ):
            if winner == lot.auctiontos_winner and price == lot.winning_price:
                # Lot has been double checked -- mark it as good
                lot.admin_validated = True
                lot.save()
                result["success_message"] = "This lot has been double checked"
                result["last_sold_lot_number"] = lot.lot_number_display
            else:
                # Mismatch between what's been saved in the db and the current request
                result = {
                    "banner": "error",
                    "last_sold_lot_number": lot.lot_number_display,
                    "success_message": f"Lot {lot.lot_number_display} already sold for {lot.currency_symbol}{lot.winning_price} to {lot.auctiontos_winner.bidder_number}.  If this is not correct, you can undo this sale",
                }
        if lot and (action == "validate" or not result["success_message"]) and lot.high_bidder:
            result["online_high_bidder_message"] = (
                f"Sell to {lot.high_bidder_for_admins} for {lot.currency_symbol}{lot.high_bid}"
            )
            # js code is not in place for this, also remove code from view_lot_simple
        if lot and not lot_error:
            lot = "valid"
        if price and not price_error:
            price = "valid"
        if winner and not winner_error:
            winner = "valid"
        result["lot"] = lot_error or lot
        result["price"] = price_error or price
        result["winner"] = winner_error or winner
        if not lot_error and not price_error and not winner_error:
            result["auction_minutes_to_end"] = self.auction.estimate_end
            result["unsold_lot_count"] = self.auction.total_unsold_lots
        return JsonResponse(result)


class AuctionUnsellLot(LoginRequiredMixin, AuctionViewMixin, View):
    def post(self, request, *args, **kwargs):
        undo_lot = request.POST.get("lot_number", None)
        if undo_lot:
            if self.auction.use_seller_dash_lot_numbering:
                undo_lot = self.auction.lots_qs.filter(custom_lot_number=undo_lot).first()
            else:
                undo_lot = self.auction.lots_qs.filter(lot_number_int=undo_lot).first()
        if undo_lot:
            result = {
                "hide_undo_button": "true",
                "last_sold_lot_number": "",
                "success_message": f"{undo_lot.lot_number_display} {undo_lot.lot_name} now has no winner and can be sold",
            }
            undo_lot.winner = None
            undo_lot.auctiontos_winner = None
            undo_lot.winning_price = None
            if not self.auction.is_online:
                undo_lot.date_end = None
                # this might need changing for online auctions
                # but as it is now, this view is only ever called for in-person auctions
            undo_lot.active = True
            undo_lot.admin_validated = False
            undo_lot.save()
            undo_lot.auction.create_history(
                applies_to="LOTS",
                action=f"Cleared the winner on lot {undo_lot.lot_number_display} to make it unsold",
                user=self.request.user,
            )
        else:
            result = {"message": "No lot found"}
        return JsonResponse(result)

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed


class CSVContactImportMixin:
    """Mixin providing shared CSV parsing utilities for importing contact records.

    Use this with views that need to import contacts (e.g., AuctionTOS or ClubMember)
    from CSV files. Subclass and implement `process_csv_data(csv_reader, filename=None)`
    to define how parsed rows are applied to your model.

    Example usage in a view::

        class MyImportView(LoginRequiredMixin, CSVContactImportMixin, View):
            def post(self, request, *args, **kwargs):
                csv_file = request.FILES.get("csv_file")
                return self.handle_csv_upload(csv_file)

            def process_csv_data(self, csv_reader, filename=None):
                for row in csv_reader:
                    email = self.extract_csv_field(row, self.EMAIL_FIELD_NAMES)
                    ...
    """

    EMAIL_FIELD_NAMES = ["email", "e-mail", "email address", "e-mail address"]
    NAME_FIELD_NAMES = ["name", "full name", "first name", "firstname"]
    ADDRESS_FIELD_NAMES = ["address", "mailing address"]
    PHONE_FIELD_NAMES = ["phone", "phone number", "telephone", "telephone number"]
    MEMO_FIELD_NAMES = ["memo", "note", "notes"]
    FIRST_NAME_FIELD_NAMES = ["first name", "firstname", "first"]
    LAST_NAME_FIELD_NAMES = ["last name", "lastname", "last", "surname"]
    MEMBERSHIP_LAST_PAID_FIELD_NAMES = [
        "membership last paid",
        "membership_last_paid",
        "last paid",
        "paid date",
        "paid",
    ]
    MEMBERSHIP_EXPIRATION_FIELD_NAMES = [
        "membership expiration date",
        "membership_expiration_date",
        "expiration date",
        "expiration",
        "expires",
        "membership expires",
    ]
    DISCORD_ID_FIELD_NAMES = ["discord id", "discord_id", "discord"]
    CONTACT_STATUS_FIELD_NAMES = ["contact status", "contact_status", "contact"]
    DATE_JOINED_FIELD_NAMES = ["date joined", "createdon", "created on", "joined", "join date", "date_joined"]

    # Maps human-readable contact status values (lowercased) to model values
    CONTACT_STATUS_MAP = {
        "contact": "contact",
        "contact normally": "contact",
        "non_essential": "non_essential",
        "non essential": "non_essential",
        "no non-essential emails": "non_essential",
        "no non essential emails": "non_essential",
        "do_not_contact": "do_not_contact",
        "do not contact": "do_not_contact",
        "dnc": "do_not_contact",
    }

    @staticmethod
    def parse_contact_status(value):
        """Map a CSV contact status value to a model value, or return None if not recognized."""
        if not value or not value.strip():
            return None
        return CSVContactImportMixin.CONTACT_STATUS_MAP.get(value.strip().lower())

    @staticmethod
    def parse_flexible_date(value):
        """Parse a date string, supporting incomplete formats: '2025' → Jan 1 2025, '2025-06' → Jun 1 2025."""
        if not value or not value.strip():
            return None
        value = value.strip()
        if re.match(r"^\d{4}$", value):
            return date_type(int(value), 1, 1)
        m = re.match(r"^(\d{4})[-/](\d{1,2})$", value)
        if m:
            return date_type(int(m.group(1)), int(m.group(2)), 1)
        # ISO format: YYYY-MM-DD
        try:
            return date_type.fromisoformat(value)
        except ValueError:
            pass
        # US format: MM/DD/YYYY or MM-DD-YYYY
        m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", value)
        if m:
            try:
                return date_type(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        # YYYY/MM/DD
        m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", value)
        if m:
            try:
                return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    @staticmethod
    def extract_csv_field(row, field_name_list, default_response=""):
        """Pass a row, and a lowercase list of field names.
        Extract the first match found (case insensitive) and return the value from the row.
        Empty string returned if the value is not found in the row."""
        # Skip falsy keys: csv.DictReader stores any surplus cells of a ragged row (more columns than
        # the header) under a None key, and None.lower() would raise. Dropping it keeps a malformed row
        # importable instead of 500-ing the whole upload.
        case_insensitive_row = {k.lower(): v for k, v in row.items() if k}
        for name in field_name_list:
            value = case_insensitive_row.get(name)
            if value is not None:
                return value
        return default_response

    @staticmethod
    def csv_columns_exist(field_names, columns):
        """Returns True if any value in the list `columns` exists in the file headers."""
        # Skip falsy entries: a ragged row's surplus cells surface as a None header key (see
        # extract_csv_field), and None.lower() would raise.
        case_insensitive_row = {k.lower() for k in field_names if k}
        for column in columns:
            if column in case_insensitive_row:
                return True
        return False

    def handle_csv_upload(self, csv_file):
        """If a CSV file has been uploaded, parse it and redirect. Calls process_csv_data()."""
        try:
            csv_file.seek(0)
            csv_reader = csv.DictReader(TextIOWrapper(csv_file.file, encoding="utf-8-sig", newline=""))
            filename = getattr(csv_file, "name", None)
            return self.process_csv_data(csv_reader, filename=filename)
        except (UnicodeDecodeError, ValueError) as e:
            messages.error(
                self.request, f"Unable to read file. Make sure this is a valid UTF-8 CSV file. Error was: {e}"
            )
            return None

    # ------------------------------------------------------------------
    # Preview / confirm framework
    #
    # Every CSV importer parses the upload into a list of JSON-serializable "planned actions", stashes them
    # in Redis under a one-time token, and shows a review page (auctions/csv_import_preview.html) before
    # anything is written. The user can cancel, see skipped rows with reasons, and for contact imports choose
    # per possible-duplicate whether to merge into the existing record (default) or create a new one.
    #
    # A subclass implements:
    #   plan_row(self, row) -> dict|None        classify one CSV row (see action schema below)
    #   apply_action(self, action, decision) -> str   write one planned action, return a result tag
    #   import_done_url(self) / import_cancel_url(self)
    #   import_target_id(self)                  binds the token to its auction/club
    #   record_import_history(self, results, filename)
    # and sets class attrs import_record_kind / import_supports_duplicates / import_preview_columns.
    #
    # Planned-action dict:
    #   {"action": "create"|"update"|"duplicate"|"skip",
    #    "fields": {...normalized values for display + apply...},
    #    "target_pk": <existing record pk or None>,   # update/duplicate
    #    "target_display": "<existing record label>", # update/duplicate
    #    "match_type": "email"|"name"|None,
    #    "reason": "<why skipped / note>",
    #    "raw": {...original row...}}                  # filled in by build_preview if omitted
    # ------------------------------------------------------------------

    PREVIEW_CACHE_PREFIX = "csv_import"
    PREVIEW_TTL_SECONDS = 60 * 60  # 1 hour
    PREVIEW_TEMPLATE = "auctions/csv_import_preview.html"

    # Subclass overrides
    import_record_kind = "record"
    import_supports_duplicates = False
    import_preview_columns = ()  # list of (header, field_key)
    # A field key in each planned action's "fields" dict used to collapse rows that refer to the same
    # record within a single file (e.g. "email" for contact importers, where one row == one person). Leave
    # None for importers where a repeated value is legitimate (e.g. several lots/awards for one seller).
    import_dedupe_field = None

    def import_target_id(self):
        """A stable id binding a preview token to one auction/club so it cannot be replayed elsewhere."""
        return None

    def _preview_cache_key(self, token):
        return f"{self.PREVIEW_CACHE_PREFIX}:{token}"

    def _dedupe_key(self, action):
        """Key used to collapse same-record rows within one file, or None to never collapse this action."""
        field = self.import_dedupe_field
        if not field or action.get("action") == "skip":
            return None
        value = (action.get("fields", {}).get(field) or "").strip().lower()
        return value or None

    @staticmethod
    def _merge_planned_fields(primary, duplicate):
        """Fold a later same-key row's data into the primary planned action: fill only blank string fields
        (so complementary rows combine without loss) while leaving the primary's existing non-empty values,
        booleans and ints untouched (so a conflicting value can't be silently flipped). Optional-column
        ``present`` flags are OR-ed so a column that appears in either row still drives an update."""
        primary_fields = primary.setdefault("fields", {})
        for key, value in duplicate.get("fields", {}).items():
            if value in (None, "", False):
                continue
            if primary_fields.get(key) in (None, ""):
                primary_fields[key] = value
        if "present" in duplicate:
            primary_present = primary.setdefault("present", {})
            for key, value in duplicate["present"].items():
                if value:
                    primary_present[key] = True

    def build_preview(self, csv_reader, filename=None):
        """Run plan_row over every row, stash the planned actions in Redis, and return the token."""
        actions = []
        # Maps a dedupe key (e.g. normalized email) -> index of the first action that "owns" it, so later
        # rows for the same record fold into it instead of creating a duplicate or being silently merged
        # away by the model layer (which would drop the later row's differing fields).
        primary_by_key = {}
        for raw in csv_reader:
            planned = self.plan_row(raw)
            if planned is None:
                continue
            planned.setdefault("raw", {k: v for k, v in raw.items() if k})
            planned["i"] = len(actions)
            key = self._dedupe_key(planned)
            if key is not None and key in primary_by_key:
                primary = actions[primary_by_key[key]]
                self._merge_planned_fields(primary, planned)
                planned["action"] = "skip"
                planned["reason"] = f"Duplicate {self.import_dedupe_field} in file — combined into an earlier row"
                planned["target_pk"] = None
                planned["target_display"] = ""
                planned["match_type"] = None
            elif key is not None:
                primary_by_key[key] = planned["i"]
            actions.append(planned)
        token = uuid.uuid4().hex
        payload = {
            "view": type(self).__name__,
            "kind": self.import_record_kind,
            "target_id": self.import_target_id(),
            "user_id": self.request.user.pk,
            "filename": filename,
            "actions": actions,
        }
        cache.set(self._preview_cache_key(token), payload, self.PREVIEW_TTL_SECONDS)
        return token

    def load_preview(self, token):
        """Return the cached payload for *token*, or None if missing/expired/not owned by this request.

        Binding to the requesting user, the auction/club target, and the originating view class prevents a
        leaked or guessed token from being replayed by another user or against a different auction/club.
        """
        if not token:
            return None
        payload = cache.get(self._preview_cache_key(token))
        if not payload:
            return None
        if payload.get("user_id") != self.request.user.pk:
            return None
        if payload.get("target_id") != self.import_target_id():
            return None
        if payload.get("view") != type(self).__name__:
            return None
        return payload

    def clear_preview(self, token):
        if token:
            cache.delete(self._preview_cache_key(token))

    def _hx_aware_redirect(self, url):
        """Redirect that becomes a full-page navigation even from an HTMx (hx-post) request."""
        if self.request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    def redirect_to_preview(self, token):
        """Navigate the browser (full page, HTMx-aware) to the preview for *token*."""
        return self._hx_aware_redirect(f"{self.request.path}?preview={token}")

    def render_preview(self, token):
        payload = self.load_preview(token)
        if payload is None:
            messages.error(self.request, "This import preview expired or was not found. Please upload the file again.")
            return redirect(self.import_cancel_url())
        actions = payload["actions"]
        columns = self.import_preview_columns
        summary = {"create": 0, "update": 0, "duplicate": 0, "skip": 0}
        apply_rows = []
        duplicate_rows = []
        skipped_rows = []
        for action in actions:
            kind = action["action"]
            summary[kind] = summary.get(kind, 0) + 1
            # Precompute display cells aligned to import_preview_columns (templates can't index a dict by a
            # variable key), so the template just iterates row.cells.
            row = {**action, "cells": [action.get("fields", {}).get(key, "") for _, key in columns]}
            if kind == "duplicate":
                duplicate_rows.append(row)
            elif kind in ("create", "update"):
                apply_rows.append(row)
            elif kind == "skip":
                skipped_rows.append(row)
        context = {
            "token": token,
            "kind": payload["kind"],
            "filename": payload.get("filename"),
            "summary": summary,
            "total": len(actions),
            "columns": columns,
            "supports_duplicates": self.import_supports_duplicates,
            "apply_rows": apply_rows,
            "duplicate_rows": duplicate_rows,
            "skipped_rows": skipped_rows,
            "confirm_url": self.request.path,
            "cancel_url": self.import_cancel_url(),
        }
        return render(self.request, self.PREVIEW_TEMPLATE, context)

    def apply_preview(self, token, post_data):
        payload = self.load_preview(token)
        if payload is None:
            messages.error(self.request, "This import preview expired or was not found. Please upload the file again.")
            return redirect(self.import_cancel_url())
        # Atomically claim this token before doing any writes. cache.add() is a Redis SET-NX, so only the
        # first of two concurrent (or double-submitted) confirms wins the claim; the rest bail out here
        # instead of applying the same batch twice. The JS submit-disable on the review page handles the
        # common accidental double-click; this guards the race / replay it can't.
        claim_key = f"{self._preview_cache_key(token)}:applying"
        if not cache.add(claim_key, 1, self.PREVIEW_TTL_SECONDS):
            messages.info(self.request, "This import is already being processed.")
            return redirect(self.import_done_url())
        results = {}
        try:
            # Apply the whole batch atomically: if one row raises, nothing is half-written.
            with transaction.atomic():
                for action in payload["actions"]:
                    decision = post_data.get(f"decision_{action['i']}", "merge")
                    tag = self.apply_action(action, decision)
                    results[tag] = results.get(tag, 0) + 1
        except Exception:
            # The batch rolled back and wrote nothing; release the claim so the admin can retry the token.
            cache.delete(claim_key)
            raise
        self.clear_preview(token)
        cache.delete(claim_key)
        self.record_import_history(results, payload.get("filename"))
        self.message_import_results(results)
        return redirect(self.import_done_url())

    def record_import_history(self, results, filename=None):
        """Optional hook: write an audit/history entry after a confirmed import. No-op by default."""

    def message_import_results(self, results):
        """Flash a summary message after a confirmed import."""
        labels = [
            ("created", "added"),
            ("updated", "updated"),
            ("merged", "merged into existing"),
            ("skipped", "skipped"),
        ]
        parts = [f"{results[tag]} {self.import_record_kind}s {label}" for tag, label in labels if results.get(tag)]
        if parts:
            messages.success(self.request, ", ".join(parts))

    def handle_import_post(self, request, csv_field_names=("csv_file",)):
        """Shared POST router for the confirm and cancel phases.

        Returns a response for a confirm/cancel submission, or None if this POST is not part of the
        preview flow (so the caller can handle a file upload or its own form, e.g. a formset)."""
        if request.POST.get("_confirm"):
            return self.apply_preview(request.POST["_confirm"], request.POST)
        if request.POST.get("_cancel"):
            self.clear_preview(request.POST.get("_cancel"))
            return redirect(self.import_cancel_url())
        return None


class BulkAddUsers(LoginRequiredMixin, CSVContactImportMixin, AuctionViewMixin, TemplateView, ContextMixin):
    """Add/edit lots of auctiontos"""

    template_name = "auctions/bulk_add_users.html"
    max_users_that_can_be_added_at_once = 200
    extra_rows = 5
    AuctionTOSFormSet = None
    allow_non_admins = True

    # CSV import preview framework (see CSVContactImportMixin)
    import_record_kind = "user"
    import_supports_duplicates = True
    import_dedupe_field = "email"  # two rows with the same email are the same person; combine them
    import_preview_columns = (
        ("Bidder #", "bidder_number"),
        ("Name", "name"),
        ("Email", "email"),
        ("Phone", "phone"),
    )
    BIDDER_NUMBER_FIELDS = ["bidder number", "bidder", "membernumber", "tempguestnumber"]
    NAME_FIELDS = ["name", "full name", "first name", "firstname"]
    EMAIL_FIELDS = ["email", "e-mail", "email address", "e-mail address"]
    ADDRESS_FIELDS = ["address", "mailing address"]
    PHONE_FIELDS = ["phone", "phone number", "telephone", "telephone number"]
    BIDDING_FIELDS = ["allow bidding", "bidding", "bidding allowed", "allowedtobid"]
    MEMO_FIELDS = ["memo", "note", "notes"]
    ADMIN_FIELDS = ["admin", "staff", "is_admin", "is_staff"]

    def _block_if_club_managed(self):
        if self.auction and self.auction.is_club_managed:
            messages.info(
                self.request,
                "This auction manages users through its club. Add or import members from the club admin page.",
            )
            return redirect(reverse("club_admin", kwargs={"slug": self.auction.club.slug}))
        return None

    def get(self, *args, **kwargs):
        _ = self.can_add_edit_people
        redirected = self._block_if_club_managed()
        if redirected is not None:
            return redirected
        preview_token = self.request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        # first, try to read in a CSV file stored in session
        initial_formset_data = self.request.session.get("initial_formset_data", [])
        if initial_formset_data:
            self.extra_rows = len(initial_formset_data) + 1
            del self.request.session["initial_formset_data"]
        else:
            # next, check GET to see if they're asking for an import from a past auction
            import_from_auction = self.request.GET.get("import")
            if import_from_auction:
                other_auction = Auction.objects.exclude(is_deleted=True).filter(slug=import_from_auction).first()
                if not other_auction.permission_check(self.request.user):
                    messages.error(
                        self.request,
                        f"You don't have permission to add users from {other_auction}",
                    )
                else:
                    auctiontos = AuctionTOS.objects.filter(auction=other_auction)
                    total_skipped = 0
                    total_tos = 0
                    for tos in auctiontos:
                        # if not self.tos_is_in_auction(self.auction, tos.name, tos.email):
                        if not self.auction.find_user(tos.name, tos.email):
                            initial_formset_data.append(
                                {
                                    "bidder_number": tos.bidder_number,
                                    "name": tos.name,
                                    "phone": tos.phone_number,
                                    "email": tos.email,
                                    "address": tos.address,
                                    "is_club_member": tos.is_club_member,
                                }
                            )
                            total_tos += 1
                        else:
                            total_skipped += 1
                    if total_tos >= self.max_users_that_can_be_added_at_once:
                        messages.error(
                            self.request,
                            f"You can only add {self.max_users_that_can_be_added_at_once} users from another auction at once; run this again to add additional users.",
                        )
                    if total_skipped:
                        messages.info(
                            self.request,
                            f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below",
                        )
                    if total_tos:
                        self.extra_rows = total_tos + 1
        self.instantiate_formset()
        self.tos_formset = self.AuctionTOSFormSet(
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
            initial=initial_formset_data,
        )
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def import_target_id(self):
        return f"auction:{self.auction.pk}"

    def import_done_url(self):
        return reverse("auction_tos_list", kwargs={"slug": self.auction.slug})

    def import_cancel_url(self):
        return reverse("bulk_add_users", kwargs={"slug": self.auction.slug})

    @staticmethod
    def _tos_label(tos):
        label = tos.name or "(no name)"
        if tos.bidder_number:
            return f"{label} (bidder #{tos.bidder_number})"
        if tos.email:
            return f"{label} ({tos.email})"
        return label

    def _parse_user_row(self, row):
        """Extract + normalize one CSV row into the fields dict, plus which optional columns the file has."""
        club_member_fields = ["member", "club member", self.auction.alternative_split_label.lower()]
        is_club_member = self.extract_csv_field(row, club_member_fields).lower() in [
            "yes",
            "true",
            "member",
            "club member",
            self.auction.alternative_split_label.lower(),
        ]
        bidding_allowed = self.extract_csv_field(row, self.BIDDING_FIELDS, "yes").lower() in ["yes", "true"]
        is_admin = self.extract_csv_field(row, self.ADMIN_FIELDS).lower() in ["yes", "true", "1"]
        fields = {
            "bidder_number": self.extract_csv_field(row, self.BIDDER_NUMBER_FIELDS)[:20],
            "name": self.extract_csv_field(row, self.NAME_FIELDS)[:181],
            "email": normalize_email(self.extract_csv_field(row, self.EMAIL_FIELDS))[:254],
            "phone": self.extract_csv_field(row, self.PHONE_FIELDS)[:20],
            "address": self.extract_csv_field(row, self.ADDRESS_FIELDS)[:500],
            "memo": (self.extract_csv_field(row, self.MEMO_FIELDS) or "")[:500],
            "is_club_member": is_club_member,
            "bidding_allowed": bidding_allowed,
            "is_admin": is_admin,
        }
        cols = list(row.keys())
        present = {
            "is_club_member": self.csv_columns_exist(cols, club_member_fields),
            "bidding_allowed": self.csv_columns_exist(cols, self.BIDDING_FIELDS),
            "memo": self.csv_columns_exist(cols, self.MEMO_FIELDS),
            "is_admin": self.csv_columns_exist(cols, self.ADMIN_FIELDS),
        }
        return fields, present

    def plan_row(self, row):
        fields, present = self._parse_user_row(row)
        name, email = fields["name"], fields["email"]
        base = {"fields": fields, "present": present, "target_pk": None, "target_display": "", "match_type": None}
        if not name and not email:
            return {**base, "action": "skip", "reason": "Row has no name or email"}
        if email:
            existing = self.auction.find_user(email=email)
            if existing:
                return {
                    **base,
                    "action": "update",
                    "target_pk": existing.pk,
                    "target_display": self._tos_label(existing),
                    "match_type": "email",
                    "reason": "Matched an existing user by email",
                }
        if name:
            existing = self.auction.find_user(name=name)
            if existing:
                return {
                    **base,
                    "action": "duplicate",
                    "target_pk": existing.pk,
                    "target_display": self._tos_label(existing),
                    "match_type": "name",
                    "reason": "Same or similar name as an existing user",
                }
        return {**base, "action": "create", "reason": ""}

    def _create_tos(self, fields):
        bidder_number = fields.get("bidder_number", "")
        if bidder_number and AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number).exists():
            bidder_number = ""
        return AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.auction.location_qs.first(),
            manually_added=True,
            bidder_number=bidder_number,
            name=fields.get("name", ""),
            phone_number=fields.get("phone", ""),
            email=fields.get("email", ""),
            address=fields.get("address", ""),
            is_club_member=fields.get("is_club_member", False),
            bidding_allowed=fields.get("bidding_allowed", True),
            memo=fields.get("memo", ""),
            is_admin=fields.get("is_admin", False),
        )

    def _update_tos(self, tos, fields, present):
        """Apply CSV fields onto an existing record. Optional booleans are only overwritten when their
        column was present in the file; the CSV bidder number wins (when non-conflicting) so the number
        physically assigned at check-in is the one the scanner resolves to. Returns True if anything changed."""
        changed = False
        name = fields.get("name", "")
        if name and tos.name != name:
            tos.name = name
            changed = True
        phone = fields.get("phone", "")
        if phone and tos.phone_number != phone:
            tos.phone_number = phone
            changed = True
        address = fields.get("address", "")
        if address and tos.address != address:
            tos.address = address
            changed = True
        email = fields.get("email", "")
        if email and not tos.email:
            tos.email = email
            changed = True
        if present.get("is_club_member") and tos.is_club_member != fields.get("is_club_member"):
            tos.is_club_member = fields.get("is_club_member")
            changed = True
        if present.get("bidding_allowed") and tos.bidding_allowed != fields.get("bidding_allowed"):
            tos.bidding_allowed = fields.get("bidding_allowed")
            changed = True
        if present.get("memo"):
            memo = fields.get("memo", "")
            if tos.memo != memo:
                tos.memo = memo
                changed = True
        if present.get("is_admin") and tos.is_admin != fields.get("is_admin"):
            tos.is_admin = fields.get("is_admin")
            changed = True
        bidder_number = fields.get("bidder_number", "")
        if bidder_number and tos.bidder_number != bidder_number:
            conflict = (
                AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number).exclude(pk=tos.pk).exists()
            )
            if not conflict:
                tos.bidder_number = bidder_number
                changed = True
        if changed:
            tos.save()
        return changed

    def apply_action(self, action, decision):
        kind = action["action"]
        if kind == "skip":
            return "skipped"
        fields = action.get("fields", {})
        present = action.get("present", {})
        target_pk = action.get("target_pk")
        if kind == "create" or (kind == "duplicate" and decision == "create"):
            tos = self._create_tos(fields)
            if kind == "duplicate" and target_pk:
                # keep the admin-review link to the record it resembles
                AuctionTOS.objects.filter(pk=tos.pk).update(possible_duplicate=target_pk)
                AuctionTOS.objects.filter(pk=target_pk, possible_duplicate__isnull=True).update(
                    possible_duplicate=tos.pk
                )
            return "created"
        # update (email match) or merge (name-match duplicate the admin chose to merge)
        tos = AuctionTOS.objects.filter(pk=target_pk, auction=self.auction).first() if target_pk else None
        if not tos:
            self._create_tos(fields)
            return "created"
        changed = self._update_tos(tos, fields, present)
        if not changed:
            return "unchanged"
        return "updated" if kind == "update" else "merged"

    def record_import_history(self, results, filename=None):
        created = results.get("created", 0)
        updated = results.get("updated", 0) + results.get("merged", 0)
        if not created and not updated:
            return
        parts = []
        if created:
            parts.append(f"{created} users added")
        if updated:
            parts.append(f"{updated} users updated")
        msg = ", ".join(parts)
        if filename:
            msg += f" from {filename}"
        self.auction.create_history(applies_to="USERS", action=msg, user=self.request.user)

    def process_csv_data(self, csv_reader, filename=None, *args, **kwargs):
        """Parse the upload into planned actions and show the review page; nothing is written yet."""
        fieldnames = csv_reader.fieldnames or []
        recognized = self.csv_columns_exist(
            fieldnames, self.EMAIL_FIELDS + self.NAME_FIELDS + self.PHONE_FIELDS + self.ADDRESS_FIELDS
        )
        if not recognized:
            messages.error(
                self.request,
                "Unable to read information from this CSV file. Make sure it contains an email and a name column.",
            )
            return self._hx_aware_redirect(self.import_cancel_url())
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)

    def post(self, request, *args, **kwargs):
        _ = self.can_add_edit_people
        redirected = self._block_if_club_managed()
        if redirected is not None:
            return redirected
        # A preview confirm/cancel submission takes priority over file uploads and the manual formset.
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        # Check for CSV file with multiple possible field names
        csv_file = None
        for field_name in ["csv_file", "csv_file_quick"]:
            csv_file = request.FILES.get(field_name)
            if csv_file:
                break

        if csv_file:
            return self.handle_csv_upload(csv_file)
        self.instantiate_formset()
        tos_formset = self.AuctionTOSFormSet(
            self.request.POST,
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
        )
        if tos_formset.is_valid():
            auctiontos = tos_formset.save(commit=False)
            for tos in auctiontos:
                tos.auction = self.auction
                tos.manually_added = True
                tos.save()
            messages.success(self.request, f"Added {len(auctiontos)} users")
            self.auction.create_history(
                applies_to="USERS", action=f"Bulk added {len(auctiontos)} users", user=self.request.user
            )
            return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        self.tos_formset = tos_formset
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["formset"] = self.tos_formset
        context["helper"] = TOSFormSetHelper()
        context["auction"] = self.auction
        context["other_auctions"] = (
            Auction.objects.exclude(is_deleted=True)
            .filter(Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True))
            .exclude(pk=self.auction.pk)
            .distinct()
            .order_by("-date_posted")[:10]
        )
        return context

    def tos_is_in_auction(self, auction, name, email):
        """Return the tos if the name or email are already present in the auction, otherwise None"""
        logger.warning("tos_is_in_auction is deprecated, use auction.find_user() instead")
        qs = AuctionTOS.objects.filter(auction=auction)
        if email:
            qs = qs.filter(email=email)
        elif name:
            qs = qs.filter(Q(name=name, email=None) | Q(name=name, email=""))
        else:
            return None
        return qs.first()

    def dispatch(self, request, *args, **kwargs):
        self.queryset = AuctionTOS.objects.none()  # we don't want to allow editing
        return super().dispatch(request, *args, **kwargs)

    def instantiate_formset(self, *args, **kwargs):
        if not self.AuctionTOSFormSet:
            self.AuctionTOSFormSet = modelformset_factory(
                AuctionTOS,
                extra=self.extra_rows,
                fields=(
                    "bidder_number",
                    "name",
                    "email",
                    "phone_number",
                    "address",
                    "pickup_location",
                    "is_club_member",
                ),
                form=QuickAddTOS,
            )


class ImportFromGoogleDrive(LoginRequiredMixin, AuctionViewMixin, TemplateView, ContextMixin):
    """Import users from a Google Drive spreadsheet"""

    template_name = "auctions/import_from_google_drive.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def post(self, request, *args, **kwargs):
        # Check if this is a sync request (no google_drive_link in POST)
        google_drive_link = request.POST.get("google_drive_link", "").strip()

        # If google_drive_link is provided, update it and sync
        if google_drive_link:
            self.auction.google_drive_link = google_drive_link
            self.auction.save()

        # Perform the sync (whether it's a new link or existing link)
        return self.sync_google_drive()

    def sync_google_drive(self):
        """Read data from Google Drive and import users"""
        if not self.auction.google_drive_link:
            messages.error(self.request, "No Google Drive link configured")
            url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
            return redirect(url)

        try:
            # Convert Google Sheets sharing link to export CSV URL
            # Example: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit#gid=0
            # Convert to: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/export?format=csv&gid=0
            link = self.auction.google_drive_link

            # Extract the spreadsheet ID from the URL
            if "/spreadsheets/d/" in link:
                spreadsheet_id = link.split("/spreadsheets/d/")[1].split("/")[0]
                # Extract gid if present
                gid = "0"
                if "gid=" in link:
                    gid = link.split("gid=")[1].split("&")[0].split("#")[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
            else:
                return self._error_redirect("Invalid Google Drive link. Please use a link to a Google Sheets document.")

            # Fetch the CSV data with timeout to prevent hanging
            response = requests.get(csv_url, timeout=30)
            response.raise_for_status()

            # Create a CSV reader from the response text (handles encoding automatically)
            csv_reader = csv.DictReader(response.text.splitlines())

            # Reuse BulkAddUsers' row planning, but show the same review page before anything is written.
            # Any exceptions from build_preview are caught by the outer try/except blocks.
            bulk_add_view = BulkAddUsers()
            bulk_add_view.request = self.request
            bulk_add_view.auction = self.auction
            token = bulk_add_view.build_preview(csv_reader, filename="Google Drive sync")

            # Record that we pulled the sheet; the actual user changes happen when the admin confirms.
            self.auction.last_sync_time = timezone.now()
            self.auction.save()
            self.auction.create_history("USERS", action="Pulled Google Drive sheet for import review")
            # Send the admin to the BulkAddUsers preview, which renders and (on confirm) applies the token.
            preview_url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug}) + f"?preview={token}"
            return redirect(preview_url)

        except requests.RequestException as e:
            if "401" in str(e):
                return self._error_redirect(
                    "Unable to fetch data from Google Drive. Make sure the link is shared with 'anyone with the link can view'"
                )
            elif "404" in str(e):
                return self._error_redirect("Link not found or invalid")
            else:
                return self._error_redirect(f"Unable to fetch data from Google Drive. Error was {e}")

        except Exception as e:
            return self._error_redirect(f"An error occurred while importing from Google Drive: {e}")

    def _error_redirect(self, error_message):
        """Helper method to display error and redirect to bulk add users page"""
        messages.error(self.request, error_message)
        url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
        return redirect(url)


class BulkAddLots(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Add/edit lots of lots for a given auctiontos pk"""

    template_name = "auctions/bulk_add_lots.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        helper = LotFormSetHelper()
        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = helper
        return self.render_to_response(context)

    def post(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            self.request.POST,
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        if lot_formset.is_valid():
            lots = lot_formset.save(commit=False)
            new_lot_count = 0
            for lot in lots:
                lot.auctiontos_seller = self.tos
                lot.auction = self.auction
                if self.tos.user:
                    lot.user = self.tos.user
                # if not lot.description:
                #    lot.description = ""
                if not lot.pk:
                    new_lot_count += 1
                    lot.added_by = self.request.user
                    if not self.is_admin:
                        if self.tos.user:
                            lot.user = self.tos.user
                lot.save()
            if lots:
                updated_lot_count = len(lots) - new_lot_count
                self.auction.create_history(
                    applies_to="LOTS",
                    action=f"Bulk added {new_lot_count}{f' and updated {updated_lot_count}' if updated_lot_count else ''} lots for {self.tos.name}",
                    user=self.request.user,
                )
                messages.success(self.request, f"Updated lots for {self.tos.name}")
                invoice = Invoice.objects.filter(auctiontos_user=self.tos, auction=self.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=self.tos, auction=self.auction)
                invoice.recalculate()
            # when saving labels, it doesn't take you off from the page you're on
            # So we need to go somewhere, and then say "download labels"
            if "print" in str(self.request.GET.get("type", "")):
                print_url = f"printredirect={reverse('print_labels_by_bidder_number', kwargs={'slug': self.auction.slug, 'bidder_number': self.tos.bidder_number})}"
            else:
                print_url = ""
            if self.is_admin:
                redirect_url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
                if print_url:
                    redirect_url += "?" + print_url
            else:
                redirect_url = reverse("selling")
                if print_url:
                    redirect_url += "?" + print_url
            return redirect(redirect_url)

        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = LotFormSetHelper()
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tos"] = self.tos
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin
        return context

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_admin = False
        if not self.auction:
            raise Http404
        bidder_number = kwargs.pop("bidder_number", None)
        self.tos = None
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
        if self.is_auction_admin:
            self.is_admin = True
        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )
        if not self.tos:
            messages.error(request, "You can't add lots until you join this auction")
            return redirect(
                f"{reverse('auction_main', kwargs={'slug': self.auction.slug})}?next={reverse('bulk_add_lots_auto_for_myself', kwargs={'slug': self.auction.slug})}"
            )
        else:
            if not self.tos.selling_allowed and not self.is_admin:
                messages.error(request, "You don't have permission to add lots to this auction")
                return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        if not self.is_admin and not self.auction.can_submit_lots:
            messages.error(request, f"Lot submission has ended for {self.auction}")
            return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        if not self.is_admin and not self.auction.allow_bulk_adding_lots:
            messages.error(
                request,
                "Bulk adding lots has been disabled in this auction, add your lots one at a time using this form",
            )
            return redirect(self.auction.add_lot_link)
        self.queryset = self.tos.unbanned_lot_qs
        if self.auction.max_lots_per_user:
            # default rows should be the max that are allowed in the auction
            if self.queryset.count() > self.auction.max_lots_per_user:
                extra = self.queryset.count()
            else:
                extra = self.auction.max_lots_per_user - self.queryset.count()
            # but of course sometimes admisn will break the rules for their users:
            if extra < 0:
                extra = 0
        else:
            extra = 5  # default rows to show if max_lots_per_user is not set for this auction
        self.LotFormSet = modelformset_factory(
            Lot,
            extra=extra,
            fields=(
                # "custom_lot_number",
                "lot_name",
                "summernote_description",
                "species_category",
                "i_bred_this_fish",
                "quantity",
                "donation",
                "reserve_price",
                "buy_now_price",
                "custom_checkbox",
                "custom_field_1",
                "custom_dropdown",
            ),
            form=QuickAddLot,
        )
        return super().dispatch(request, *args, **kwargs)


class BulkAddLotsAuto(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Add/edit lots with auto-save functionality - lots are saved as user types"""

    template_name = "auctions/bulk_add_lots_auto.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tos"] = self.tos
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin

        # Get existing lots for this user/auction
        context["existing_lots"] = self.queryset.order_by("-date_posted")

        # Get custom field configurations
        context["use_custom_checkbox"] = self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name
        context["custom_checkbox_name"] = self.auction.custom_checkbox_name if context["use_custom_checkbox"] else ""

        context["use_custom_field_1"] = self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name
        context["custom_field_1_name"] = self.auction.custom_field_1_name if context["use_custom_field_1"] else ""
        context["custom_field_1_required"] = self.auction.custom_field_1 == "required"
        context["custom_dropdown_name"] = self.auction.custom_dropdown_name
        context["custom_dropdown_options"] = list(
            AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon").values_list("value", flat=True)
        )
        context["use_custom_dropdown"] = (
            self.auction.use_custom_dropdown_field != "disable"
            and self.auction.custom_dropdown_name
            and len(context["custom_dropdown_options"]) >= 2
        )
        context["custom_dropdown_required"] = self.auction.use_custom_dropdown_field == "required"

        context["use_i_bred_this_fish"] = self.auction.use_i_bred_this_fish_field
        context["use_quantity"] = self.auction.use_quantity_field
        context["use_donation"] = self.auction.use_donation_field

        context["reserve_price_mode"] = self.auction.reserve_price
        context["buy_now_mode"] = self.auction.buy_now
        context["minimum_bid"] = self.auction.minimum_bid

        context["auto_add_images"] = self.auction.auto_add_images

        # Lot limit settings
        context["max_lots_per_user"] = self.auction.max_lots_per_user
        context["allow_additional_lots_as_donation"] = self.auction.allow_additional_lots_as_donation
        context["current_lot_count"] = self.queryset.count()

        # For determining number of initial blank rows
        max_lots = self.auction.max_lots_per_user
        current_count = self.queryset.count()
        if max_lots:
            initial_rows = min(5, max_lots - current_count) if current_count < max_lots else 0
        else:
            initial_rows = 5
        context["initial_rows"] = max(initial_rows, 1)  # At least 1 row

        return context

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.pop("slug", ""))
        bidder_number = kwargs.pop("bidder_number", None)
        self.tos = None

        # Security: Only admins can access the bidder_number URL
        if bidder_number:
            # Check admin status first
            if not self.is_auction_admin:
                messages.error(request, "Only auction admins can add lots for other users")
                return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
            if not self.tos:
                messages.error(request, "User not found in this auction")
                return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))

        self.is_admin = self.is_auction_admin

        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )
        if not self.tos:
            messages.error(request, "You can't add lots until you join this auction")
            return redirect(
                f"{reverse('auction_main', kwargs={'slug': self.auction.slug})}?next={reverse('bulk_add_lots_auto_for_myself', kwargs={'slug': self.auction.slug})}"
            )
        else:
            if not self.tos.selling_allowed and not self.is_admin:
                messages.error(request, "You don't have permission to add lots to this auction")
                return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        if not self.is_admin and not self.auction.can_submit_lots:
            messages.error(request, f"Lot submission has ended for {self.auction}")
            return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        if not self.is_admin and not self.auction.allow_bulk_adding_lots:
            messages.error(
                request,
                "Bulk adding lots has been disabled in this auction, add your lots one at a time using this form",
            )
            return redirect(self.auction.add_lot_link)
        self.queryset = self.tos.unbanned_lot_qs
        return super().dispatch(request, *args, **kwargs)


class SaveLotAjax(APIView, AuctionViewMixin):
    """AJAX endpoint to save a single lot"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    allow_non_admins = True

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            lot_id = data.get("lot_id")
            bidder_number = data.get("bidder_number")

            # Determine which TOS we're adding lots for
            self.tos = None
            self.is_admin = self.is_auction_admin

            if bidder_number:
                # Someone is trying to add lots for a specific user
                # Only admins can do this
                if not self.is_admin:
                    return JsonResponse({"success": False, "error": "Only auction admins can add lots for other users"})
                self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
                if not self.tos:
                    return JsonResponse({"success": False, "error": "User not found in this auction"})
            else:
                # Adding lots for yourself
                self.tos = (
                    AuctionTOS.objects.filter(auction=self.auction)
                    .filter(Q(email=request.user.email) | Q(user=request.user))
                    .first()
                )
                if not self.tos:
                    return JsonResponse({"success": False, "error": "You must join this auction before adding lots"})

            # Check if user has permission to add lots
            if not self.tos.selling_allowed and not self.is_admin:
                return JsonResponse(
                    {"success": False, "error": "You don't have permission to add lots to this auction"}
                )

            # Create or get existing lot
            if lot_id:
                lot = Lot.objects.filter(lot_number=lot_id, auction=self.auction, auctiontos_seller=self.tos).first()
                if not lot:
                    return JsonResponse({"success": False, "error": "Lot not found"})
                is_new = False

                # Check if lot can be edited
                if not lot.can_be_edited and not self.is_admin:
                    return JsonResponse(
                        {"success": False, "error": lot.cannot_be_edited_reason or "This lot cannot be edited"}
                    )
            else:
                lot = Lot(
                    auction=self.auction,
                    auctiontos_seller=self.tos,
                    user=self.tos.user or None,
                    added_by=request.user,
                )
                is_new = True

            admin_bypassed_lot_limit = False  # Track if admin bypassed lot limit
            admin_bypassed_selling_allowed = False  # Track if admin bypassed selling_allowed

            # Check if admin is bypassing selling_allowed restriction
            if self.is_admin and not self.tos.selling_allowed:
                admin_bypassed_selling_allowed = True

            # Check lot limits
            if is_new and self.auction.max_lots_per_user:
                current_count = self.tos.unbanned_lot_qs.count()
                # Admins can bypass limits for both their own lots and other users' lots
                bypass_limit = self.is_admin
                limit_exceeded = current_count >= self.auction.max_lots_per_user

                if limit_exceeded and not bypass_limit:
                    # Check if donation lots are allowed beyond the limit
                    donation = data.get("donation", False)
                    if not donation or not self.auction.allow_additional_lots_as_donation:
                        return JsonResponse(
                            {
                                "success": False,
                                "errors": {
                                    "general": f"You have reached the maximum of {self.auction.max_lots_per_user} lots for this auction"
                                },
                            }
                        )

                # Track if admin bypassed the limit for visual feedback
                admin_bypassed_lot_limit = bypass_limit and limit_exceeded

            # Validate and save fields
            errors = {}

            lot_name = data.get("lot_name", "").strip()
            if not lot_name:
                errors["lot_name"] = "Lot name is required"
            elif len(lot_name) > 40:
                errors["lot_name"] = "Lot name must be 40 characters or less"
            else:
                lot.lot_name = lot_name

            # Species category (auto-set to Uncategorized)
            if not lot.species_category_id:
                lot.species_category = Category.objects.filter(name="Uncategorized").first()

            # Custom checkbox
            if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
                lot.custom_checkbox = data.get("custom_checkbox", False)

            # Custom field 1
            if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
                custom_field_1 = data.get("custom_field_1", "").strip()
                if self.auction.custom_field_1 == "required" and not custom_field_1:
                    errors["custom_field_1"] = f"{self.auction.custom_field_1_name} is required"
                elif len(custom_field_1) > 60:
                    errors["custom_field_1"] = f"{self.auction.custom_field_1_name} must be 60 characters or less"
                else:
                    lot.custom_field_1 = custom_field_1

            custom_dropdown_options = list(
                AuctionDropdown.objects.filter(auction=self.auction).values_list("value", flat=True)
            )
            if (
                self.auction.use_custom_dropdown_field != "disable"
                and self.auction.custom_dropdown_name
                and len(custom_dropdown_options) >= 2
            ):
                custom_dropdown = data.get("custom_dropdown", "").strip()
                if len(custom_dropdown) > CUSTOM_DROPDOWN_MAX_LENGTH:
                    errors["custom_dropdown"] = (
                        f"Custom dropdown value must be {CUSTOM_DROPDOWN_MAX_LENGTH} characters or less"
                    )
                elif custom_dropdown and custom_dropdown not in custom_dropdown_options:
                    errors["custom_dropdown"] = "Select a valid custom dropdown option"
                elif self.auction.use_custom_dropdown_field == "required" and not custom_dropdown:
                    errors["custom_dropdown"] = f"{self.auction.custom_dropdown_name} is required"
                else:
                    lot.custom_dropdown = custom_dropdown
            else:
                lot.custom_dropdown = ""

            # I bred this fish
            if self.auction.use_i_bred_this_fish_field:
                lot.i_bred_this_fish = data.get("i_bred_this_fish", False)

            # Quantity
            if self.auction.use_quantity_field:
                quantity = data.get("quantity")
                if quantity is None or quantity == "":
                    quantity = 1
                try:
                    quantity = int(quantity)
                    if quantity < 1:
                        errors["quantity"] = "Quantity must be at least 1"
                    else:
                        lot.quantity = quantity
                except (ValueError, TypeError):
                    errors["quantity"] = "Quantity must be a number"
            else:
                lot.quantity = 1

            # Donation
            if self.auction.use_donation_field:
                lot.donation = data.get("donation", False)

            # Reserve price
            if self.auction.reserve_price != "disable":
                reserve_price = data.get("reserve_price")
                if reserve_price is None or reserve_price == "":
                    reserve_price = self.auction.minimum_bid
                try:
                    reserve_price = Decimal(str(reserve_price))
                    if reserve_price < Decimal("0.01"):
                        errors["reserve_price"] = "Minimum bid must be at least $0.01"
                    elif reserve_price > 2000:
                        errors["reserve_price"] = "Minimum bid must be $2000 or less"
                    elif self.auction.only_whole_dollar_bids and reserve_price != reserve_price.to_integral_value():
                        errors["reserve_price"] = "This auction only allows whole dollar amounts"
                    else:
                        lot.reserve_price = reserve_price
                except (ValueError, TypeError, InvalidOperation):
                    errors["reserve_price"] = "Minimum bid must be a number"

                if self.auction.reserve_price == "required" and not reserve_price:
                    errors["reserve_price"] = "Minimum bid is required"
            else:
                lot.reserve_price = self.auction.minimum_bid

            # Buy now price
            if self.auction.buy_now != "disable":
                buy_now_price = data.get("buy_now_price")
                if buy_now_price is not None and buy_now_price != "":
                    try:
                        buy_now_price = Decimal(str(buy_now_price))
                        if buy_now_price < Decimal("0.01"):
                            errors["buy_now_price"] = "Buy now price must be at least $0.01"
                        elif buy_now_price > 1000:
                            errors["buy_now_price"] = "Buy now price must be $1000 or less"
                        elif self.auction.only_whole_dollar_bids and buy_now_price != buy_now_price.to_integral_value():
                            errors["buy_now_price"] = "This auction only allows whole dollar amounts"
                        else:
                            lot.buy_now_price = buy_now_price
                    except (ValueError, TypeError, InvalidOperation):
                        errors["buy_now_price"] = "Buy now price must be a number"
                else:
                    lot.buy_now_price = None

                if self.auction.buy_now == "required" and not buy_now_price:
                    errors["buy_now_price"] = "Buy now price is required"
            else:
                lot.buy_now_price = None

            if errors:
                return JsonResponse({"success": False, "errors": errors})

            # Save the lot - locking is handled in Lot.save() for both standard and seller_dash modes
            lot.save()

            # Create auction history entry
            if is_new:
                # New lot created
                AuctionHistory.objects.create(
                    auction=self.auction,
                    user=request.user,
                    action=f"created lot #{lot.lot_number_display}: {lot.lot_name}",
                    applies_to="LOTS",
                )
            else:
                # Check if lot is more than 20 minutes old
                lot_age = timezone.now() - lot.date_posted
                if lot_age.total_seconds() > 1200:  # 20 minutes in seconds
                    AuctionHistory.objects.create(
                        auction=self.auction,
                        user=request.user,
                        action=f"edited lot #{lot.lot_number_display}: {lot.lot_name}",
                        applies_to="LOTS",
                    )

            # Update invoice
            invoice = Invoice.objects.filter(auctiontos_user=self.tos, auction=self.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=self.tos, auction=self.auction)
            invoice.recalculate()

            return JsonResponse(
                {
                    "success": True,
                    "lot_id": lot.lot_number,
                    "lot_number_display": lot.lot_number_display,
                    "lot_link": lot.lot_link,
                    "lot_pk": lot.pk,
                    "is_new": is_new,
                    "admin_bypassed_lot_limit": admin_bypassed_lot_limit,
                    "admin_bypassed_selling_allowed": admin_bypassed_selling_allowed,
                }
            )

        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON data"})
        except Exception:
            logger.exception("Failed to save lot via lot modal for auction %s", self.auction.pk)
            return JsonResponse({"success": False, "error": "Unable to save lot."})

    def dispatch(self, request, *args, **kwargs):
        # Let DRF's IsAuthenticated permission return a clean 401/403 instead of crashing
        # on request.user.email below when an unauthenticated (e.g. expired session) request comes in
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        self.get_auction(kwargs.pop("slug", ""))

        # Get bidder_number from POST data if present (for admin adding lots for specific user)
        bidder_number = None
        if request.method == "POST":
            try:
                data = json.loads(request.body)
                bidder_number = data.get("bidder_number")
            except (json.JSONDecodeError, AttributeError):
                pass

        # Security check: Only admins can specify a bidder_number
        self.is_admin = self.is_auction_admin
        if bidder_number and not self.is_admin:
            return JsonResponse({"success": False, "error": "Only auction admins can add lots for other users"})

        # Get the TOS - either for specified bidder or for current user
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
            if not self.tos:
                return JsonResponse({"success": False, "error": "User not found in this auction"})
        else:
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )

        if not self.tos:
            return JsonResponse({"success": False, "error": "You must join this auction first"})

        if not self.tos.selling_allowed and not self.is_admin:
            return JsonResponse({"success": False, "error": "You don't have permission to add lots"})

        if not self.is_admin and not self.auction.can_submit_lots:
            return JsonResponse({"success": False, "error": "Lot submission has ended"})

        return super().dispatch(request, *args, **kwargs)


class ImportLotsFromCSV(LoginRequiredMixin, CSVContactImportMixin, AuctionViewMixin, View):
    """Import or update lots from a CSV file.

    Each row either updates an existing lot (matched by lot number) or creates a lot under a seller (an
    AuctionTOS, matched/created by normalized email then name). The shared preview surfaces lots to
    create/update, seller possible-duplicates (merge into existing vs create new), and skipped rows with
    reasons before anything is written."""

    import_record_kind = "lot"
    import_supports_duplicates = True
    import_preview_columns = (
        ("Lot #", "lot_number"),
        ("Lot name", "lot_name"),
        ("Seller", "name"),
        ("Email", "email"),
    )

    LOT_NUMBER_FIELDS = ["lot number", "lot_number", "lot #", "number"]
    EMAIL_FIELDS = ["email", "e-mail", "email address", "e-mail address"]
    NAME_FIELDS = ["name", "full name", "first name", "firstname", "bidder name"]
    LOT_NAME_FIELDS = ["lot name", "lot_name", "item", "item name"]
    DESCRIPTION_FIELDS = ["description", "desc", "details"]
    QUANTITY_FIELDS = ["quantity", "qty", "amount"]
    RESERVE_PRICE_FIELDS = ["reserve price", "reserve_price", "minimum bid", "min bid", "starting bid"]
    BUY_NOW_PRICE_FIELDS = ["buy now price", "buy_now_price", "buy now", "buynow"]
    CATEGORY_FIELDS = ["category", "species category", "species_category"]
    BRED_FIELDS = ["breeder points", "i bred this fish", "i_bred_this_fish", "bred"]
    DONATION_FIELDS = ["donation", "donate"]

    def import_target_id(self):
        return f"auction:{self.auction.pk}"

    def import_done_url(self):
        return reverse("auction_lot_list", kwargs={"slug": self.auction.slug})

    def import_cancel_url(self):
        return reverse("auction_lot_list", kwargs={"slug": self.auction.slug})

    def get(self, request, *args, **kwargs):
        preview_token = request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        return self._hx_aware_redirect(self.import_cancel_url())

    def post(self, request, *args, **kwargs):
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        csv_file = request.FILES.get("csv_file", None)
        if not csv_file:
            messages.error(request, "No CSV file provided")
            return self._hx_aware_redirect(self.import_cancel_url())
        return self.handle_csv_upload(csv_file)

    def _custom_field_specs(self):
        """Resolve the auction's configurable custom-field column names + valid dropdown values."""
        checkbox_fields = ["custom checkbox", "custom_checkbox"]
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            checkbox_fields.append(self.auction.custom_checkbox_name.lower())
        field_1_fields = ["custom field", "custom_field_1", "custom field 1"]
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            field_1_fields.append(self.auction.custom_field_1_name.lower())
        dropdown_fields = ["custom dropdown", "custom_dropdown"]
        if self.auction.custom_dropdown_name:
            dropdown_fields.append(self.auction.custom_dropdown_name.lower())
        dropdown_options = set()
        if self.auction.use_custom_dropdown_field != "disable" and self.auction.custom_dropdown_name:
            dropdown_options = set(AuctionDropdown.objects.filter(auction=self.auction).values_list("value", flat=True))
            if len(dropdown_options) < 2:
                dropdown_options = set()
        return checkbox_fields, field_1_fields, dropdown_fields, dropdown_options

    @staticmethod
    def _to_bool(value):
        if isinstance(value, str):
            return value.lower() in ["yes", "true", "1", "y", "t"]
        return bool(value)

    @staticmethod
    def _to_int(value, default=None):
        try:
            return int(value) if value else default
        except (TypeError, ValueError):
            return default

    def _parse_lot_row(self, row):
        checkbox_fields, field_1_fields, dropdown_fields, dropdown_options = self._custom_field_specs()
        custom_dropdown = self.extract_csv_field(row, dropdown_fields)
        if custom_dropdown not in dropdown_options:
            custom_dropdown = ""
        category_name = self.extract_csv_field(row, self.CATEGORY_FIELDS)
        category = Category.objects.filter(name__iexact=category_name).first() if category_name else None
        return {
            "lot_number": self.extract_csv_field(row, self.LOT_NUMBER_FIELDS),
            "email": normalize_email(self.extract_csv_field(row, self.EMAIL_FIELDS))[:254],
            "name": self.extract_csv_field(row, self.NAME_FIELDS)[:181],
            "lot_name": self.extract_csv_field(row, self.LOT_NAME_FIELDS)[:40],
            "description": self.extract_csv_field(row, self.DESCRIPTION_FIELDS),
            "quantity": self._to_int(self.extract_csv_field(row, self.QUANTITY_FIELDS, "1"), 1),
            "reserve_price": self._to_int(self.extract_csv_field(row, self.RESERVE_PRICE_FIELDS)),
            "buy_now_price": self._to_int(self.extract_csv_field(row, self.BUY_NOW_PRICE_FIELDS)),
            "category_id": category.pk if category else None,
            "i_bred_this_fish": self._to_bool(self.extract_csv_field(row, self.BRED_FIELDS)),
            "donation": self._to_bool(self.extract_csv_field(row, self.DONATION_FIELDS)),
            "custom_checkbox": self._to_bool(self.extract_csv_field(row, checkbox_fields)),
            "custom_field_1": self.extract_csv_field(row, field_1_fields)[:60],
            "custom_dropdown": custom_dropdown,
        }

    def _find_lot(self, lot_number):
        if not lot_number:
            return None
        lot = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, custom_lot_number=lot_number).first()
        if lot:
            return lot
        lot_number_int = self._to_int(lot_number)
        if lot_number_int is not None:
            return (
                Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, lot_number_int=lot_number_int).first()
            )
        return None

    @staticmethod
    def _seller_label(tos):
        label = tos.name or "(no name)"
        if tos.email:
            return f"{label} ({tos.email})"
        return label

    def _seller_invoice_open(self, tos):
        invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
        return invoice is None or invoice.status == "DRAFT"

    def plan_row(self, row):
        fields = self._parse_lot_row(row)
        base = {"fields": fields, "target_pk": None, "target_display": "", "match_type": None, "seller_pk": None}
        # Step 1: update an existing lot matched by lot number (no seller involved)
        lot = self._find_lot(fields["lot_number"])
        if lot:
            return {**base, "action": "update", "target_pk": lot.pk, "reason": "Update existing lot"}
        # Step 2: a new lot needs both a seller name and email
        if not fields["name"] or not fields["email"]:
            return {**base, "action": "skip", "reason": "Missing lot number and complete bidder information"}
        if not fields["lot_name"]:
            return {**base, "action": "skip", "reason": "Missing required lot information (lot name)"}
        # Step 3: resolve the seller — exact email match attaches silently; a name-only match is a duplicate
        seller_by_email = self.auction.find_user(email=fields["email"])
        if seller_by_email:
            if not self._seller_invoice_open(seller_by_email):
                return {**base, "action": "skip", "reason": f"{seller_by_email.name}'s invoice is not open"}
            return {
                **base,
                "action": "create",
                "seller_pk": seller_by_email.pk,
                "reason": "New lot for existing seller",
            }
        seller_by_name = self.auction.find_user(name=fields["name"])
        if seller_by_name:
            if not self._seller_invoice_open(seller_by_name):
                return {**base, "action": "skip", "reason": f"{seller_by_name.name}'s invoice is not open"}
            return {
                **base,
                "action": "duplicate",
                "target_pk": seller_by_name.pk,
                "seller_pk": seller_by_name.pk,
                "target_display": self._seller_label(seller_by_name),
                "match_type": "name",
                "reason": "Seller name matches an existing user",
            }
        return {**base, "action": "create", "reason": "New lot and new seller"}

    def _update_lot(self, lot, fields):
        # Don't touch winner, winning_price, partial_refund, banned — only the importable fields.
        if fields.get("lot_name"):
            lot.lot_name = fields["lot_name"]
        if fields.get("description"):
            lot.summernote_description = fields["description"]
        if fields.get("quantity"):
            lot.quantity = fields["quantity"]
        if fields.get("reserve_price") is not None:
            lot.reserve_price = fields["reserve_price"]
        if fields.get("buy_now_price") is not None:
            lot.buy_now_price = fields["buy_now_price"]
        if fields.get("category_id"):
            lot.species_category_id = fields["category_id"]
        lot.i_bred_this_fish = fields.get("i_bred_this_fish", False)
        lot.donation = fields.get("donation", False)
        lot.custom_checkbox = fields.get("custom_checkbox", False)
        if fields.get("custom_field_1"):
            lot.custom_field_1 = fields["custom_field_1"]
        if fields.get("custom_dropdown"):
            lot.custom_dropdown = fields["custom_dropdown"]
        lot.save()

    def _resolve_seller(self, fields, action, decision):
        """Return (seller, created_bool). Reuses the matched seller unless the admin chose to create a new
        record for a name-match duplicate."""
        seller_pk = action.get("seller_pk")
        make_new = action["action"] == "duplicate" and decision == "create"
        if seller_pk and not make_new:
            seller = AuctionTOS.objects.filter(pk=seller_pk, auction=self.auction).first()
            if seller:
                return seller, False
        seller = AuctionTOS.objects.create(
            auction=self.auction,
            pickup_location=self.auction.location_qs.first(),
            manually_added=True,
            name=fields.get("name", ""),
            email=fields.get("email", ""),
        )
        return seller, True

    def _create_lot(self, fields, seller):
        new_lot = Lot(
            lot_name=fields.get("lot_name", ""),
            summernote_description=fields.get("description") or "",
            quantity=fields.get("quantity") or 1,
            reserve_price=(
                fields["reserve_price"] if fields.get("reserve_price") is not None else self.auction.minimum_bid
            ),
            buy_now_price=fields.get("buy_now_price"),
            i_bred_this_fish=fields.get("i_bred_this_fish", False),
            donation=fields.get("donation", False),
            custom_checkbox=fields.get("custom_checkbox", False),
            custom_field_1=fields.get("custom_field_1", ""),
            custom_dropdown=fields.get("custom_dropdown", ""),
            auctiontos_seller=seller,
            auction=self.auction,
            added_by=self.request.user,
        )
        if seller.user:
            new_lot.user = seller.user
        if fields.get("category_id"):
            new_lot.species_category_id = fields["category_id"]
        new_lot.save()

    def apply_action(self, action, decision):
        kind = action["action"]
        if kind == "skip":
            return "skipped"
        fields = action.get("fields", {})
        if kind == "update":
            lot = Lot.objects.exclude(is_deleted=True).filter(pk=action.get("target_pk"), auction=self.auction).first()
            if not lot:
                return "skipped"
            self._update_lot(lot, fields)
            return "updated"
        # create or duplicate → resolve the seller, re-check the invoice, create the lot
        seller, created_seller = self._resolve_seller(fields, action, decision)
        if not self._seller_invoice_open(seller):
            return "skipped"
        if created_seller:
            self._users_created = getattr(self, "_users_created", 0) + 1
        self._create_lot(fields, seller)
        return "created"

    def message_import_results(self, results):
        parts = []
        if results.get("created"):
            parts.append(f"{results['created']} lots created")
        if results.get("updated"):
            parts.append(f"{results['updated']} lots updated")
        users_created = getattr(self, "_users_created", 0)
        if users_created:
            parts.append(f"{users_created} users added")
        if results.get("skipped"):
            parts.append(f"{results['skipped']} rows skipped")
        if parts:
            messages.success(self.request, ", ".join(parts))

    def record_import_history(self, results, filename=None):
        if not results.get("created") and not results.get("updated"):
            return
        history_msg = f"CSV import: {results.get('created', 0)} lots created, {results.get('updated', 0)} lots updated"
        users_created = getattr(self, "_users_created", 0)
        if users_created:
            history_msg += f", {users_created} users added"
        if filename:
            history_msg += f" from {filename}"
        self.auction.create_history(applies_to="LOTS", action=history_msg, user=self.request.user)

    def process_csv_data(self, csv_reader, filename=None):
        """Parse the upload into planned actions and show the review page; nothing is written yet."""
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)


class ViewLot(DetailView):
    """Show the picture and detailed information about a lot, and allow users to place bids"""

    template_name = "view_lot_images.html"
    model = Lot
    custom_lot_number = None
    auction_slug = None
    enable_404 = True

    def dispatch(self, request, *args, **kwargs):
        self.auction_slug = kwargs.pop("slug", None)
        self.custom_lot_number = kwargs.pop("custom_lot_number", None)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        obj = self.get_queryset().first()
        if not obj and self.enable_404:
            raise Http404
        return obj

    def get_queryset(self):
        pk = self.kwargs.get(self.pk_url_kwarg)
        qs = Lot.objects.exclude(is_deleted=True)
        latitude = self.request.COOKIES.get("latitude")
        longitude = self.request.COOKIES.get("longitude")
        if latitude and longitude:
            qs = qs.annotate(distance=distance_to(latitude, longitude))
        elif self.request.user.is_authenticated:
            # UserData is auto-created when user is saved
            if self.request.user.userdata.latitude and self.request.user.userdata.longitude:
                latitude = self.request.user.userdata.latitude
                longitude = self.request.user.userdata.longitude
                if latitude and longitude:
                    qs = qs.annotate(distance=distance_to(latitude, longitude))
        if pk:
            qs = qs.filter(pk=pk)
        else:
            # we are probably here form the auction/custom lot number route
            filters = Q(
                # legacy lot numbers in auctions
                auction__isnull=False,
                auction__slug=self.auction_slug,
                auction__use_seller_dash_lot_numbering=True,
                custom_lot_number__isnull=False,
                custom_lot_number=self.custom_lot_number,
            )

            if self.custom_lot_number.isnumeric():
                filters |= Q(
                    # autogenerated int lot numbers in auctions
                    auction__isnull=False,
                    auction__slug=self.auction_slug,
                    auction__use_seller_dash_lot_numbering=False,
                    lot_number_int__isnull=False,
                    lot_number_int=self.custom_lot_number,
                )

            qs = qs.filter(filters)
        return qs

    def get_context_data(self, **kwargs):
        lot = self.get_object()
        context = super().get_context_data(**kwargs)
        context["domain"] = Site.objects.get_current().domain
        context["is_auction_admin"] = False
        if lot.auction:
            context["auction"] = lot.auction
            context["is_auction_admin"] = lot.auction.permission_check(self.request.user)
            if lot.auction.first_bid_payout and not lot.auction.invoiced:
                if not self.request.user.is_authenticated or not Bid.objects.exclude(is_deleted=True).filter(
                    user=self.request.user, lot_number__auction=lot.auction
                ):
                    messages.info(
                        self.request,
                        f"Bid on (and win) any lot in {lot.auction} and get ${lot.auction.first_bid_payout} back!",
                    )
        if self.request.user.is_authenticated:
            viewer_bid = (
                Bid.objects.exclude(is_deleted=True)
                .filter(user=self.request.user, lot_number=lot.pk)
                .order_by("-bid_time")
                .first()
            )
            if viewer_bid:
                context["viewer_bid_pk"] = viewer_bid.pk
                context["viewer_bid"] = viewer_bid.amount
                if lot.auction and not lot.auction.only_whole_dollar_bids:
                    defaultBidAmount = viewer_bid.amount + Decimal("0.01")
                else:
                    defaultBidAmount = viewer_bid.amount + 1
            else:
                defaultBidAmount = 0
                context["viewer_bid"] = None
            context["has_push_subscription"] = PushInformation.objects.filter(user=self.request.user).exists()
        else:
            defaultBidAmount = 0
            context["viewer_bid"] = None
            context["has_push_subscription"] = False
        if lot.auction and lot.auction.online_bidding == "buy_now_only" and lot.buy_now_price:
            defaultBidAmount = lot.buy_now_price
            context["force_buy_now"] = True
        else:
            context["force_buy_now"] = False
        if not lot.sealed_bid:
            # reserve price if there are no bids
            if not lot.high_bidder:
                defaultBidAmount = lot.reserve_price
            else:
                if lot.auction and not lot.auction.only_whole_dollar_bids:
                    # 5% rounded down to nearest cent, minimum $0.01
                    min_increment = max(
                        (lot.high_bid * Decimal("0.05")).quantize(Decimal("0.01"), rounding="ROUND_DOWN"),
                        Decimal("0.01"),
                    )
                else:
                    # 5% rounded down to nearest dollar, minimum $1
                    min_increment = max(
                        (lot.high_bid * Decimal("0.05")).to_integral_value(rounding="ROUND_DOWN"),
                        Decimal(1),
                    )
                if defaultBidAmount > lot.high_bid + min_increment:
                    pass
                else:
                    defaultBidAmount = lot.high_bid + min_increment
        context["viewer_pk"] = self.request.user.pk
        context["submitter_pk"] = getattr(lot.user, "pk", 0)
        context["user_specific_bidding_error"] = False
        if not self.request.user.is_authenticated:
            context["user_specific_bidding_error"] = (
                f"You have to <a href='/login/?next={lot.lot_link}'>sign in</a> to place bids."
            )
        if context["viewer_pk"] == context["submitter_pk"]:
            context["user_specific_bidding_error"] = "You can't bid on your own lot"
        context["amount"] = defaultBidAmount
        context["only_whole_dollar_bids"] = lot.auction.only_whole_dollar_bids if lot.auction else True
        context["watched"] = Watch.objects.filter(lot_number=lot.lot_number, user=self.request.user.id)
        context["category"] = lot.species_category
        # context['form'] = CreateBid(initial={'user': self.request.user.id, 'lot_number':lot.pk, "amount":defaultBidAmount}, request=self.request)
        context["user_tos"] = None
        context["user_tos_location"] = None
        if lot.auction and self.request.user.is_authenticated:
            tos = AuctionTOS.objects.filter(
                Q(user=self.request.user) | Q(email=self.request.user.email),
                auction=lot.auction,
            ).first()
            if tos:
                context["user_tos"] = True
                context["user_tos_location"] = tos.pickup_location
                if not tos.can_bid_in_auction:
                    if tos.requires_check_in_before_bidding:
                        context["user_specific_bidding_error"] = "You must check in at the event before you can bid"
                    else:
                        context["user_specific_bidding_error"] = (
                            "This auction requires admin approval before you can bid"
                        )
            else:
                context["user_specific_bidding_error"] = (
                    f"This lot is part of <b>{lot.auction}</b>. Please <a href='/auctions/{lot.auction.slug}/?next={lot.lot_link}#join'>read the auction's rules and join the auction</a> to bid<br>"
                )
            if not lot.auction.is_online and lot.auction.message_users_when_lots_sell:
                context["push_notifications_possible"] = True
        if lot.within_dynamic_end_time and lot.minutes_to_end > 0 and not lot.sealed_bid:
            messages.info(
                self.request,
                "Bidding is ending soon.  Bids placed now will extend the end time of this lot.  This page will update automatically, you don't need to reload it",
            )
        if not context["user_tos"] and not lot.ended and lot.auction:
            if lot.auction.online_bidding != "disable":
                messages.info(
                    self.request,
                    f"Please <a href='/auctions/{lot.auction.slug}/?next=/lots/{lot.pk}/'>read the auction's rules and join the auction</a> to bid",
                )
        if self.request.user.is_authenticated:
            userData = self.request.user.userdata
            userData.last_activity = timezone.now()
            userData.save()
            if userData.last_ip_address:
                if userData.last_ip_address != lot.seller_ip and lot.bidder_ip_same_as_seller:
                    messages.info(
                        self.request,
                        "Heads up: one of the bidders on this lot has the same IP address as the seller of this lot.  This can happen when someone is bidding on their own lots.  Never bid more than a lot is worth to you.",
                    )
        if lot.user:
            if lot.user.pk == self.request.user.pk:
                LotHistory.objects.filter(lot=lot.pk, seen=False).update(seen=True)
        context["bids"] = []
        if lot.auction:
            if context["is_auction_admin"]:
                context["bids"] = lot.bids
        context["debug"] = settings.DEBUG
        try:
            if lot.local_pickup:
                context["distance"] = f"{int(lot.distance)} miles away"
            else:
                distances = [25, 50, 100, 200, 300, 500, 1000, 2000, 3000]
                for distance in distances:
                    if lot.distance < distance:
                        context["distance"] = f"less than {distance} miles away"
                        break
                if lot.distance > 3000:
                    context["distance"] = "over 3000 miles away"
        except (AttributeError, TypeError):
            context["distance"] = 0
        # for lots that are part of an auction, it's very handy to show the exchange info right on the lot page
        # this should be visible only to people running the auction or the seller
        if lot.auction and lot.auction.is_online and lot.sold:
            if context["is_auction_admin"] or self.request.user == lot.user:
                context["show_exchange_info"] = True
        context["show_image_add_button"] = lot.image_permission_check(self.request.user)
        context["show_bap_badge"] = False
        context["bap_eligible_reason"] = None
        context["bap_eligible_reason_display"] = None
        if lot.auction and lot.auction.club:
            seller_user = lot.user or (lot.auctiontos_seller.user if lot.auctiontos_seller else None)
            viewer = self.request.user
            viewer_is_seller = viewer.is_authenticated and seller_user and viewer == seller_user
            viewer_has_bap = viewer.is_authenticated and check_club_permission(
                viewer, lot.auction.club, "permission_manage_bap"
            )
            if viewer_is_seller or viewer_has_bap:
                context["show_bap_badge"] = True
                if lot.ended and not lot.sold:
                    club = lot.auction.club if lot.auction else None
                    reason = "not_sold" if (not club or club.only_sold_lots) else lot.unsold_lot_no_bap_reason
                else:
                    reason = lot.unsold_lot_no_bap_reason
                context["bap_eligible_reason"] = reason
                if reason:
                    context["bap_eligible_reason_display"] = dict(lot.BAP_REASON_CHOICES).get(reason, reason)
            context["viewer_has_bap"] = viewer_has_bap
            if viewer_has_bap and lot.sold:
                club = lot.auction.club
                context["bap_club"] = club
                _bap_override = (
                    ClubBapCategoryOverride.objects.filter(club=club, category=lot.species_category).first()
                    if lot.species_category
                    else None
                )
                context["bap_default_points"] = (
                    _bap_override.points
                    if _bap_override is not None
                    else (club.points_per_lot or (lot.species_category.bap_points if lot.species_category else 5))
                )
        if lot.use_images_from and self.request.user.is_authenticated:
            is_lot_creator = (lot.user and lot.user == self.request.user) or (
                lot.auctiontos_seller and lot.auctiontos_seller.user == self.request.user
            )
            if is_lot_creator:
                context["images_managed_from_lot"] = lot.use_images_from
        # chat subscription stuff
        if self.request.user.is_authenticated:
            context["show_chat_subscriptions_checkbox"] = True
            context["autocheck_chat_subscriptions"] = "false"
            existing_subscription = ChatSubscription.objects.filter(lot=lot, user=self.request.user).first()
            if (
                lot.user
                and lot.user == self.request.user
                and not self.request.user.userdata.email_me_when_people_comment_on_my_lots
            ):
                context["show_chat_subscriptions_checkbox"] = False
            if lot.user != self.request.user and not self.request.user.userdata.email_me_about_new_chat_replies:
                context["show_chat_subscriptions_checkbox"] = False
            if self.request.user.userdata.email_me_about_new_chat_replies and not existing_subscription:
                context["autocheck_chat_subscriptions"] = "true"
            if existing_subscription:
                context["chat_subscriptions_is_checked"] = not existing_subscription.unsubscribed
                context["autocheck_chat_subscriptions"] = "false"
            else:
                context["chat_subscriptions_is_checked"] = False
        if (
            lot.auctiontos_winner
            and self.request.user.is_authenticated
            and self.request.user.email == lot.auctiontos_winner.email
        ) or (lot.winner and self.request.user.is_authenticated and self.request.user == lot.winner):
            if lot.feedback_rating == 0 and lot.date_end and timezone.now() > lot.date_end + timedelta(days=2):
                context["show_feedback_dialog"] = True
        return context


class ViewLotSimple(ViewLot, AuctionViewMixin):
    """Minimalist view of a lot, just image and description.  For htmx calls"""

    template_name = "view_lot_simple.html"
    enable_404 = False

    def get_context_data(self, **kwargs):
        context = DetailView.get_context_data(self, **kwargs)
        lot = self.get_object()
        context["lot"] = lot
        if lot and lot.auction:
            self.auction = lot.auction
            if self.is_auction_admin and self.auction.message_users_when_lots_sell and not lot.sold:
                result = {
                    "type": "chat_message",
                    "info": "CHAT",
                    "message": "This lot is about to be sold!",
                    "pk": -1,
                    "username": "System",
                }
                lot.send_websocket_message(result)
                watchers = Watch.objects.filter(
                    lot_number=lot.pk, user__userdata__push_notifications_when_lots_sell=True
                ).exclude(
                    # it would be awkward to have notifications pop up when you're projecting an image of the lot
                    user=self.request.user
                )
                for watch in watchers:
                    # does the user actually have a subscription?
                    push_info = PushInformation.objects.filter(user=watch.user).first()
                    if push_info:
                        payload = {
                            "head": lot.lot_name + " is about to be sold",
                            "body": f"Lot {lot.lot_number_display}  Don't miss out, bid now!  You're getting this notification because you watched this lot.",
                            "url": "https://" + lot.full_lot_link,
                            "tag": f"lot_sell_notification_{lot.pk}",
                        }
                        if lot.thumbnail:
                            payload["icon"] = lot.thumbnail.display_url
                        try:
                            send_user_notification(user=watch.user, payload=payload, ttl=10000)
                        except (requests.exceptions.RequestException, WebPushException):
                            # The push endpoint is invalid or unreachable; remove the stale subscription
                            # and record the failure in the auction history so admins can see it.
                            # Note: django-webpush only auto-deletes on HTTP 410, but FCM uses
                            # HTTP 404 for expired tokens, so we must also handle that here.
                            push_info.delete()
                            AuctionHistory.objects.create(
                                auction=lot.auction,
                                user=None,
                                action=f"push notification error occurred for {watch.user.username}",
                                applies_to="USERS",
                            )
        return context


class ImageCreateView(LoginRequiredMixin, CreateView):
    """Add an image to a lot"""

    model = LotImage
    template_name = "image_form.html"
    form_class = CreateImageForm

    def get_lot(self, request, *args, **kwargs):
        return get_object_or_404(Lot, pk=kwargs["lot"], is_deleted=False)

    def dispatch(self, request, *args, **kwargs):
        self.lot = self.get_lot(self, request, *args, **kwargs)
        if not self.lot:
            messages.info(
                request,
                f"All lots for {self.tos.bidder_number} already have an image",
            )
            return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        # try:
        #     self.lot = Lot.objects.get(lot_number=kwargs["lot"], is_deleted=False)
        # except:
        #     raise Http404
        if not self.lot.image_permission_check(request.user):
            messages.error(request, "You can't add an image to this lot")
            return redirect(self.get_success_url())
        if self.lot.image_count > 5:
            messages.error(
                request,
                "You can't add another image to this lot.  Delete one and try again",
            )
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = self.lot.lot_link
        return data["next"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Add image to {self.lot.lot_name}"
        if self.lot.use_images_from:
            context["images_managed_from_lot"] = self.lot.use_images_from
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        uploaded_image = form.cleaned_data.get("image")

        # Attempt to convert non-standard JPEG formats (like MPO) to standard JPEG. The
        # upload was already validated in the form, so this is best-effort: if it fails we
        # just fall through and let the normal save path try the original file.
        if isinstance(uploaded_image, UploadedFile):
            try:
                with Image.open(uploaded_image) as img:
                    if img.format != "JPEG":
                        img = img.convert("RGB")  # Ensure it's in a JPEG-safe mode
                        buffer = BytesIO()
                        img.save(buffer, format="JPEG")
                        buffer.seek(0)
                        image.image.save(
                            uploaded_image.name.replace(".jpeg", "") + ".jpg", ContentFile(buffer.read()), save=False
                        )
            except IMAGE_PROCESSING_EXCEPTIONS as e:
                logger.info("Could not pre-convert uploaded image to JPEG: %s", e)

        try:
            image.save()
        except IMAGE_PROCESSING_EXCEPTIONS as e:
            # The image itself is unusable (bad format, corrupt, decompression bomb...).
            # Show the uploader a friendly, actionable error.
            logger.info("Rejected lot image during save: %s", e)
            form.add_error(
                "image",
                "We couldn't process that image -- it may be corrupt or in an unsupported format. "
                "Please try a different photo.",
            )
            return self.form_invalid(form)
        # Anything else (permission denied writing to mediafiles, disk full, database
        # errors...) is a server/site problem, not the user's file. Let it propagate so it
        # becomes a 500 and the admins get emailed instead of blaming the uploader's photo.
        return super().form_valid(form)


class QuickBulkAddImages(ImageCreateView):
    """Add images to any lots that don't have one"""

    def get_lot(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.tos = get_object_or_404(AuctionTOS, bidder_number=kwargs.pop("bidder_number"), auction=self.auction)
        return (
            Lot.objects.filter(auctiontos_seller=self.tos, winning_price__isnull=True)
            .exclude(lotimage__isnull=False)
            .distinct()
            .order_by("date_posted")
            .first()
        )

    def get_success_url(self):
        return reverse("bulk_add_image", kwargs={"slug": self.auction.slug, "bidder_number": self.tos.bidder_number})


class ImageUpdateView(UpdateView):
    """Edit an existing image"""

    model = LotImage
    template_name = "image_form.html"
    form_class = CreateImageForm

    def dispatch(self, request, *args, **kwargs):
        try:
            self.lot = self.get_object().lot_number
        except AttributeError:
            raise Http404
        if not self.lot.image_permission_check(request.user):
            messages.error(request, "You can't change this image")
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return self.get_object().lot_number.lot_link

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Editing image for {self.get_object().lot_number.lot_name}"
        if self.lot.use_images_from:
            context["images_managed_from_lot"] = self.lot.use_images_from
        return context

    def form_valid(self, form, **kwargs):
        """A bit of cleanup"""
        image = form.save(commit=False)
        image.lot_number = self.lot
        if not self.lot.image_count:
            image.is_primary = True
        if not image.image_source:
            image.image_source = "RANDOM"
        image.save()
        messages.success(self.request, "Image updated")
        return super().form_valid(form)


class LotValidation(LoginRequiredMixin):
    """
    Base class for adding a lot.  This defines the rules for validating a lot
    """

    auction = None  # used for specifying which auction via GET param

    def dispatch(self, request, *args, **kwargs):
        # if the user hasn't filled out their address, redirect:
        userData = request.user.userdata
        if not userData.address or not request.user.first_name or not request.user.last_name:
            messages.error(self.request, "Please fill out your contact info before creating a lot")
            return redirect(f"{reverse('contact_info')}?{urlencode({'next': request.get_full_path()})}")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form, **kwargs):
        """
        There is quite a lot that needs to be done before the lot is saved
        """
        lot = form.save(commit=False)
        lot.user = self.request.user
        lot.date_of_last_user_edit = timezone.now()
        if lot.buy_now_price:
            if lot.buy_now_price < lot.reserve_price:
                lot.buy_now_price = lot.reserve_price
                messages.error(
                    self.request,
                    "Buy now price can't be lower than the minimum bid.  Buy now price has been set to the minimum bid, but you should probably edit this lot and change the buy now price.",
                )
        if lot.auction:
            # if not lot.auction.is_online:
            #    if lot.buy_now_price or lot.reserve_price > lot.auction.minimum_bid:
            #        messages.info(self.request, f"Reserve and buy now prices may not be used in this auction.  Read the auction's rules for more information")
            if lot.auction.reserve_price == "disable":
                lot.reserve_price = lot.auction.minimum_bid
            if lot.auction.buy_now == "disable" and lot.buy_now_price:
                lot.buy_now_price = None
            if (lot.auction.buy_now == "require") and not lot.buy_now_price:
                lot.buy_now_price = lot.auction.minimum_bid
                messages.error(self.request, "You need to set a buy now price for this lot!")
            lot.date_end = lot.auction.date_end
            userData = self.request.user.userdata
            userData.last_auction_used = lot.auction
            userData.last_activity = timezone.now()
            userData.save()
            auctiontos = AuctionTOS.objects.filter(user=self.request.user, auction=lot.auction).first()
            if not auctiontos:
                # it should not be possible to get here (famous last words...)
                # remember that on form submit in CreateLotForm.clean(), we are validating that the user has an auctiontos
                messages.error(
                    self.request,
                    f"You need to <a href='/auctions/{lot.auction.slug}'>confirm your pickup location for this auction</a> before people can bid on this lot.",
                )
            else:
                lot.auctiontos_seller = auctiontos
                invoice = Invoice.objects.filter(auctiontos_user=auctiontos, auction=lot.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=lot.auction)
                invoice.recalculate()
        else:
            # this lot is NOT part of an auction
            try:
                run_duration = int(form.cleaned_data["run_duration"])
            except (ValueError, KeyError):
                run_duration = 10
            if not lot.date_posted:
                lot.date_posted = timezone.now()
            lot.date_end = lot.date_posted + timedelta(days=run_duration)
            lot.lot_run_duration = run_duration
            lot.donation = False
        # someday we may change this to be a field on the form, but for now we need to collect data
        lot.promotion_weight = randint(0, 20)
        if lot.pk:
            # this is an existing lot
            lot.save()
        else:
            # this is a new lot
            lot.added_by = self.request.user
            lot.save()
            # if this was cloned from another lot, get the images from that lot
            if form.cleaned_data["cloned_from"]:
                try:
                    original_lot = Lot.objects.get(pk=form.cleaned_data["cloned_from"], is_deleted=False)
                    if original_lot.user_id == self.request.user.pk or self.request.user.is_superuser:
                        original_images = LotImage.objects.filter(lot_number=original_lot.lot_number)
                        for original_image in original_images:
                            new_image = LotImage.objects.create(
                                createdon=original_image.createdon,
                                lot_number=lot,
                                image_source=original_image.image_source,
                                is_primary=original_image.is_primary,
                                url=original_image.url,
                            )
                            if original_image.image:
                                new_image.image = get_thumbnailer(original_image.image)
                                # both rows share the same file, so they share the same Cloudflare image
                                new_image.cloudflare_image_id = original_image.cloudflare_image_id
                            # if the original lot sold, this picture sure isn't of the actual item
                            if original_lot.winner and original_image.image_source == "ACTUAL":
                                new_image.image_source = "REPRESENTATIVE"
                            new_image.save()
                        # we are only cloning images here, not watchers, views, or other related models
                except Exception as e:
                    logger.exception(e)
            msg = "Created lot! "
            if not lot.image_count:
                msg += f"You should probably <a href='/images/add_image/{lot.lot_number}/'>add an image</a>  to this lot.  Or, "
            msg += "<a href='/lots/new'>create another lot</a>"
            messages.success(
                self.request,
                msg,
            )
        # if image_url is set, add an image to the lot using this URL, then clear the field
        image_url = form.cleaned_data.get("image_url")
        if image_url:
            try:
                validate_image_url(image_url)
                # check direct images on this lot (not delegated via use_images_from) for is_primary
                LotImage.objects.create(
                    lot_number=lot,
                    url=image_url,
                    is_primary=not LotImage.objects.filter(lot_number=lot).exists(),
                    image_source="RANDOM",
                )
            except ValidationError:
                messages.error(self.request, "The image URL provided was not valid and will not be used.")
            lot.image_url = None
            lot.save(update_fields=["image_url"])
        return super().form_valid(form)

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["auction"] = self.auction
        kwargs["user"] = self.request.user
        data = self.request.GET.copy()
        kwargs["cloned_from"] = data.get("copy", None)
        return kwargs


class LotCreateView(LotValidation, CreateView):
    """
    Creating a new lot
    """

    model = Lot
    template_name = "lot_form.html"
    form_class = CreateLotForm
    auction = None

    # it's better to take the user to the lot they just added, in case they want to edit it
    # def get_success_url(self):
    #    return "/lots/new/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "New lot"
        context["new"] = True

        # Check if user needs to see the modal about joining an auction
        userData = self.request.user.userdata
        can_sell_independently = userData.can_submit_standalone_lots

        # Get available auctions for this user
        available_auctions = userData.available_auctions_to_submit_lots

        # Show modal if user can't sell independently and has no available auctions
        context["show_no_auction_modal"] = not can_sell_independently and not available_auctions.exists()
        context["last_auction_name"] = None
        context["lot_submission_ended_message"] = None

        # If they have a last used auction, check if lot submission has ended
        if userData.last_auction_used and context["show_no_auction_modal"]:
            last_auction = userData.last_auction_used
            context["last_auction_name"] = last_auction.title
            if last_auction.lot_submission_end_date and last_auction.lot_submission_end_date < timezone.now():
                context["lot_submission_ended_message"] = (
                    f"Lot submission has ended for the {last_auction.title} auction"
                )

        return context

    def get_initial(self):
        """Pre-fill form fields from GET params. Any field in the form can be set this way.
        The 'auction' param is handled separately in dispatch() and 'cloned_from' in get_form_kwargs()."""
        initial = super().get_initial()
        exclude = {"auction", "cloned_from"}
        form_fields = set(self.form_class.Meta.fields) | set(self.form_class.declared_fields)
        field_objects = getattr(self.form_class, "base_fields", {})
        # Identify checkbox-like fields so we can coerce their initial values properly
        checkbox_fields = {
            name
            for name, field in field_objects.items()
            if getattr(getattr(field, "widget", None), "input_type", None) == "checkbox"
        }
        true_values = {"1", "true", "yes", "on"}
        false_values = {"0", "false", "no", "off"}
        for key, values in self.request.GET.lists():
            if key in form_fields and key not in exclude:
                field = field_objects.get(key)
                if field is not None and getattr(field.widget, "allow_multiple_selected", False):
                    initial[key] = values
                elif key in checkbox_fields:
                    # For checkbox fields, use the last value (multiple values shouldn't occur)
                    normalized = values[-1].strip().lower() if values else ""
                    if normalized in true_values:
                        initial[key] = True
                    elif normalized in false_values:
                        initial[key] = False
                    else:
                        initial[key] = values[-1] if values else ""
                else:
                    # For single-value fields, last value wins (mirrors QueryDict.items() behavior)
                    if values:
                        initial[key] = values[-1]
        return initial

    def form_valid(self, form, **kwargs):
        """When a new lot is created, make sure to create an invoice for the seller"""
        lot = form.save(commit=False)
        if lot.auction and lot.auctiontos_seller:
            invoice = Invoice.objects.filter(auctiontos_user=lot.auctiontos_seller, auction=lot.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=lot.auctiontos_seller, auction=lot.auction)
            invoice.recalculate()
        result = super().form_valid(form, **kwargs)
        # Create history after lot is saved and has a lot_number_display
        if lot.auction and lot.auctiontos_seller:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Added lot {lot.lot_number_display} {lot.lot_name}",
                user=self.request.user,
            )
        return result

    def dispatch(self, request, *args, **kwargs):
        userData = self.request.user.userdata
        if userData.last_auction_used:
            if (
                userData.last_auction_used.can_submit_lots
                and not userData.last_auction_used.is_online
                and userData.last_auction_used.allow_bulk_adding_lots
            ):
                messages.info(
                    request,
                    f"Sick of adding lots one at a time?  <a href='{reverse('bulk_add_lots_auto_for_myself', kwargs={'slug': userData.last_auction_used.slug})}'>Add lots of lots to {userData.last_auction_used}</a>",
                )
        data = self.request.GET.copy()
        auction_slug = data.get("auction", None)
        if auction_slug:
            self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=auction_slug).first()
            if self.auction:
                error = None
                if timezone.now() < self.auction.lot_submission_start_date:
                    error = "Lot submission has not opened yet for this auction."
                if self.auction.lot_submission_end_date:
                    if self.auction.lot_submission_end_date < timezone.now():
                        error = "Lot submission has ended for this auction."
                tos = AuctionTOS.objects.filter(user=self.request.user, auction=self.auction).first()
                if not tos:
                    error = "You haven't joined this auction yet.  Click the green button at the bottom of this page to join the auction.</a>"
                if error:
                    messages.error(request, error)
                    return redirect(self.auction.get_absolute_url())
        return super().dispatch(request, *args, **kwargs)


class LotUpdate(LotValidation, UpdateView):
    """
    Changing an existing lot
    This is almost identical to the create view, but needs to verify permissions to edit the lot
    """

    model = Lot
    template_name = "lot_form.html"
    form_class = CreateLotForm

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the lot creator can edit a lot")
            return redirect(reverse("home"))
        if not self.get_object().can_be_edited:
            messages.error(request, self.get_object().cannot_be_edited_reason)
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse("selling")
        # return f"/lots/{self.kwargs['pk']}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = f"Edit {self.get_object().lot_name}"
        return context

    def form_valid(self, form):
        """Track history when a lot is edited"""
        lot = self.get_object()
        # Check if we should create history before saving
        should_create_history = lot.auction and form.has_changed()
        # Save the form
        result = super().form_valid(form)
        # Create history after successful update
        if should_create_history:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Edited lot {lot.lot_number_display}",
                user=self.request.user,
                form=form,
            )
        return result


class AuctionDelete(LoginRequiredMixin, AuctionViewMixin, DeleteView):
    model = Auction

    def dispatch(self, request, *args, **kwargs):
        result = super().dispatch(request, *args, **kwargs)
        # self.auction may not be set if LoginRequiredMixin redirected
        if hasattr(self, "auction") and self.auction and not self.auction.can_be_deleted:
            messages.error(request, "There are already lots in this auction, it can't be deleted")
            return redirect(reverse("home"))
        return result

    def get_success_url(self):
        return reverse("auctions")


class LotDelete(LoginRequiredMixin, DeleteView):
    model = Lot

    def dispatch(self, request, *args, **kwargs):
        if not self.get_object().can_be_deleted:
            messages.error(request, self.get_object().cannot_be_deleted_reason)
            return redirect(reverse("home"))
        if not (request.user.is_superuser or self.get_object().user == self.request.user):
            messages.error(request, "Only the creator of a lot can delete it")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        next_url = self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("selling")

    def form_valid(self, form):
        """Track history when a lot is deleted"""
        lot = self.get_object()
        if lot.auction:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"Deleted lot {lot.lot_number_display}",
                user=self.request.user,
            )
        messages.info(self.request, f"Successfully deleted lot {lot.lot_number_display} {lot.lot_name}")
        return super().form_valid(form)


class ImageDelete(LoginRequiredMixin, DeleteView):
    model = LotImage

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().lot_number.user == request.user:
            auth = True
        if not self.get_object().lot_number.can_be_edited:
            auth = False
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "You can't change this image")
            return redirect(self.get_object().lot_number.get_absolute_url())
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        if self.get_object().is_primary:
            # in this case, we need to set a new primary image
            try:
                newImage = (
                    LotImage.objects.filter(lot_number=self.get_object().lot_number)
                    .exclude(pk=self.get_object().pk)
                    .order_by("createdon")[0]
                )
                newImage.is_primary = True
                newImage.save()
            except IndexError:
                pass
        return self.get_object().lot_number.get_absolute_url()


class BidDelete(LoginRequiredMixin, DeleteView):
    model = Bid
    removing_own_bid = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["removing_own_bid"] = self.removing_own_bid
        return context

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if not self.get_object().lot_number.bids_can_be_removed:
            messages.error(request, "You can no longer remove bids from this lot.")
            return redirect(self.get_success_url())
        if self.get_object().lot_number.auction:
            if self.get_object().lot_number.auction.allow_deleting_bids and request.user == self.get_object().user:
                if request.user == self.get_object().user:
                    self.removing_own_bid = True
                    auth = True
            if self.get_object().lot_number.auction.permission_check(self.request.user):
                auth = True
        if not auth:
            messages.error(
                request,
                "Your account doesn't have permission to remove bids from this lot",
            )
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        bid = self.get_object()
        lot = bid.lot_number
        success_url = self.get_success_url()
        if self.removing_own_bid:
            own_bid_removal_messages = [
                "{user} got cold feet and withdrew their bid!",
                "{user} has bravely retreated from the bidding war!",
                "And just like that, {user}'s bid vanished into thin air!",
                "The elusive {user} has chickened out and removed their bid!",
                "Looks like {user} couldn't handle the heat and pulled their bid!",
                "{user} just remembered they haven't paid rent this month and removed their bid!",
            ]
            history_message = secrets.choice(own_bid_removal_messages).format(user=self.request.user)
        else:
            history_message = f"{self.request.user} has removed {bid.user}'s bid"
        if lot.ended:
            lot.winner = None
            lot.auctiontos_winner = None
            lot.winning_price = None
            if lot.auction and lot.auction.date_end:
                lot.date_end = lot.auction.date_end
            else:
                lot.date_end = timezone.now() + timedelta(days=lot.lot_run_duration)
            lot.active = True
            lot.buy_now_used = False
            if lot.label_printed:
                lot.label_needs_reprinting = True
            lot.save()
        bid.delete()
        # Also soft-delete any other bid records for this user on the same lot
        Bid.objects.exclude(is_deleted=True).filter(
            user=bid.user,
            lot_number=lot,
        ).update(is_deleted=True)
        LotHistory.objects.create(lot=lot, user=self.request.user, message=history_message, changed_price=True)
        return HttpResponseRedirect(success_url)

    def get_success_url(self):
        return self.get_object().lot_number.get_absolute_url()


class LotAdmin(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Creation and management for Lots that are part of an auction"""

    template_name = "auctions/generic_admin_form.html"
    form_class = EditLot
    model = Lot

    def get_queryset(self):
        return Lot.objects.all()

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop("pk")
        try:
            self.lot = Lot.objects.get(pk=pk, is_deleted=False)
        except Exception:
            raise Http404
        if self.lot.auction:
            self.auction = self.lot.auction
        else:
            raise Http404
        self.is_auction_admin
        self.lot_initial_winner = self.lot.auctiontos_winner
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["lot"] = self.lot
        form_kwargs["user"] = self.request.user
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = ""
        context["modal_title"] = f"Edit lot {self.lot.lot_number_display}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if form.has_changed():
                self.lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Edited lot {self.lot.lot_number_display}:",
                    user=self.request.user,
                    form=form,
                )
                # Check if only winner and winning_price were changed
                changed_fields = set(form.changed_data)
                winner_fields = {"auctiontos_winner", "winning_price"}
                if changed_fields and changed_fields.issubset(winner_fields):
                    quick_set_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.auction.slug})
                    messages.info(
                        self.request,
                        format_html(
                            "You're doing things the hard way - <a href='{}'>quick set lot winners</a> page lets you mark lots sold much more quickly.",
                            quick_set_url,
                        ),
                    )
            obj = self.lot
            # obj.custom_lot_number = form.cleaned_data["custom_lot_number"]
            obj.lot_name = form.cleaned_data["lot_name"] or "Unknown lot"
            category = form.cleaned_data["species_category"]
            if not category:
                category = Category.objects.filter(name="Uncategorized").first()
            obj.species_category = category
            obj.summernote_description = form.cleaned_data["summernote_description"]
            # obj.auctiontos_seller = form.cleaned_data['auctiontos_seller'] or request.user
            obj.quantity = form.cleaned_data["quantity"] or 1
            obj.donation = form.cleaned_data["donation"]
            obj.i_bred_this_fish = form.cleaned_data["i_bred_this_fish"]
            obj.reserve_price = form.cleaned_data["reserve_price"]
            obj.buy_now_price = form.cleaned_data["buy_now_price"]
            obj.banned = form.cleaned_data["banned"]
            obj.auctiontos_winner = form.cleaned_data["auctiontos_winner"]
            obj.winning_price = form.cleaned_data["winning_price"]
            obj.custom_checkbox = form.cleaned_data["custom_checkbox"]
            obj.custom_field_1 = form.cleaned_data["custom_field_1"]
            obj.custom_dropdown = form.cleaned_data["custom_dropdown"]
            # need to make sure the winner matches the auctiontos_winner
            if obj.pk and obj.winner:
                if not obj.auctiontos_winner:
                    obj.winner = None
                elif obj.auctiontos_winner.user:
                    obj.winner = obj.auctiontos_winner.user
                # winner not set if auctiontos_winner is set for the first time...don't see a real downside here, winner is generally not set as part of an auction anyway
            obj.save()
            # add message if the winner changed
            if obj.auctiontos_winner:
                if self.lot_initial_winner != obj.auctiontos_winner:
                    try:
                        obj.add_winner_message(self.request.user, obj.auctiontos_winner, obj.winning_price)
                    except Exception:
                        logger.exception("add_winner_message failed for lot %s", obj.pk)
                    if not obj.date_end:
                        obj.date_end = timezone.now()
                        obj.active = False
                        obj.save()
                    if obj.auction and obj.auction.club and not obj.bap_points_awarded and not obj.manually_approved:
                        try:
                            obj.auto_award_bap_points()
                        except Exception:
                            logger.exception("auto_award_bap_points failed for lot %s", obj.pk)
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)


class AuctionTOSDelete(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Delete AuctionTOSs"""

    template_name = "auctions/auctiontos_confirm_delete.html"
    merge_template_name = "auctions/contact_merge.html"
    form_class = DeleteAuctionTOS
    model = AuctionTOS
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        _ = self.can_add_edit_people
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["auctiontos"] = self.auctiontos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auctiontos"] = self.auctiontos
        context["tooltip"] = ""
        context["modal_title"] = f"Delete {self.auctiontos.name}"
        return context

    def _is_merge_action(self):
        return self.request.GET.get("action") == "merge" or self.request.POST.get("action") == "merge"

    def _merge_success_url(self):
        return reverse("auction_tos_list", kwargs={"slug": self.auctiontos.auction.slug})

    def _merge_label(self, auctiontos):
        return f"{auctiontos.name} (bidder #{auctiontos.bidder_number})"

    @staticmethod
    def _is_merge_empty(value):
        return value in (None, "")

    def _get_review_initial(self, source, target, form_class):
        form = form_class(instance=target, auction=self.auction)
        initial = {}
        for field_name in form.fields:
            target_value = getattr(target, field_name, None)
            source_value = getattr(source, field_name, None)
            if self._is_merge_empty(target_value) and not self._is_merge_empty(source_value):
                initial[field_name] = source_value.pk if hasattr(source_value, "pk") else source_value
        return initial

    @staticmethod
    def _format_merge_value(value):
        if value in (None, ""):
            return "—"
        return value

    def _build_merge_rows(self, source, target, form):
        rows = []
        for field_name, field in form.fields.items():
            rows.append(
                {
                    "label": field.label,
                    "source_value": self._format_merge_value(getattr(source, field_name, None)),
                    "target_value": self._format_merge_value(getattr(target, field_name, None)),
                }
            )
        return rows

    def _render_merge_select(self, request, form):
        return render(
            request,
            self.merge_template_name,
            {
                "step": "select",
                "page_title": f"Merge user — {self.auctiontos.name}",
                "heading": "Merge user",
                "subheading": f"Auction: {self.auction}",
                "selection_form": form,
                "source_label": self._merge_label(self.auctiontos),
                "cancel_url": self._merge_success_url(),
                "action_url": request.get_full_path(),
                "action_mode": "merge",
            },
        )

    def _render_merge_review(self, request, target, form):
        return render(
            request,
            self.merge_template_name,
            {
                "step": "review",
                "page_title": f"Merge user — {self.auctiontos.name}",
                "heading": "Merge user",
                "subheading": f"Auction: {self.auction}",
                "source": self.auctiontos,
                "target": target,
                "source_label": self._merge_label(self.auctiontos),
                "target_label": self._merge_label(target),
                "review_form": form,
                "comparison_rows": self._build_merge_rows(self.auctiontos, target, form),
                "summary_lines": [
                    f"{self._merge_label(self.auctiontos)} will be deleted.",
                    f"{self._merge_label(target)} will be kept.",
                    "Won lots, sold lots, invoice adjustments, and payments will move to the kept user.",
                ],
                "target_field_name": "target",
                "cancel_url": self._merge_success_url(),
                "action_url": request.get_full_path(),
                "action_mode": "merge",
                "save_button_label": f"Save and delete {self.auctiontos.name}",
            },
        )

    def _get_merge_target(self, target_pk):
        return get_object_or_404(AuctionTOS, pk=target_pk, auction=self.auction)

    def get(self, request, *args, **kwargs):
        if self._is_merge_action():
            form = AuctionTOSMergeTargetForm(self.auctiontos, self.auction)
            return self._render_merge_select(request, form)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if self._is_merge_action():
            if request.POST.get("step") == "review":
                target = self._get_merge_target(request.POST.get("target"))
                review_form = AuctionTOSMergeReviewForm(request.POST, instance=target, auction=self.auction)
                if review_form.is_valid():
                    with transaction.atomic():
                        target = review_form.save()
                        target.merge_duplicate(
                            self.auctiontos,
                            reason=f"merged by {request.user.username}",
                            user=request.user,
                            preserve_missing_fields=False,
                        )
                    messages.success(request, f"Merged {self.auctiontos.name} into {target.name}.")
                    return redirect(self._merge_success_url())
                return self._render_merge_review(request, target, review_form)
            selection_form = AuctionTOSMergeTargetForm(self.auctiontos, self.auction, request.POST)
            if selection_form.is_valid():
                target = selection_form.cleaned_data["target"]
                review_form = AuctionTOSMergeReviewForm(
                    instance=target,
                    initial=self._get_review_initial(self.auctiontos, target, AuctionTOSMergeReviewForm),
                    auction=self.auction,
                )
                return self._render_merge_review(request, target, review_form)
            return self._render_merge_select(request, selection_form)
        form = self.get_form()
        if form.is_valid():
            success_url = reverse("auction_tos_list", kwargs={"slug": self.auctiontos.auction.slug})
            if form.cleaned_data["delete_lots"]:
                sold_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.auctiontos)
                won_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.auctiontos)
                for lot in sold_lots:
                    lot.delete()
                for lot in won_lots:
                    LotHistory.objects.create(
                        lot=lot,
                        user=request.user,
                        message=f"{request.user.username} has removed {self.auctiontos} from this auction, this lot no longer has a winner.",
                        notification_sent=True,
                        bid_amount=0,
                        changed_price=True,
                        seen=True,
                    )
                    lot.auctiontos_winner = None
                    lot.winning_price = None
                    lot.active = True
                    lot.save()
                self.auction.create_history(
                    applies_to="USERS", action=f"Deleted {self.auctiontos.name}", user=request.user
                )
                self.auctiontos.delete()
            elif form.cleaned_data["merge_with"]:
                new_auctiontos = AuctionTOS.objects.get(pk=form.cleaned_data["merge_with"])
                new_auctiontos.merge_duplicate(
                    self.auctiontos, reason=f"merged by {request.user.username}", user=request.user
                )
            else:
                # No lots to delete and no merge target selected; delete this AuctionTOS
                self.auction.create_history(
                    applies_to="USERS", action=f"Deleted {self.auctiontos.name}", user=request.user
                )
                self.auctiontos.delete()
            return redirect(success_url)
        else:
            return self.form_invalid(form)


class AuctionTOSAdmin(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Creation and management for AuctionTOSs"""

    template_name = "auctions/generic_admin_form.html"
    form_class = CreateEditAuctionTOS
    model = AuctionTOS
    allow_non_admins = True  # we gate via can_add_edit_people for finer control

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop("pk")
        self.is_edit_form = True
        try:
            self.auctiontos = AuctionTOS.objects.get(pk=pk)
        except Exception:
            self.auctiontos = None
        if self.auctiontos:
            self.auction = self.auctiontos.auction
        else:
            try:
                self.auction = Auction.objects.get(slug=pk, is_deleted=False)
                self.is_edit_form = False
            except Auction.DoesNotExist:
                raise Http404
        _ = self.can_add_edit_people  # raises PermissionDenied if not allowed
        if self.auction.is_club_managed:
            # In club-managed mode, member details are edited in the club admin, not here.
            if self.is_edit_form and self.auctiontos and self.auctiontos.clubmember_id:
                target = reverse("clubmember_admin", kwargs={"pk": self.auctiontos.clubmember_id})
                target += f"?tos={self.auctiontos.pk}"
                return redirect(target)
            if not self.is_edit_form:
                # Creating a new user — redirect to club member create form.
                target = reverse("clubmember_create", kwargs={"slug": self.auction.club.slug})
                if self.auction.manage_users_through_club == "checkin":
                    target += f"?auction={self.auction.slug}"
                return redirect(target)
            # Editing an existing TOS that has no club member link (e.g. added before club
            # management was enabled) — fall through and show the regular AuctionTOS form.
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["is_edit_form"] = self.is_edit_form
        form_kwargs["auctiontos"] = self.auctiontos
        # Pre-populate new-user form from GET params (name, email, phone)
        if not self.is_edit_form and self.request.method == "GET":
            prefill = {}
            for field in ("name", "email", "phone"):
                val = self.request.GET.get(field, "").strip()
                if val:
                    prefill[field if field != "phone" else "phone_number"] = val
            if prefill:
                form_kwargs.setdefault("initial", {}).update(prefill)
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.is_edit_form and self.auction.is_online:
            context["tooltip"] = (
                "This is an online auction: users should join through this site. You probably don't want to add them here."
            )
        # context['new_form'] = CreateEditAuctionTOS(
        #     is_edit_form=self.is_edit_form,
        #     auctiontos=self.auctiontos,
        #     auction=self.auction
        # )
        context["unsold_lot_warning"] = ""
        if self.auctiontos:
            try:
                invoice = self.auctiontos.invoice
                _ensure_invoice_renewal_state(invoice)
                invoice_string = invoice.invoice_summary_short
                context["top_buttons"] = render_to_string("invoice_buttons.html", {"invoice": invoice})
                context["unsold_lot_warning"] = invoice.unsold_lot_warning
            except AttributeError:
                invoice = None
                invoice_string = ""
            context["modal_title"] = f"{self.auctiontos.name} {invoice_string}"
        else:
            context["modal_title"] = "Add new user"
        if self.auctiontos:
            context["invoice"] = self.auctiontos.invoice
            context["is_admin"] = True
        # for real time form validation
        extra_script = "<script>"
        if self.auctiontos:
            extra_script += f"var pk={self.auctiontos.pk};"
        else:
            extra_script += "var pk=null;"
        extra_script += f"""var validation_url = '{reverse("auctiontos_validation", kwargs={"slug": self.auction.slug})}';
                            var csrf_token = '{get_token(self.request)}';"""
        extra_script += """

    function setFieldInvalid(fieldId, message, is_invalid) {
        var field = document.getElementById(fieldId);
        if (!field) return;

        var feedbackId = fieldId + "_feedback";
        var feedback = document.getElementById(feedbackId);

        if (is_invalid) {
            field.classList.add("is-invalid");
            var existing_error = document.getElementById( "error_1_"+fieldId);
            if (existing_error) {
                existing_error.remove();
            }
            if (feedback) {
                feedback.remove();
            }
            feedback = document.createElement("div");
            feedback.id = feedbackId;
            feedback.className = "invalid-feedback";
            field.parentNode.appendChild(feedback);

            feedback.textContent = message;
        } else {
            field.classList.remove("is-invalid");
            if (feedback) {
                feedback.remove();
            }
        }
    }

    function showAutocomplete(response, remove) {
        var feedback = document.getElementById('id_name_feedback');
        if (feedback) {
            feedback.remove();
        }
        if (remove) {
            return;
        }
        feedback = document.createElement("div");
        feedback.id = "id_name_feedback";
        feedback.className = "valid-feedback d-block cursor-pointer";
        var buttonText = response.id_email ? "Click to use " + response.id_email : "Click to fill in details";
        feedback.innerHTML = "<button role='button' class='btn btn-sm btn-info' id='autocompleteTosForm'>" + buttonText + "</button>";
        var autocomplete = response;
        document.getElementById('id_name').parentNode.appendChild(feedback);

        //setTimeout(function() {
            var link = document.getElementById('autocompleteTosForm');
            link.addEventListener('click', function(event) {
            event.preventDefault();

            for (var key in autocomplete) {
                console.log(key);
                if (autocomplete.hasOwnProperty(key)) {
                    var element = document.getElementById(key);
                    if (element) {
                        if (element.type !== "checkbox" && element.value === "") {
                            element.value = autocomplete[key] || '';
                        }
                        if (element.type === "checkbox") {
                            element.checked = autocomplete[key] === true;
                        }
                    }
                }
            }

            });
            link.focus();
        //}, 40);

    }

    function hasAutocompleteData(response) {
        return !!(response.id_email || response.id_address || response.id_phone_number || response.id_memo);
    }


    function setFieldNote(fieldId, message) {
        var field = document.getElementById(fieldId);
        if (!field) return;

        var noteId = fieldId + "_note";
        var note = document.getElementById(noteId);
        if (note) {
            note.remove();
        }

        if (!message) {
            return;
        }

        note = document.createElement("div");
        note.id = noteId;
        note.className = "text-warning small mt-1";
        note.textContent = message;
        field.parentNode.appendChild(note);
    }

    function validateField() {
        var data = {
            pk: pk,
            name: $("#id_name").val(),
            bidder_number: $("#id_bidder_number").val(),
            email: $("#id_email").val(),
        };

        $.ajax({
            url: validation_url,
            type: "POST",
            data: data,
            headers: { "X-CSRFToken": csrf_token },
            success: function (response) {
                if (response.name_tooltip) {
                    setFieldNote("id_name", response.name_tooltip);
                    showAutocomplete(response, true)
                } else if (hasAutocompleteData(response)) {
                    setFieldNote("id_name", "");
                    showAutocomplete(response)
                } else {
                    setFieldNote("id_name", "");
                    showAutocomplete(response, true)
                }
                if (response.email_tooltip) {
                    setFieldInvalid("id_email", response.email_tooltip, true);
                } else {
                    setFieldInvalid("id_email", response.email_tooltip, false);
                }
                if (response.bidder_number_tooltip) {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, true);
                } else {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, false);
                }
            }
        });
    }

    $("#id_bidder_number, #id_name, #id_email").on("blur", validateField);
        </script>"""
        context["extra_script"] = mark_safe(extra_script)
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if self.auctiontos:
                obj = self.auctiontos
                if form.has_changed():
                    self.auction.create_history(
                        applies_to="USERS",
                        action=f"Edited {obj.name}: ",
                        user=request.user,
                        form=form,
                    )
            else:
                obj = AuctionTOS.objects.create(
                    auction=self.auction,
                    pickup_location=form.cleaned_data["pickup_location"],
                    manually_added=True,
                )
                self.auction.create_history(
                    applies_to="USERS",
                    action=f"Added {form.cleaned_data['name']}",
                    user=request.user,
                )
            obj.bidder_number = form.cleaned_data["bidder_number"]
            obj.pickup_location = form.cleaned_data["pickup_location"]
            obj.name = form.cleaned_data["name"]
            obj.email = form.cleaned_data["email"]
            obj.phone_number = form.cleaned_data["phone_number"]
            obj.address = form.cleaned_data["address"]
            obj.is_admin = form.cleaned_data["is_admin"]
            obj.bidding_allowed = form.cleaned_data["bidding_allowed"]
            obj.selling_allowed = form.cleaned_data["selling_allowed"]
            obj.is_club_member = form.cleaned_data["is_club_member"]
            obj.memo = form.cleaned_data["memo"]
            obj.save()
            return close_modal_response("reload-page")
        else:
            name = form.cleaned_data.get("name")
            if not name:
                self.get_form().add_error("name", "Name is required")
            return self.form_invalid(form)


class AuctionConfirmView(LoginRequiredMixin, TemplateView):
    """
    Confirmation page for auction creation - allows user to choose between creating a club auction or selling a single item
    """

    template_name = "auction_confirm.html"

    def dispatch(self, request, *args, **kwargs):
        # Check if user has permission to create auctions
        auction_creation_allowed = False
        if self.request.user.is_authenticated and self.request.user.userdata.can_create_club_auctions:
            auction_creation_allowed = True
        if self.request.user.is_superuser:
            auction_creation_allowed = True
        if not auction_creation_allowed:
            # If user can't create auctions, redirect them directly to selling
            return redirect("selling")
        return super().dispatch(request, *args, **kwargs)


def _add_club_admins_as_auction_tos(auction, requesting_user):
    """Create AuctionTOS admin records for club members with admin/manage_auctions permissions.

    Only runs when the auction has a club and at least one pickup location.
    Skips the requesting user (already an admin as the auction creator).
    """
    if not auction.club:
        return
    default_location = auction.location_qs.first()
    if not default_location:
        return
    manage_auctions_members = (
        ClubMember.objects.filter(
            club=auction.club,
            is_deleted=False,
        )
        .filter(Q(permission_manage_auctions=True) | Q(permission_admin=True))
        .exclude(user=requesting_user)
        .distinct()
    )
    for member in manage_auctions_members:
        existing_tos = None
        if member.user:
            existing_tos = AuctionTOS.objects.filter(auction=auction, user=member.user).first()
        if not existing_tos and member.email:
            existing_tos = AuctionTOS.objects.filter(auction=auction, email=member.email).first()
        if not existing_tos:
            AuctionTOS.objects.create(
                auction=auction,
                user=member.user,
                pickup_location=default_location,
                name=member.display_name,
                email=member.email or "",
                phone_number=member.phone_number or "",
                address=member.address or "",
                is_admin=True,
                manually_added=True,
            )
            auction.create_history(
                applies_to="USERS",
                action=f"Automatically added {member.display_name} as auction admin because of their club role in '{auction.club}'.",
                user=None,
            )


class AuctionCreateView(CreateView, LoginRequiredMixin):
    """
    Creating a new auction
    """

    model = Auction
    template_name = "auction_create_form.html"
    form_class = CreateAuctionForm
    redirect_url = None  # really only used if this is a cloned auction
    cloned_from = None

    def dispatch(self, request, *args, **kwargs):
        original_dispatch = super().dispatch(request, *args, **kwargs)
        auction_creation_allowed = False
        if self.request.user.is_authenticated and self.request.user.userdata.can_create_club_auctions:
            auction_creation_allowed = True
        if self.request.user.is_superuser:
            auction_creation_allowed = True
        if not auction_creation_allowed:
            return redirect(reverse("home"))
        return original_dispatch

    def get_success_url(self):
        if self.redirect_url:
            return self.redirect_url
        else:
            messages.success(
                self.request,
                "Auction created!  Now, create a location to exchange lots.",
            )
            return reverse("create_auction_pickup_location", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "New auction"
        context["new"] = True
        userData = self.request.user.userdata
        # a bit of logic used on auction_create_form.html to suggest auction names
        context["club"] = ""
        club = userData.club
        if club:
            context["club"] = str(club)
            if club.abbreviation:
                context["club"] = club.abbreviation
        if settings.ENABLE_CLUB_FINDER and not club:
            context["show_club_tip"] = True
        return context

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["user"] = self.request.user
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        data = self.request.GET.copy()
        self.cloned_from = data.get("copy", None)
        kwargs["cloned_from"] = self.cloned_from
        return kwargs

    def form_valid(self, form, **kwargs):
        """Rules for new auction creation"""
        auction = form.save(commit=False)
        auction.created_by = self.request.user
        auction.promote_this_auction = False  # all auctions start not promoted
        cloned_from = form.cleaned_data["cloned_from"]
        auction.date_start = form.cleaned_data["date_start"]
        is_online = True
        clone_from_auction = None
        if "clone" in str(self.request.GET):
            try:
                original_auction = Auction.objects.get(slug=cloned_from, is_deleted=False)
                if original_auction:
                    # you still don't get to clone auctions that aren't yours...
                    if original_auction.permission_check(self.request.user):
                        clone_from_auction = original_auction
            except Exception as e:
                logger.exception(e)
                pass
        elif "online" in str(self.request.GET):
            is_online = True
        else:
            is_online = False
        run_duration = timezone.timedelta(days=7)  # only set for is_online
        online_bidding_start_diff = timezone.timedelta(days=7)
        online_bidding_end_diff = timezone.timedelta(minutes=0)
        lot_submission_end_date_diff = timezone.timedelta(minutes=0)
        if clone_from_auction:
            fields_to_clone = [
                "is_online",
                "summernote_description",
                "lot_entry_fee",
                "unsold_lot_fee",
                "winning_bid_percent_to_club",
                "first_bid_payout",
                "club_member_discount",
                "sealed_bid",
                "max_lots_per_user",
                "allow_additional_lots_as_donation",
                "make_stats_public",
                "use_categories",
                "bump_cost",
                "is_chat_allowed",
                "lot_promotion_cost",
                "code_to_add_lots",
                "online_bidding",
                "pre_register_lot_discount_percent",
                "only_approved_sellers",
                "only_approved_bidders",
                "email_users_when_invoices_ready",
                "invoice_payment_instructions",
                "minimum_bid",
                "winning_bid_percent_to_club_for_club_members",
                "lot_entry_fee_for_club_members",
                "set_lot_winners_url",
                "require_phone_number",
                "buy_now",
                "reserve_price",
                "tax",
                "advanced_lot_adding",
                "date_online_bidding_starts",
                "date_online_bidding_ends",
                "allow_deleting_bids",
                "auto_add_images",
                "message_users_when_lots_sell",
                "label_print_fields",
                "force_donation_threshold",
                "use_quantity_field",
                "custom_checkbox_name",
                "custom_field_1",
                "custom_field_1_name",
                "use_custom_dropdown_field",
                "custom_dropdown_name",
                "allow_bulk_adding_lots",
                "copy_users_when_copying_this_auction",
                "use_donation_field",
                "use_i_bred_this_fish_field",
                "use_seller_dash_lot_numbering",
                "enable_online_payments",
                "enable_square_payments",
                "add_membership_fee_to_invoices_for_expired_members",
                "alternate_split_mode",
                "alternative_split_label",
                "google_drive_link",
                "only_whole_dollar_bids",
                "club",
                "manage_users_through_club",
            ]
            for field in fields_to_clone:
                setattr(auction, field, getattr(original_auction, field))
            if original_auction.date_end:
                run_duration = original_auction.date_end - original_auction.date_start
            if original_auction.date_online_bidding_starts:
                online_bidding_start_diff = original_auction.date_start - original_auction.date_online_bidding_starts
            if original_auction.date_online_bidding_ends:
                online_bidding_end_diff = original_auction.date_start - original_auction.date_online_bidding_ends
            if original_auction.lot_submission_end_date:
                lot_submission_end_date_diff = original_auction.date_start - original_auction.lot_submission_end_date
            auction.cloned_from = original_auction
        else:
            auction.is_online = is_online
            # The model default ("custom") preserves behavior for pre-existing and cloned
            # auctions; brand-new auctions start with the alternate split off.
            auction.alternate_split_mode = "off"
            if not is_online:
                # override default settings for new in-person auctions
                auction.online_bidding = "disable"
                auction.buy_now = "disable"
                auction.reserve_price = "disable"
            else:
                # override default settings for new online auctions
                auction.use_quantity_field = True
        if not auction.summernote_description:
            auction.summernote_description = """
            <h4>General information</h4>
            You should remove this line and edit this section to suit your auction.
            Use the formatting here as an example.<br><br>
            <h4>Rules</h4>
            <ul><li>You cannot sell anything banned by state law.</li>
            <li>All lots must be properly bagged.  No leaking bags!</li>
            <li>You do not need to be a club member to buy or sell lots.</li></ul>"""
        if auction.is_online:
            auction.date_end = auction.date_start + run_duration
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_end
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start
        else:
            auction.date_end = None
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_start - lot_submission_end_date_diff
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start - run_duration
            if not auction.date_online_bidding_starts:
                auction.date_online_bidding_starts = auction.date_start - online_bidding_start_diff
            if not auction.date_online_bidding_ends:
                auction.date_online_bidding_ends = auction.date_start - online_bidding_end_diff
        auction.save()
        # let's route in-person auctions to the rule page next
        if not auction.is_online and not clone_from_auction:
            self.redirect_url = auction.get_edit_url()
            # Create a default pickup location.  This is handled better in models.auction.save()
            # PickupLocation.objects.create(
            #     name=str(auction),
            #     auction=auction,
            #     is_default=True,
            #     user=self.request.user)
        if clone_from_auction:
            # because we will almost certainly have locations, we can simply default to the main auction page
            self.redirect_url = auction.get_absolute_url()
            originalLocations = PickupLocation.objects.filter(auction=clone_from_auction)
            for location in originalLocations:
                location.pk = None  # duplicate all fields
                if location.name == str(clone_from_auction):
                    location.name = str(auction)
                location.auction = auction
                auction_time = clone_from_auction.date_start
                if clone_from_auction.date_end:
                    auction_time = clone_from_auction.date_end
                if location.pickup_time:
                    firstTimeDiff = location.pickup_time - auction_time
                    if auction.date_end:
                        location.pickup_time = auction.date_end + firstTimeDiff
                    else:
                        location.pickup_time = auction.date_start + firstTimeDiff
                if location.second_pickup_time:
                    secondTimeDiff = location.second_pickup_time - auction_time
                    if auction.date_end:
                        location.second_pickup_time = auction.date_end + secondTimeDiff
                    else:
                        location.second_pickup_time = auction.date_start + secondTimeDiff
                location.save()
            # copy any auctiontos, if appropriate
            if clone_from_auction.copy_users_when_copying_this_auction:
                auctiontos = AuctionTOS.objects.filter(auction=clone_from_auction)
                for tos in auctiontos:
                    # in tos.save(), bid permissions are reset if there's no pk
                    # to preserve them, we store them here, then resave again once the new instance is created
                    original_bid_permission = tos.bidding_allowed
                    tos.pk = None
                    tos.createdon = None
                    tos.auction = auction
                    # tos.email_address_status = "UNKNOWN"
                    tos.manually_added = True
                    tos.print_reminder_email_sent = False
                    if tos.pickup_location.name == str(clone_from_auction):
                        new_location_name = str(auction)
                    else:
                        new_location_name = tos.pickup_location.name
                    new_location = PickupLocation.objects.filter(auction=auction, name=new_location_name).first()
                    if new_location:
                        tos.pickup_location = new_location
                        tos.save()
                        tos.bidding_allowed = original_bid_permission
                        tos.save()  # see comment above
            original_dropdown_options = AuctionDropdown.objects.filter(auction=clone_from_auction)
            for dropdown_option in original_dropdown_options:
                AuctionDropdown.objects.create(
                    auction=auction,
                    user=dropdown_option.user,
                    value=dropdown_option.value,
                )
        action = "Created auction"
        if clone_from_auction:
            action += f" by copying {clone_from_auction}"
        auction.create_history(
            applies_to="RULES",
            action=action,
            user=self.request.user,
        )
        # Associate auction with the creator's club if they have admin or manage_auctions permission
        if not auction.club:
            creator_userdata = self.request.user.userdata
            creator_club = creator_userdata.club
            if creator_club and (
                check_club_permission(self.request.user, creator_club, "permission_admin")
                or check_club_permission(self.request.user, creator_club, "permission_manage_auctions")
            ):
                auction.club = creator_club
                auction.save(update_fields=["club"])
                auction.create_history(
                    applies_to="RULES",
                    action=f"Automatically associated with club '{creator_club}' based on auction creator's preferences.",
                    user=None,
                )
        self.request.user.userdata.last_auction_used = auction
        self.request.user.userdata.save(update_fields=["last_auction_used"])
        # Add club admin members as AuctionTOS admins (works for copied auctions with locations,
        # and for new auctions once a pickup location exists — also called from PickupLocationsCreate)
        _add_club_admins_as_auction_tos(auction, self.request.user)
        return super().form_valid(form)


class AuctionInfo(FormMixin, DetailView, AuctionViewMixin):
    """Main view of a single auction"""

    template_name = "auction.html"
    model = Auction
    form_class = AuctionJoin
    rewrite_url = None
    auction = None
    allow_non_admins = True

    def get(self, request, *args, **kwargs):
        if self.is_auction_admin:
            if str(request.GET.get("dismissed_promo_banner", "")).lower() in ("1", "true"):
                self.auction.dismissed_promo_banner = True
                self.auction.save()
            if str(request.GET.get("make_current_auction", "")).lower() in ("1", "true") and self.auction.club_id:
                club = self.auction.club
                club.current_auction = self.auction
                club.save(update_fields=["current_auction"])
                messages.success(request, f"This is now the current auction for {club.name}.")
                return redirect("auction_main", slug=self.auction.slug)
            if request.user.is_superuser:
                if str(request.GET.get("trust_user", "")).lower() in ("1", "true"):
                    self.auction.created_by.userdata.is_trusted = True
                    self.auction.created_by.userdata.save()
                    messages.success(request, f"{self.auction.created_by.username} is now trusted")
                if str(request.GET.get("make_club_admin", "")).lower() in ("1", "true"):
                    creator = self.auction.created_by
                    creator_club = getattr(creator.userdata, "club", None)
                    if creator_club:
                        # Fill contact info from user account; get_or_create won't duplicate.
                        member = ClubMember.objects.filter(club=creator_club, user=creator).first()
                        if not member:
                            member = ClubMember(
                                club=creator_club,
                                user=creator,
                                source="manually_added",
                            )
                        # Always populate contact fields from user data (safe to overwrite blanks).
                        member.name = member.name or creator.get_full_name() or creator.username
                        if not member.email:
                            member.email = creator.email or None
                        if not member.phone_number:
                            member.phone_number = getattr(creator.userdata, "phone_number", None) or None
                        if not member.address:
                            member.address = getattr(creator.userdata, "address", None) or ""
                        # Count the creator's clubless auctions before saving: granting
                        # permission_admin fires the on_club_member_saved signal, which associates
                        # those auctions with the club and books their club ledger. Capturing the
                        # count first keeps the success message accurate.
                        assigned_count = Auction.objects.filter(
                            created_by=creator, club__isnull=True, is_deleted=False
                        ).count()
                        member.permission_admin = True
                        member.save()
                        ClubHistory.objects.create(
                            club=creator_club,
                            user=request.user,
                            action=f"Granted admin permissions to {creator.get_full_name() or creator.username} via auction admin panel"
                            + (f"; assigned {assigned_count} auction(s) to club" if assigned_count else ""),
                            applies_to="MEMBERS",
                        )
                        messages.success(
                            request,
                            f"{creator.username} is now an admin of {creator_club.name}"
                            + (f" and {assigned_count} auction(s) assigned to club" if assigned_count else ""),
                        )
            if self.auction.created_by.pk == request.user.pk:
                if str(request.GET.get("enable_online_payments", "")).lower() in ("1", "true"):
                    self.auction.enable_online_payments = True
                    self.auction.save()
                if str(request.GET.get("enable_square_payments", "")).lower() in ("1", "true"):
                    self.auction.enable_square_payments = True
                    self.auction.save()
                if str(request.GET.get("dismissed_paypal_banner", "")).lower() in ("1", "true"):
                    self.auction.dismissed_paypal_banner = True
                    self.auction.save()
                if str(request.GET.get("dismissed_square_banner", "")).lower() in ("1", "true"):
                    self.auction.dismissed_square_banner = True
                    self.auction.save()
                if str(request.GET.get("never_show_paypal_connect", "")).lower() in ("1", "true"):
                    messages.info(
                        request,
                        "You won't see the PayPal connection prompt again.  You can always enable PayPal under Preferences>More>Connect your PayPal account.",
                    )
                    request.user.userdata.never_show_paypal_connect = True
                    request.user.userdata.save()
                if str(request.GET.get("never_show_square_connect", "")).lower() in ("1", "true"):
                    messages.info(
                        request,
                        "You won't see the Square connection prompt again.  You can always enable Square under Preferences>More>Connect your Square account.",
                    )
                    request.user.userdata.never_show_square_connect = True
                    request.user.userdata.save()
        return super().get(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        if self.auction:
            self.object = self.auction
        else:
            try:
                auction = Auction.objects.get(slug=self.kwargs.get(self.slug_url_kwarg), is_deleted=False)
                self.auction = auction
                self.object = self.auction
            except Auction.DoesNotExist:
                msg = "No auctions found matching the query"
                raise Http404(msg)
        return self.object

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            if not data["next"]:
                data["next"] = self.auction.view_lot_link
            return data["next"]
        except Exception:
            return self.auction.view_lot_link

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["user"] = self.request.user
        form_kwargs["auction"] = self.auction
        form_kwargs["next_url"] = self.request.GET.get("next")
        return form_kwargs

    def dispatch(self, request, *args, **kwargs):
        if self.get_object().permission_check(request.user):
            locations = self.get_object().location_qs.count()
            if not locations:
                messages.info(
                    self.request,
                    "You haven't added any pickup locations to this auction yet. <a href='/locations/new/'>Add one now</a>",
                )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pickup_locations"] = self.auction.location_qs
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        # Offer "make this the current club auction" to admins when the auction has a club
        # and isn't already that club's current auction.
        context["can_make_current_auction"] = bool(
            self.auction.club_id and self.is_auction_admin and self.auction.club.current_auction_id != self.auction.pk
        )
        # Show "make club admin" button to superusers when auction creator has a club in their profile.
        # Show when: creator isn't yet an admin of that club, OR this auction has no club assigned yet.
        if self.request.user.is_superuser and self.auction.created_by:
            creator_club = getattr(self.auction.created_by.userdata, "club", None)
            if creator_club:
                creator_is_admin = creator_club.members.filter(
                    user=self.auction.created_by, permission_admin=True, is_deleted=False
                ).exists()
                auction_needs_club = not self.auction.club
                if not creator_is_admin or auction_needs_club:
                    context["can_make_club_admin"] = True
                    context["creator_club"] = creator_club
        if self.auction.closed:
            context["ended"] = True
            messages.info(
                self.request,
                f"This auction has ended.  You can't bid on anything, but you can still <a href='{self.auction.view_lot_link}'>view lots</a>.",
            )
        else:
            context["ended"] = False

        # Initialize existingTos and i_agree for form
        existingTos = None
        i_agree = False
        existing_club_member = None

        if self.request.user.is_authenticated:
            tos = AuctionTOS.objects.filter(user=self.request.user, auction=self.auction).first()
            existing_club_member = _find_club_member(self.auction.club, self.request.user, self.request.user.email)
            if tos:
                existingTos = tos.pickup_location
                i_agree = True
                context["hasChosenLocation"] = existingTos.pk if existingTos else False
            else:
                context["hasChosenLocation"] = False
                if self.auction.multi_location:
                    i_agree = True
                else:
                    existingTos = PickupLocation.objects.filter(auction=self.auction).first()
        else:
            context["hasChosenLocation"] = False
            if self.auction.multi_location:
                i_agree = True
            else:
                existingTos = PickupLocation.objects.filter(auction=self.auction).first()
        context["show_club_join_message"] = bool(
            self.auction.is_club_managed and self.auction.club and not existing_club_member
        )
        context["active_tab"] = "main"
        # Check if user has lots in this auction
        if self.request.user.is_authenticated:
            context["user_has_lots"] = (
                Lot.objects.exclude(is_deleted=True)
                .filter(auction=self.auction, auctiontos_seller__user=self.request.user)
                .exists()
            )
        else:
            context["user_has_lots"] = False
        if self.request.user.is_authenticated and self.request.user.pk == self.auction.created_by.pk:
            invalidPickups = self.auction.pickup_locations_before_end
            if invalidPickups:
                messages.info(
                    self.request,
                    f"<a href='{invalidPickups}'>Some pickup times</a> are set before the end date of the auction",
                )
            nonLogicalTimes = self.auction.has_non_logical_times
            if nonLogicalTimes:
                messages.info(
                    self.request,
                    f"<a href='{nonLogicalTimes}'>Auction start or end time</a> should be set to a logical time like 14:30 or 09:00",
                )
            if self.auction.time_start_is_at_night and not self.auction.is_online:
                messages.info(
                    self.request,
                    f"You know your auction is starting in the middle of the night, right? <a href='{reverse('edit_auction', kwargs={'slug': self.auction.slug})}'>Click here to change when bidding opens</a> and remember that it's in 24 hour time",
                )

        context["form"] = AuctionJoin(
            user=self.request.user,
            auction=self.auction,
            next_url=self.request.GET.get("next"),
            initial={
                "user": getattr(self.request.user, "id", None),
                "auction": self.auction.pk,
                "pickup_location": existingTos,
                "i_agree": i_agree,
            },
        )
        context["rewrite_url"] = self.rewrite_url
        # Email button: shown to authenticated users when the auction belongs to a club
        if self.auction.club:
            from .email_routing import email_routing_enabled

            if email_routing_enabled():
                context["auction_contact_email"] = self.auction.sender_email
            else:
                context["auction_contact_email"] = self.auction.club.contact_email or None
        else:
            context["auction_contact_email"] = None
        return context

    def post(self, request, *args, **kwargs):
        auction = self.auction
        form = self.get_form()
        if request.user.is_authenticated and form.is_valid():
            userData = self.request.user.userdata
            if auction.require_phone_number and not userData.phone_number:
                messages.error(
                    self.request,
                    "This auction requires a phone number before you can join",
                )
                return redirect(f"{reverse('contact_info')}?next={auction.get_absolute_url()}")
            find_by_email = AuctionTOS.objects.filter(
                email=self.request.user.email,
                auction=auction,
                # manually_added=True,
                # user__isnull=True
            ).first()
            is_new_join = False
            if find_by_email:
                # Check if the user already has a separate TOS (from a prior join by user FK)
                existing_by_user = (
                    AuctionTOS.objects.filter(user=self.request.user, auction=auction)
                    .exclude(pk=find_by_email.pk)
                    .first()
                )
                if existing_by_user:
                    # Keep the oldest record as canonical
                    if (
                        find_by_email.createdon
                        and existing_by_user.createdon
                        and find_by_email.createdon < existing_by_user.createdon
                    ):
                        canonical, duplicate = find_by_email, existing_by_user
                    else:
                        canonical, duplicate = existing_by_user, find_by_email
                    canonical.merge_duplicate(duplicate, reason="duplicate detected on join")
                    obj = canonical
                else:
                    obj = find_by_email
                    obj.user = self.request.user
            else:
                obj, created = AuctionTOS.objects.get_or_create(
                    user=self.request.user,
                    auction=auction,
                    defaults={
                        "pickup_location": form.cleaned_data["pickup_location"],
                        # Seed the email on creation so the record never takes the None->email
                        # transition that used to trip AuctionTOS.save()'s email-change guard and
                        # clear this freshly-linked user. `or None` (not "") keeps the "no email"
                        # admin filter working, which relies on email__isnull.
                        "email": self.request.user.email or None,
                    },
                )
                is_new_join = created
            obj.pickup_location = form.cleaned_data["pickup_location"]
            # check if mail was chosen
            if obj.pickup_location.pickup_by_mail:
                if not userData.address:
                    messages.error(
                        self.request,
                        "You have to set your address before you can choose pickup by mail",
                    )
                    return redirect(f"{reverse('contact_info')}?next={auction.get_absolute_url()}")
            if form.cleaned_data["time_spent_reading_rules"] > obj.time_spent_reading_rules:
                obj.time_spent_reading_rules = form.cleaned_data["time_spent_reading_rules"]
            # even if an auctiontos was originally manually added, if the user clicked join, mark them as not manually added
            obj.manually_added = False
            if obj.email_address_status == "UNKNOWN":
                # if it bounced in the past, the user may have a full inbox or something
                obj.email_address_status = "VALID"
            # fill out some information in the tos if not already filled out
            if not obj.name or obj.name == "Unknown":
                obj.name = self.request.user.first_name + " " + self.request.user.last_name
            if not obj.email:
                obj.email = self.request.user.email
            if not obj.phone_number:
                obj.phone_number = userData.phone_number
            if not obj.address:
                obj.address = userData.address
            if auction.is_club_managed:
                club_member = _find_club_member(auction.club, user=self.request.user, email=obj.email)
                club_member_is_new = False
                if not club_member:
                    club_member = ClubMember(
                        club=auction.club,
                        user=self.request.user,
                        name=obj.name or self.request.user.get_full_name() or self.request.user.username,
                        email=obj.email or self.request.user.email,
                        phone_number=obj.phone_number or "",
                        address=obj.address or "",
                        source=str(auction.title)[:200],
                        added_by=self.request.user,
                    )
                    if auction.only_approved_sellers:
                        club_member.selling_allowed = False
                    if auction.only_approved_bidders:
                        club_member.bidding_allowed = False
                    club_member.save()
                    club_member_is_new = True
                elif not club_member.user_id:
                    club_member.user = self.request.user
                    club_member.save(update_fields=["user"])
                if not club_member.bidder_number:
                    club_member.generate_bidder_number(save=True)
                obj.clubmember = club_member
                obj.bidder_number = club_member.bidder_number
                obj.bidding_allowed = club_member.bidding_allowed
                obj.selling_allowed = club_member.selling_allowed
                if club_member_is_new:
                    from .models import ClubHistory

                    ClubHistory.objects.create(
                        club=auction.club,
                        user=self.request.user,
                        applies_to="MEMBERS",
                        action=f"{club_member.name} joined via auction '{auction.title}'",
                    )
            obj.save()
            # also update userdata to reflect the last auction
            userData.last_auction_used = auction
            userData.last_activity = timezone.now()
            userData.save()
            if auction.is_club_managed and obj.clubmember_id:
                obj.clubmember.update_last_club_activity()
            # Only create history if this is a new join
            if is_new_join:
                auction.create_history(
                    applies_to="USERS",
                    action=f"{obj.name} has joined this auction",
                    user=self.request.user,
                )
            return self.form_valid(form)
        else:
            logger.debug(form.cleaned_data)
            return self.form_invalid(form)


class FAQ(AdminEmailMixin, ListView):
    """Show all questions"""

    model = FAQ
    template_name = "faq.html"
    ordering = ["category_text"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["hide_google_login"] = True
        return context


class PromoSite(TemplateView):
    template_name = "promo.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_PROMO_PAGE:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        context["online_tutorial"] = settings.ONLINE_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial"] = settings.IN_PERSON_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial_chapters"] = settings.IN_PERSON_TUTORIAL_CHAPTERS
        context["online_tutorial_chapters"] = settings.ONLINE_TUTORIAL_CHAPTERS
        return context


class ToDefaultLandingPage(View):
    """
    Allow the user to pick up where they left off
    """

    def tos_check(self, request, auction, routeByLastAuction):
        if not auction:
            if request.user.is_authenticated:
                return AllLots.as_view()(request)
            else:
                if settings.ENABLE_PROMO_PAGE:
                    return PromoSite.as_view()(request)
                else:
                    return AllAuctions.as_view()(request)
        # Only check TOS if authenticated
        if request.user.is_authenticated and AuctionTOS.objects.filter(user=request.user, auction=auction).exists():
            return AllLots.as_view(
                rewrite_url=f"/?{auction.slug}",
                auction=auction,
                routeByLastAuction=routeByLastAuction,
            )(request)
        # Anonymous or not joined – send to auction info page
        return AuctionInfo.as_view(rewrite_url=f"/?{auction.slug}", auction=auction)(request)

    def get(self, request, *args, **kwargs):
        data = request.GET.copy()
        routeByLastAuction = False
        if request.user.is_authenticated:
            try:
                userData = request.user.userdata
                userData.last_activity = timezone.now()
                userData.save()
            except AttributeError:
                # probably not signed in
                pass
        try:
            # if the slug was set in the URL
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=list(data.keys())[0])[0]
            # return tos_check(request, auction, routeByLastAuction)
        except Exception:
            # if not, check and see if the user has been participating in an auction
            try:
                auction = UserData.objects.get(user=request.user).last_auction_used
                # Admins of an in-person auction land on the users list, not the lot list — but only
                # while the auction is still current. Once it's pretty_much_over (wound down 24h+),
                # that redirect is stale, so fall through to the invoice/browse path instead.
                if (
                    auction
                    and not auction.is_online
                    and not auction.pretty_much_over
                    and auction.permission_check(request.user)
                ):
                    return redirect(auction.user_admin_link)
                invoice = (
                    Invoice.objects.filter(auctiontos_user__user=request.user, auctiontos_user__auction=auction)
                    .exclude(status="DRAFT")
                    .first()
                )
                if invoice:
                    messages.info(
                        request,
                        f'{auction} has ended.  <a href="{reverse("invoice_by_pk", kwargs={"pk": invoice.pk})}">View your invoice</a> or <a href="{reverse("feedback")}">leave feedback</a> on lots you bought or sold',
                    )
                    return redirect(reverse("allLots"))
                else:
                    # in progress online auctions get routed
                    if AuctionTOS.objects.filter(user=request.user, auction=auction, auction__is_online=True).exists():
                        # only show the banner if the TOS is signed
                        # messages.add_message(request, messages.INFO, f'{auction} is the last auction you joined.  <a href="/lots/">View all lots instead</a>')
                        routeByLastAuction = True
            except (TypeError, AttributeError, Auction.DoesNotExist):
                # probably no userdata or userdata.auction is None
                auction = None
        return self.tos_check(request, auction, routeByLastAuction)


class MyAccount(LoginRequiredMixin, RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        return reverse("userpage", kwargs={"slug": self.request.user.username})


class AllAuctions(LocationMixin, HTMxTableView):
    model = Auction
    no_location_message = "Set your location to see how far away auctions are"
    table_class = AuctionHTMxTable
    filterset_class = AuctionFilter
    template_name = "all_auctions.html"
    htmx_table_header_template = "auctions/partials/all_auctions_table_header.html"
    # paginate_by = 100

    def get_queryset(self):
        last_auction_pk = -1
        if self.request.user.is_authenticated and self.request.user.userdata.last_auction_used:
            last_auction_pk = self.request.user.userdata.last_auction_used.pk
        qs = (
            Auction.objects.all()
            .annotate(
                is_last_used=Case(
                    When(pk=last_auction_pk, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )
            .order_by("-is_last_used", "-date_start")
        )
        next_90_days = timezone.now() + timedelta(days=90)
        two_years_ago = timezone.now() - timedelta(days=365 * 2)
        standard_filter = Q(
            promote_this_auction=True,
            date_start__lte=next_90_days,
            date_posted__gte=two_years_ago,
        )
        latitude, longitude = self.get_coordinates()
        self._user_has_location = bool(latitude and longitude)
        if latitude and longitude:
            qs = qs.annotate(distance=Auction.get_closest_location_distance_subquery(latitude, longitude))
        else:
            qs = qs.annotate(distance=Value(0, output_field=FloatField()))
        if not self.request.user.is_authenticated:
            qs = qs.exclude(is_deleted=True)
            return qs.filter(standard_filter).annotate(joined=Value(0, output_field=FloatField())).distinct()
        if self.request.user.is_superuser:
            # joined is disabled for admins because we need to return before filtering non-promoted auctions
            return qs.annotate(joined=Value(0, output_field=FloatField())).order_by("-date_posted").distinct()
        qs = qs.exclude(is_deleted=True)
        joined_subquery = Exists(
            AuctionTOS.objects.filter(
                Q(user=self.request.user) | Q(email=self.request.user.email),
                auction=OuterRef("pk"),
            )
        )
        qs = (
            qs.filter(
                Q(auctiontos__user=self.request.user)
                | Q(auctiontos__email=self.request.user.email)
                | Q(created_by=self.request.user)
                | standard_filter
            )
            .annotate(joined=joined_subquery)
            .distinct()
        )
        # Apply nearby filter if user has a location set, the preference is enabled, and nearby=false is not in GET params
        self.nearby_filter_active = False
        userdata = self.request.user.userdata
        self._base_qs = qs  # save pre-filter qs for auto-remove fallback
        if latitude and longitude and userdata.show_nearby_auctions and self.request.GET.get("nearby") != "false":
            online_distance = userdata.email_me_about_new_auctions_distance or 100
            in_person_distance = userdata.email_me_about_new_in_person_auctions_distance or 100
            qs = qs.annotate(
                preferred_distance=Case(
                    When(is_online=True, then=Value(online_distance)),
                    default=Value(in_person_distance),
                    output_field=FloatField(),
                )
            )
            nearby_filter = Q(joined=True) | Q(created_by=self.request.user) | Q(distance__lte=F("preferred_distance"))
            qs = qs.filter(nearby_filter)
            self.nearby_filter_active = True
        return qs

    def get_context_data(self, **kwargs):
        # Auto-remove nearby filter when no results exist but the search term has results without distance constraint
        nearby_filter_auto_removed = None
        if getattr(self, "nearby_filter_active", False) and not self.object_list.exists():
            query = self.request.GET.get("query", "")
            if query:
                base_qs = getattr(self, "_base_qs", None)
                if base_qs is not None:
                    fallback_qs = AuctionFilter({"query": query}, queryset=base_qs).qs
                    if fallback_qs.exists():
                        self.object_list = fallback_qs
                        self.nearby_filter_active = False
                        nearby_filter_auto_removed = "No nearby auctions match your search \u2014 showing all results."
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        if not self.object_list.exists():
            context["no_results"] = (
                f"<span class='text-danger'>No auctions found.</span>  This only searches club auctions, if you're looking for {settings.WEBSITE_FOCUS} to buy, check out <a href='/lots/'>the list of lots for sale</a>"
            )
        context["nearby_filter_auto_removed"] = nearby_filter_auto_removed
        context["is_htmx"] = bool(self.request.headers.get("HX-Request"))
        context["show_new_auction_button"] = True
        if self.request.user.is_authenticated and not self.request.user.userdata.can_create_club_auctions:
            context["show_new_auction_button"] = False
        if not self.request.user.is_authenticated and not settings.ALLOW_USERS_TO_CREATE_AUCTIONS:
            context["show_new_auction_button"] = False
        if self.request.user.is_superuser:
            context["show_new_auction_button"] = True
        context["nearby_filter_active"] = getattr(self, "nearby_filter_active", False)
        user_has_location = getattr(self, "_user_has_location", False)
        context["user_has_location"] = user_has_location
        if user_has_location and self.request.user.is_authenticated:
            try:
                ud = self.request.user.userdata
                unit = ud.distance_unit or "miles"
                online_d = ud.email_me_about_new_auctions_distance or 100
                in_person_d = ud.email_me_about_new_in_person_auctions_distance or 100
                if unit == "km":
                    online_d = round(online_d * MILES_TO_KM)
                    in_person_d = round(in_person_d * MILES_TO_KM)
                context["online_distance"] = online_d
                context["in_person_distance"] = in_person_d
                context["distance_unit"] = unit
            except Exception:
                context["user_has_location"] = False
        return context

    def get_table(self, **kwargs):
        return self.table_class(self.get_table_data(), request=self.request, **kwargs)


class Leaderboard(ListView):
    model = UserData
    template_name = "leaderboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["total_lots"] = UserData.objects.filter(rank_total_lots__isnull=False).order_by("rank_total_lots")
        context["unique_species"] = UserData.objects.filter(number_unique_species__isnull=False).order_by(
            "rank_unique_species"
        )
        # context['total_spent'] = UserData.objects.filter(rank_total_spent__isnull=False).order_by('rank_total_spent')
        context["total_bids"] = UserData.objects.filter(rank_total_bids__isnull=False).order_by("rank_total_bids")
        return context


class AllLots(LotListView, AuctionViewMixin):
    """Show all lots"""

    rewrite_url = (
        # use JS to rewrite the shown URL.  This is used only for auctions.
        None
    )
    auction = None
    allow_non_admins = True

    def render_to_response(self, context, **response_kwargs):
        """override the default just to add a cookie -- this will allow us to save ordering for subsequent views"""
        response = super().render_to_response(context, **response_kwargs)
        if hasattr(self, "ordering"):
            response.set_cookie("lot_order", self.ordering)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = self.request.GET.copy()
        can_show_unloved_tip = True
        self.ordering = ""  # default ordering is set in LotFilter.__init__
        # I don't love having this in two places, but it seems necessary
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        if "order" in data:
            self.ordering = data["order"]
        else:
            if "lot_order" in self.request.COOKIES:
                data["order"] = self.request.COOKIES["lot_order"]
                self.ordering = data["order"]
        if self.ordering == "unloved":
            can_show_unloved_tip = False
            if randint(1, 10) > 9:
                # we need a gentle nudge to remind people not to ALWAYS sort by least popular
                context["search_button_tooltip"] = "Sorting by least popular"
        if not context["auction"]:
            context["auction"] = self.auction
        else:
            self.auction = context["auction"]
        if self.auction:
            context["is_auction_admin"] = self.is_auction_admin
            if self.auction.minutes_to_end < 1440 and self.auction.minutes_to_end > 0 and can_show_unloved_tip:
                context["search_button_tooltip"] = "Try sorting by least popular to find deals!"
        if self.rewrite_url:
            if "auction" not in data and "q" not in data:
                context["rewrite_url"] = self.rewrite_url
        if "q" in data:
            if data["q"]:
                user = None
                if self.request.user.is_authenticated:
                    user = self.request.user
                SearchHistory.objects.create(user=user, search=data["q"], auction=self.auction)
        context["lot_view_type"] = "all"
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        # LotFilter hides lots posted in the last 20 minutes from non-owners. When those are the
        # only lots in the auction the list looks empty, so flag it and let the template explain the
        # short wait instead of showing a bare "No lots found". Superusers and in-person auctions
        # don't hide new lots (see LotFilter.qs), so there's nothing to explain there.
        context["recently_added_lots_hidden"] = False
        if self.auction and self.auction.is_online and not self.request.user.is_superuser:
            recent_lots = Lot.objects.filter(
                auction=self.auction,
                is_deleted=False,
                banned=False,
                date_posted__gte=timezone.now() - timedelta(minutes=20),
            )
            if self.request.user.is_authenticated:
                recent_lots = recent_lots.exclude(user=self.request.user)
            context["recently_added_lots_hidden"] = recent_lots.exists()
        context["hide_google_login"] = True
        return context


class Invoices(ListView, LoginRequiredMixin):
    """Get all invoices for the current user"""

    model = Invoice
    template_name = "all_invoices.html"
    ordering = ["-date"]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            Q(auctiontos_user__user=self.request.user) | Q(auctiontos_user__email=self.request.user.email)
        ).order_by("-date")
        return qs

    # def get_context_data(self, **kwargs):
    #     context = super().get_context_data(**kwargs)
    #     return context


class InvoiceCreateView(LoginRequiredMixin, View, AuctionViewMixin):
    """Create a new invoice for a user in an auction"""

    def get(self, request, *args, **kwargs):
        """Create invoice and redirect to invoice detail page"""
        # Get the auctiontos
        auctiontos_pk = self.kwargs.get("pk")
        try:
            auctiontos = AuctionTOS.objects.get(pk=auctiontos_pk)
        except AuctionTOS.DoesNotExist:
            messages.error(request, "User not found")
            return redirect(reverse("home"))

        # Set auction for permission check
        self.auction = auctiontos.auction

        # Check if user is auction admin
        if not self.is_auction_admin:
            messages.error(request, "You don't have permission to create invoices for this auction")
            return redirect(reverse("home"))

        # Auto check-in if auction uses check-in mode
        if auctiontos.auction.use_check_in_mode and not auctiontos.checked_in:
            auctiontos.checked_in = timezone.now()
            update_fields = ["checked_in"]
            if not auctiontos.bidding_allowed:
                auctiontos.bidding_allowed = True
                update_fields.append("bidding_allowed")
            auctiontos.save(update_fields=update_fields)
            auctiontos.auction.create_history(
                applies_to="USERS",
                action=f"Checked in {auctiontos.name} (invoice created)",
                user=request.user,
            )

        # Check for existing invoices - get the oldest one (first created)
        existing_invoice = (
            Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).order_by("date").first()
        )

        if existing_invoice:
            # Check for and delete any duplicate invoices (keep the oldest)
            duplicate_invoices = Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).exclude(
                pk=existing_invoice.pk
            )

            duplicate_count = duplicate_invoices.count()
            if duplicate_count > 0:
                duplicate_invoices.delete()
                messages.info(request, f"Removed {duplicate_count} duplicate invoice(s)")

            # Redirect to existing invoice
            messages.info(request, "Invoice already exists for this user")
            return redirect(existing_invoice.get_absolute_url())

        # Create new invoice
        invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=auctiontos.auction)
        invoice.recalculate()

        messages.success(request, f"Invoice created for {auctiontos.name}")
        return redirect(invoice.get_absolute_url())


class InvoiceView(DetailView, FormMixin, AuctionViewMixin):
    """Show a single invoice"""

    template_name = "invoice.html"
    model = Invoice
    # form_class = InvoiceUpdateForm
    # expects opened or printed, this field will be set to true when the user the invoice is for opens it
    form_view = "opened"
    allow_non_admins = True
    authorized_by_default = False
    using_no_login_link = False

    def get_object(self):
        """"""
        try:
            return Invoice.objects.get(pk=self.kwargs.get(self.pk_url_kwarg))
        except Invoice.DoesNotExist:
            if self.request.user.is_authenticated:
                return Invoice.objects.filter(
                    auctiontos_user__user=self.request.user,
                    auction__slug=self.kwargs["slug"],
                ).first()
        return None

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        auth = self.authorized_by_default
        self.is_admin = False
        invoice = self.get_object()
        if not invoice:
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.kwargs["slug"]).first()
            if auction:
                messages.error(
                    request,
                    "You don't have an invoice for this auction yet.  Your invoice will be created once you buy or sell lots in this auction.",
                )
                return redirect(auction.get_absolute_url())
            raise Http404
        mark_invoice_viewed_by_user = False
        self.auction = invoice.auction or (invoice.auctiontos_user.auction if invoice.auctiontos_user else None)
        if self.auction and self.is_auction_admin:
            auth = True
            self.is_admin = True
        elif not self.auction and invoice.club and request.user.is_authenticated:
            if check_club_permission(request.user, invoice.club, "permission_add_edit"):
                auth = True
                self.is_admin = True
        if self.auction and self.auction.invoice_payment_instructions and invoice.status == "UNPAID":
            messages.info(request, self.auction.invoice_payment_instructions)
        if request.user.is_authenticated:
            if invoice.club and invoice.buyer == request.user:
                mark_invoice_viewed_by_user = True
                auth = True
            elif invoice.auctiontos_user and (
                invoice.auctiontos_user.email == request.user.email or invoice.auctiontos_user.user == request.user
            ):
                mark_invoice_viewed_by_user = True
                auth = True
        if not auth:
            messages.error(
                request,
                "Your account doesn't have permission to view this invoice. Are you signed in with the correct account?",
            )
            return redirect(reverse("home"))
        if mark_invoice_viewed_by_user:
            setattr(invoice, self.form_view, True)  # this will set printed or opened as appropriate
            invoice.save()
        self.InvoiceAdjustmentFormSet = modelformset_factory(
            InvoiceAdjustment, extra=1, can_delete=True, form=InvoiceAdjustmentForm
        )
        self.queryset = invoice.adjustments
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        invoice = self.get_object()
        _ensure_invoice_renewal_state(invoice)
        context = {}
        context["debug"] = settings.DEBUG
        context["using_no_login_link"] = self.using_no_login_link
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin
        context["invoice"] = invoice
        # light theme for some invoices to allow printing
        if "print" in self.request.GET.copy():
            context["base_template_name"] = "print.html"
            context["show_links"] = False
        else:
            context["base_template_name"] = "base.html"
            context["show_links"] = True
        context["location"] = invoice.location
        context["print_label_link"] = None
        if invoice.auction and invoice.auctiontos_user and invoice.auction.is_online:
            context["print_label_link"] = reverse(
                "print_labels_by_bidder_number",
                kwargs={
                    "slug": invoice.auction.slug,
                    "bidder_number": invoice.auctiontos_user.bidder_number,
                },
            )
        context["is_auction_admin"] = self.auction and self.is_auction_admin
        context["website_focus"] = settings.WEBSITE_FOCUS
        club = invoice.auction.club if invoice.auction else None
        context["viewer_has_bap"] = club is not None and check_club_permission(
            self.request.user, club, "permission_manage_bap"
        )
        if context["viewer_has_bap"] and club:
            context["bap_default_points"] = club.points_per_lot
        return context

    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        adjustment_formset = self.InvoiceAdjustmentFormSet(
            self.request.POST,
            form_kwargs={"invoice": self.get_object()},
            queryset=self.queryset,
        )
        if adjustment_formset.is_valid() and self.is_admin:
            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.invoice = self.get_object()
                adjustment.user = request.user
                adjustment.save()
            if adjustments:
                messages.success(self.request, "Invoice adjusted")
                if self.auction:
                    self.auction.create_history(
                        applies_to="INVOICES",
                        action=f"Adjusted invoice for {self.get_object().auctiontos_user.name if self.get_object().auctiontos_user else self.get_object()}",
                        user=request.user,
                    )
            for form in adjustment_formset.deleted_forms:
                if form.instance.pk:
                    form.instance.delete()
            return redirect(reverse("invoice_by_pk", kwargs={"pk": self.get_object().pk}))
        context = self.get_context_data(**kwargs)
        context["formset"] = adjustment_formset
        context["helper"] = InvoiceAdjustmentFormSetHelper()
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        if self.get_object().unsold_lot_warning and self.auction and self.is_auction_admin:
            messages.info(
                self.request,
                "This user still has unsold lots, make sure to sell all non-donation lots before marking this ready or paid.",
            )
        self.object = self.get_object()
        invoice_adjustment_formset = self.InvoiceAdjustmentFormSet(
            form_kwargs={"invoice": self.get_object()}, queryset=self.queryset
        )
        helper = InvoiceAdjustmentFormSetHelper()
        context = self.get_context_data(object=self.object)
        context["formset"] = invoice_adjustment_formset
        context["helper"] = helper
        # recaluclating slows things down,
        # I am not sure if it's a good idea to have it here or not
        self.object.recalculate()
        return self.render_to_response(context)


class InvoiceNoLoginView(InvoiceView):
    """Enter a uuid, go to your invoice.  This bypasses the login checks"""

    # need a template with a popup
    authorized_by_default = True
    form_view = "opened"
    using_no_login_link = True

    def get_object(self):
        if not self.uuid:
            raise Http404
        return get_object_or_404(Invoice, no_login_link=self.uuid)

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get("uuid", None)
        invoice = self.get_object()
        invoice.opened = True
        invoice.save()
        if invoice.auctiontos_user:
            invoice.auctiontos_user.email_address_status = "VALID"
            invoice.auctiontos_user.save()
        if invoice.club and not invoice.auction:
            return render(
                request,
                "auctions/club_membership_payment.html",
                {"club": invoice.club, "invoice": invoice},
            )
        return super().dispatch(request, *args, **kwargs)


class SquarePaymentSuccessView(InvoiceNoLoginView):
    """
    Success redirect for Square payment links.
    Marks invoice as opened but does NOT verify email address.
    This prevents incorrectly marking emails as valid when users scan QR codes.
    """

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get("uuid", None)
        invoice = self.get_object()
        # Mark invoice as opened but don't verify email
        invoice.opened = True
        invoice.save()
        # Skip the parent's dispatch which marks email as VALID
        # Call grandparent (InvoiceView) dispatch instead
        return InvoiceView.dispatch(self, request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_payment_button"] = True
        return context


class LotLabelView(TemplateView, WeasyTemplateResponseMixin, AuctionViewMixin):
    """View and print labels for an auction"""

    # these are defined in urls.py and used in get_object(), below
    bidder_number = None
    username = None
    # This one is the old one, it has some good stuff in it like QR code
    # template_name = "invoice_labels.html"
    template_name = "label_template.html"
    allow_non_admins = True
    filename = ""  # this will be automatically generated in dispatch
    # Tuned for known overflow breakpoints (long seller emails/lot numbers) per label preset.
    # shrink_threshold: start scaling after this length.
    # ratio_base: numerator for ratio_base / text_length scaling.
    # min_ratio: floor so text stays readable.
    SELLER_EMAIL_FONT_CONFIG = {
        "sm": {"shrink_threshold": 18, "ratio_base": 14, "min_ratio": 0.6},
        "lg": {"shrink_threshold": 20, "ratio_base": 15, "min_ratio": 0.55},
        "thermal_sm": {"shrink_threshold": 17, "ratio_base": 13, "min_ratio": 0.45},
        "thermal_very_sm": {"shrink_threshold": 13, "ratio_base": 10, "min_ratio": 0.4},
    }
    LOT_NUMBER_FONT_CONFIG = {
        "sm": {"shrink_threshold": 6, "ratio_base": 4.2, "min_ratio": 0.6},
        "lg": {"shrink_threshold": 6, "ratio_base": 4.8, "min_ratio": 0.6},
        "thermal_sm": {"shrink_threshold": 5, "ratio_base": 3.5, "min_ratio": 0.45},
        "thermal_very_sm": {"shrink_threshold": 4, "ratio_base": 3, "min_ratio": 0.4},
    }

    def get_queryset(self):
        return self.tos.print_labels_qs

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.bidder_number = kwargs.pop("bidder_number", None)
        self.username = kwargs.pop("username", None)
        printing_for_self = False
        if self.bidder_number:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=self.bidder_number).first()
        if self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user__username=self.username).first()
        if not self.bidder_number and not self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user=request.user).first()
        if not self.tos:
            if self.is_auction_admin:
                # should never get here as long as admins are following links
                messages.error(request, "Unable to find any labels to print.")
            else:
                messages.error(
                    request,
                    "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels.",
                )
            return redirect(self.auction.get_absolute_url())
        checks_pass = False
        if self.is_auction_admin:
            checks_pass = True
            # if this is an admin printing someone else's lots, the file name should be the name of the person whose lots they're printing
            self.filename = self.tos.name or self.tos.bidder_number
        if request.user.is_authenticated:
            if request.user == self.tos.user:
                printing_for_self = True
                checks_pass = True
                # if this is a user printing their own lots, the file name should be the name of the auction
                self.filename = str(self.auction)
        if printing_for_self:
            if self.auction.is_online and not self.auction.closed:
                messages.error(
                    request,
                    "This is an online auction; you should print your labels after the auction ends, and before you exchange lots.",
                )
                return redirect(self.auction.get_absolute_url())
        if checks_pass and self.tos:
            if not self.get_queryset():
                if printing_for_self:
                    messages.error(
                        request,
                        "You don't have any lots with printable labels in this auction.",
                    )
                else:
                    if not self.auction.is_online:
                        messages.error(request, "There aren't any lots with printable labels")
                    else:
                        messages.error(
                            request,
                            "No lots with printable labels.  Only lots with a winner will have a label generated for them.",
                        )
                return redirect(self.auction.get_absolute_url())
            return super().dispatch(request, *args, **kwargs)
        else:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))

    def get_pdf_filename(self):
        label_name = re.sub(r"[^a-zA-Z0-9]", "_", (self.filename or "labels").lower())
        return f"{label_name}.pdf"

    @staticmethod
    def get_seller_email_font_size(seller_email, preset):
        """Shrink seller email font for configured label presets when needed."""
        if not seller_email:
            return None
        config = LotLabelView.SELLER_EMAIL_FONT_CONFIG.get(preset)
        if not config:
            return None
        if len(seller_email) <= config["shrink_threshold"]:
            return None
        font_ratio = max(config["min_ratio"], config["ratio_base"] / len(seller_email))
        return f"{font_ratio:.2f}em"

    @staticmethod
    def get_lot_number_font_size(lot_number_display, preset):
        """Shrink lot number font for configured label presets when needed."""
        if not lot_number_display:
            return None
        lot_number_display = str(lot_number_display)
        config = LotLabelView.LOT_NUMBER_FONT_CONFIG.get(preset)
        if not config:
            return None
        if len(lot_number_display) <= config["shrink_threshold"]:
            return None
        font_ratio = max(config["min_ratio"], config["ratio_base"] / len(lot_number_display))
        return f"{font_ratio:.2f}em"

    def get_context_data(self, **kwargs):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        context = {}
        context["empty_labels"] = user_label_prefs.empty_labels
        context["print_border"] = user_label_prefs.print_border
        context["first_column_width"] = 0.62
        if user_label_prefs.preset == "sm":
            # Avery 5160 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 2.55
            context["label_height"] = 0.99
            context["label_margin_right"] = 0.19
            context["label_margin_bottom"] = 0.01
            context["page_margin_top"] = 0.57
            context["page_margin_bottom"] = 0.1
            context["page_margin_left"] = 0.23
            context["page_margin_right"] = 0
            context["font_size"] = 10
            context["unit"] = "in"
        elif user_label_prefs.preset == "lg":
            # Avery 18262 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 3.85
            context["label_height"] = 1.2
            context["label_margin_right"] = 0.25
            context["label_margin_bottom"] = 0.13
            context["page_margin_top"] = 0.88
            context["page_margin_bottom"] = 0.6
            context["page_margin_left"] = 0.3
            context["page_margin_right"] = 0
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
        elif user_label_prefs.preset == "thermal_sm":
            # thermal label printer 3x2
            context["page_width"] = 3
            context["page_height"] = 2
            context["label_width"] = 2.78
            context["label_height"] = 1.9
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            # override the user selected setting for thermal labels
            context["print_border"] = False
        elif user_label_prefs.preset == "thermal_very_sm":
            # thermal label printer 30252 (1 1/8" x 3 1/2")
            context["page_width"] = 3.5
            context["page_height"] = 1.125
            context["label_width"] = 3.3
            context["label_height"] = 1.025
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 12
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            context["print_border"] = False
        else:
            context.update(
                {f"{field.name}": getattr(user_label_prefs, field.name) for field in UserLabelPrefs._meta.get_fields()}
            )
        unit = 2.54 if context.get("unit") == "cm" else 1

        context["label_width"] = context.get("label_width") * unit
        context["label_height"] = context.get("label_height") * unit
        context["label_margin_right"] = context.get("label_margin_right") * unit
        context["label_margin_bottom"] = context.get("label_margin_bottom") * unit

        context["page_margin_top"] = context.get("page_margin_top") * unit
        context["page_margin_bottom"] = context.get("page_margin_bottom") * unit
        context["page_margin_left"] = context.get("page_margin_left") * unit
        context["page_margin_right"] = context.get("page_margin_right") * unit

        context["page_width"] = context.get("page_width") * unit
        context["page_height"] = context.get("page_height") * unit

        # Calculate the available space on the page
        available_width = context["page_width"] - context["page_margin_left"] - context["page_margin_right"]

        available_height = context["page_height"] - context["page_margin_top"] - context["page_margin_bottom"]

        # Page breaks don't work, see https://github.com/Kozea/WeasyPrint/issues/1967
        # manually calculating
        labels_per_row = int(available_width // (context["label_width"] + context["label_margin_right"]))
        labels_per_column = int(available_height // (context["label_height"] + context["label_margin_bottom"]))
        context["labels_per_page"] = labels_per_row * labels_per_column
        if context["labels_per_page"] == 0:
            messages.error(
                self.request,
                "Your lot label setting may be wrong. The label size is too large for the page size.  <a href='/printing'>Adjust your label settings</a>",
            )
            context["labels_per_page"] = 1

        labels = self.get_queryset().select_related(
            "auction",
            "auctiontos_seller",
            "auctiontos_winner",
            "auctiontos_winner__pickup_location",
            "species_category",
            "user",
        )

        # Cap thermal labels at 100 per PDF
        is_thermal = user_label_prefs.preset in ["thermal_sm", "thermal_very_sm"]

        if is_thermal:
            # Check if we have more than 100 labels efficiently
            # We fetch 101 labels to determine if there are more than 100
            labels_list = list(labels[:101])
            if len(labels_list) > 100:
                # Show warning and limit to first 100
                total_labels_count = labels.count()
                labels = labels_list[:100]
                messages.warning(
                    self.request,
                    f"Only the first 100 labels are included in this PDF (you have {total_labels_count} total labels). "
                    f"To print the remaining labels, use the 'Print unprinted labels' option.",
                )
            else:
                # Use the list we already fetched (100 or fewer labels)
                labels = labels_list
        else:
            labels = list(labels)

        for label in labels:
            label.label_printed = True
            label.label_needs_reprinting = False
        Lot.objects.bulk_update(labels, ["label_printed", "label_needs_reprinting"])

        # First column width is fixed at 0.63 for most labels and overridden for large and thermal
        # context['first_column_width'] = (context['label_width'] / 4)
        # let's keep the QR code a fixed size regardless of the label size
        # context['qr_code_height'] = min(context['first_column_width'], context['label_height'] / 2)
        context["qr_code_height"] = 0.5 * 72
        height_for_text = context["label_height"] * 72
        if "qr_code" in self.auction.label_print_fields:
            height_for_text = height_for_text - context["qr_code_height"]
        leading_ratio = 1.3
        line_height = context["font_size"] * leading_ratio
        lines_that_fit = int(height_for_text / line_height * 1.2)
        lines_that_fit -= 1  # for the lot number
        first_column_fields = [
            "quantity_label",
            "donation_label",
            "min_bid_label",
            "buy_now_label",
            "custom_checkbox_label",
            "custom_dropdown_label",
            "i_bred_this_fish_label",
            "auction_date",
        ]
        first_column_fields_to_print = [
            field for field in first_column_fields if field in self.auction.label_print_fields
        ]
        # Split the fields: first column and overflow to second column
        first_column_fields = first_column_fields_to_print[:lines_that_fit]
        first_column_fields_to_put_in_second_column = first_column_fields_to_print[lines_that_fit:]

        for label in labels:
            label_first_column_fields = []
            label_second_column_fields = []
            for field in first_column_fields:
                label_first_column_fields.append(getattr(label, field))
            for field in first_column_fields_to_put_in_second_column:
                label_second_column_fields.append(getattr(label, field))
            label.first_column_fields = label_first_column_fields
            label.second_column_fields = label_second_column_fields
            label.seller_email_font_size = self.get_seller_email_font_size(label.seller_email, user_label_prefs.preset)
            label.lot_number_font_size = self.get_lot_number_font_size(
                label.lot_number_display, user_label_prefs.preset
            )
        context["labels"] = (["empty"] * context["empty_labels"]) + list(labels)
        context["text_area_width"] = context["label_width"] - context["first_column_width"]
        context["description_font_size"] = int(context["font_size"] * 0.7)
        context["first_column_font_size"] = int(context["font_size"] * 0.8)
        # for sizing
        context["all_borders"] = False
        return context

    def generate_qr_code(self, label, qr_code_width, qr_code_height):
        label_qr_code = qr_code.qrcode.maker.make_qr_code_image(
            label.qr_code,
            QRCodeOptions(
                size="T",
                border=1,
                error_correction="L",
                image_format="png",
            ),
        )
        image_stream = BytesIO(label_qr_code)
        return PImage(
            image_stream,
            width=qr_code_width,
            height=qr_code_height,
            lazy=0,
            hAlign="LEFT",
        )


class UnprintedLotLabelsView(LotLabelView):
    """Print lot labels, but only ones that haven't already been printed"""

    def get_queryset(self):
        return self.tos.unprinted_labels_qs


class SingleLotLabelView(LotLabelView):
    """Reprint labels for just one lot"""

    def get_queryset(self):
        return Lot.objects.filter(pk=self.lot.pk)

    def dispatch(self, request, *args, **kwargs):
        self.lot = get_object_or_404(Lot, pk=kwargs.pop("pk"), is_deleted=False)
        self.filename = f"label_{self.lot.lot_number_display}"
        if self.lot.auctiontos_seller:
            self.auction = self.lot.auctiontos_seller.auction
            auth = False
            if self.lot.auctiontos_seller.user and self.lot.auctiontos_seller.user.pk == request.user.pk:
                auth = True
            if not auth and not self.is_auction_admin:
                messages.error(
                    request,
                    "You can't print labels for other people's lots unless you are an admin",
                )
                return redirect(reverse("home"))
        if not self.lot.auctiontos_seller:
            if self.lot.user and self.lot.user is not request.user:
                messages.error(request, "You can only print labels for your own lots")
                return redirect(reverse("home"))
        # ?format=png (or ?fmt=png) returns a single rendered PNG via the shared label renderer
        # instead of the WeasyPrint PDF sheet, so the web endpoint matches the mobile app. Honors
        # ?resolution=WIDTHxHEIGHT&dpi=N (default 600x400 @ 203dpi).
        if (request.GET.get("format") or request.GET.get("fmt")) == "png":
            from .mobile.services.labels import LabelService

            try:
                content, content_type = LabelService.render_label(
                    self.lot,
                    "png",
                    resolution=request.GET.get("resolution"),
                    dpi=request.GET.get("dpi"),
                )
            except ValueError:
                logging.getLogger(__name__).warning(
                    "Invalid label rendering parameters for lot %s",
                    self.lot.pk,
                    exc_info=True,
                )
                return HttpResponseBadRequest("Invalid label rendering parameters.")
            return HttpResponse(content, content_type=content_type)
        # super() would try to find an auction
        return View.dispatch(self, request, *args, **kwargs)


class GetClubs(APIView):
    """Used for autocomplete on the contact info page"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        search = request.POST["search"]
        result = Club.objects.filter(Q(name__icontains=search) | Q(abbreviation__icontains=search)).values(
            "id", "name", "abbreviation"
        )
        return JsonResponse(list(result), safe=False)


class BulkSetLotsWon(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Sell all lots based on the current filter to online high bidder"""

    template_name = "auctions/generic_admin_form.html"
    form_class = BulkSellLotsToOnlineHighBidder

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.original_query = request.GET.get("query", "")
        if not self.original_query:
            self.original_query = request.POST.get("query", "")
        self.query = unquote(self.original_query)
        self.queryset = LotAdminFilter.generic(self, self.auction.lots_qs, self.query)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            for lot in self.queryset:
                try:
                    lot.sell_to_online_high_bidder
                except Exception:
                    logger.exception("sell_to_online_high_bidder failed for lot %s", lot.pk)
                    continue
                if lot.auctiontos_winner:
                    try:
                        lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
                    except Exception:
                        logger.exception("add_winner_message failed for lot %s", lot.pk)
            try:
                self.auction.create_history(
                    applies_to="LOTS",
                    action=f"Sold {self.queryset.count()} lots to online high bidder",
                    user=request.user,
                )
            except Exception:
                logger.exception("create_history failed for auction %s", self.auction.pk)
            return close_modal_response("reload-page")
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tooltip = "This is intended to be used with silent auctions where people place bids on their phones, or with hybrid online auctions where some lots will be sold ahead of time.  It will sell any lots with online bids to the current online high bidder."
        if not self.query:
            tooltip += "<br><br><span class='text-warning'>You are about to set the winners of all lots.  This is a bad idea, you should click on cancel and then type in a filter first.</span>"
        else:
            tooltip += f"<br><br>You are about to set the winners of {self.queryset.count()} lots that match the filter <span class='text-warning'>{self.query}</span>"
        context["tooltip"] = tooltip
        context["modal_title"] = "Sell lots to online high bidders"
        return context

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["query"] = self.query
        form_kwargs["auction"] = self.auction
        form_kwargs["queryset"] = self.queryset
        return form_kwargs


class InvoiceBulkUpdateStatus(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Change invoice statuses in bulk"""

    template_name = "auctions/generic_admin_form.html"
    form_class = ChangeInvoiceStatusForm
    show_checkbox = False

    def get_queryset(self):
        return Invoice.objects.filter(auctiontos_user__auction=self.auction, status=self.old_invoice_status)

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.invoice_count = self.get_queryset().count()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["invoice_count"] = self.invoice_count
        if self.invoice_count:
            form_kwargs["show_checkbox"] = self.show_checkbox
        else:
            form_kwargs["show_checkbox"] = False
        form_kwargs["post_target_url"] = "auction_invoices_" + self.new_status_display.lower()
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

    def post(self, request, *args, **kwargs):
        invoices = self.get_queryset()
        run_at = None
        # Set or clear invoice_notification_due based on new status
        if self.new_invoice_status in ("UNPAID", "PAID"):
            run_at = timezone.now() + timedelta(seconds=INVOICE_NOTIFICATION_DELAY_SECONDS)
        for invoice in invoices:
            # Core: change the status and save. Extras follow, each guarded.
            if self.new_invoice_status in ("PAID", "UNPAID") and not invoice.renewal_needed:
                try:
                    _ensure_invoice_renewal_state(invoice)
                except Exception:
                    logger.exception("Failed to ensure renewal state for invoice %s in bulk", invoice.pk)
            try:
                invoice.status = self.new_invoice_status
                invoice.invoice_notification_due = run_at
                invoice.save()
            except Exception:
                logger.exception("Failed to update invoice %s to %s in bulk", invoice.pk, self.new_invoice_status)
                continue
            try:
                invoice.recalculate()
            except Exception:
                logger.exception("recalculate failed for invoice %s in bulk", invoice.pk)
            if self.new_invoice_status == "PAID":
                try:
                    _process_invoice_membership_renewal(invoice, acting_user=request.user)
                except Exception:
                    logger.exception("membership renewal failed for invoice %s in bulk", invoice.pk)
            try:
                if run_at:
                    schedule_invoice_notification(invoice.pk, run_at)
                else:
                    cancel_invoice_notification(invoice.pk)
            except Exception:
                logger.exception("schedule/cancel notification failed for invoice %s in bulk", invoice.pk)
        action = f"Set {invoices.count()} invoices from {self.old_status_display} to {self.new_status_display}"
        try:
            self.auction.create_history(
                applies_to="INVOICES",
                action=action,
                user=request.user,
            )
        except Exception:
            logger.exception("create_history failed for bulk invoice update on auction %s", self.auction.pk)
        return close_modal_response("reload-page")


class MarkInvoicesReady(InvoiceBulkUpdateStatus):
    old_invoice_status = "DRAFT"
    new_invoice_status = "UNPAID"
    old_status_display = "draft"
    new_status_display = "ready"

    show_checkbox = True

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.auction.email_users_when_invoices_ready = form.cleaned_data.get(
                "send_invoice_ready_notification_emails"
            )
            self.auction.save()
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = (
            f"Changing the invoice status to ready will block users from bidding.  You should make any needed adjustments before setting invoices to ready.  This will change the status of {self.invoice_count} invoices, and allow you to export them to PayPal."
        )
        if not self.auction.closed and self.auction.is_online:
            context["unsold_lot_warning"] = "Don't set invoices ready yet!  This auction hasn't ended."
            if not self.auction.minutes_to_end:
                active_lot_count = self.auction.lots_qs.filter(active=True).count()
                context["unsold_lot_warning"] += (
                    f" There are still {active_lot_count} lots with last-minute bids on them"
                )
        if not self.auction.is_online:
            context["unsold_lot_warning"] = (
                "You usually don't need to use this.  Set people's invoices to paid one at a time, as people leave the auction."
            )
        if not self.invoice_count:
            context["modal_title"] = "No open invoices"
            context["tooltip"] = (
                "There aren't any open invoices in this auction.  Invoices are created automatically whenever a user buys or sells a lot."
            )
        else:
            context["modal_title"] = f"Set {self.invoice_count} open invoices to ready"
        return context


class MarkInvoicesPaid(InvoiceBulkUpdateStatus):
    old_invoice_status = "UNPAID"
    new_invoice_status = "PAID"
    old_status_display = "ready"
    new_status_display = "paid"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        unchanged_invoices = Invoice.objects.filter(auctiontos_user__auction=self.auction, status="DRAFT").count()
        context["unsold_lot_warning"] = (
            "You probably don't need this.  Instead, set invoices to paid one at a time, as users pay them."
        )
        context["tooltip"] = ""
        if unchanged_invoices:
            context["tooltip"] += (
                f" There are {unchanged_invoices} open invoices in this auction that will not be changed; set them ready first if you want to change them."
            )
        context["tooltip"] += f" This will set {self.invoice_count} ready invoices to paid."
        if not self.invoice_count:
            context["modal_title"] = "No ready invoices"
            if unchanged_invoices:
                context["tooltip"] = (
                    f"There's {unchanged_invoices} invoices that are still open.  You should set open invoices to ready before using this."
                )
        else:
            context["modal_title"] = f"Set {self.invoice_count} ready invoices to paid"
        return context


class LotRefundDialog(LoginRequiredMixin, DetailView, FormMixin, AuctionViewMixin):
    model = Lot
    template_name = "auctions/generic_admin_form.html"
    form_class = LotRefundForm
    winner_invoice = None
    seller_invoice = None

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.lot.auction.create_history(
                applies_to="LOTS",
                action=f"Removed/refunded lot {self.lot.lot_number_display}",
                user=request.user,
            )
            refund = form.cleaned_data["partial_refund_percent"] or 0
            self.lot.refund(refund, request.user)
            banned = form.cleaned_data["banned"]
            self.lot.remove(banned, request.user)
            if self.seller_invoice:
                self.seller_invoice.recalculate()
            if self.winner_invoice:
                self.winner_invoice.recalculate()
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)

    def dispatch(self, request, *args, **kwargs):
        pk = self.kwargs.get(self.pk_url_kwarg)
        self.lot = get_object_or_404(
            Lot,
            is_deleted=False,
            auction__isnull=False,
            auctiontos_seller__isnull=False,
            pk=pk,
        )
        self.object = self.lot
        self.auction = self.lot.auction
        self.is_auction_admin
        self.seller_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_seller).first()
        if self.lot.auctiontos_winner:
            self.winner_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_winner).first()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["lot"] = self.lot
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["lot"] = self.lot
        if not self.lot.sold:
            context["tooltip"] = (
                "This lot has not sold, there's nothing to refund.  If there's a problem with this lot, remove it."
            )
        else:
            # if a refund has already been issued for this lot, we need to calculate how much is unpaid by temporarily removing it
            existing_refund = self.lot.partial_refund_percent
            if existing_refund:
                self.lot.partial_refund_percent = 0
                self.lot.save()
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                # if we removed a refund before for math purposes, put it back now
                self.lot.partial_refund_percent = existing_refund
                self.lot.save()
                tooltip = "A refund has already been issued for this lot.  The refund percent is based on the original sale price.<br><br>"
            else:
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                tooltip = "<small>This lot has sold.  If there's a problem with it, you should issue a refund which will show up on the seller and winner's invoices.</small><br><br>"
            if self.lot.winning_price:
                full_buyer_refund = self.lot.winning_price + (self.lot.winning_price * self.lot.auction.tax / 100)
            else:
                full_buyer_refund = 0
            if self.seller_invoice and self.seller_invoice.status == "DRAFT":
                tooltip += (
                    "Seller's invoice is open; $<span id='seller_refund'></span> will automatically be removed.<br>"
                )
            else:
                tooltip += "Seller's invoice is not open.  <span class='text-warning'>Collect $<span id='seller_refund'></span> from the seller</span><br>"
            if self.lot.winning_price and self.lot.auctiontos_winner:
                if self.winner_invoice and self.winner_invoice.status == "DRAFT":
                    tooltip += "Winner's invoice is open; refund of $<span id='buyer_refund'></span> will automatically be added<br>"
                else:
                    tooltip += "Winner's invoice is not open.  <span class='text-warning'>Refund the winner $<span id='buyer_refund'></span></span>"
                    if self.lot.auction.tax:
                        tooltip += f"<small> (includes {self.lot.auction.tax}% tax)</small><br>"
                    else:
                        tooltip += "<br>"
            tooltip += "<br><br>"
            extra_script = """
            <script>$('#id_partial_refund_percent').on('change keyup', function(){recalculate()});
            function recalculate(){
                var refund = $('#id_partial_refund_percent').val();var tax = """
            extra_script += f"{self.lot.auction.tax};var full_seller_refund = {full_seller_refund};var full_buyer_refund = {full_buyer_refund};"
            extra_script += """
                $('#seller_refund').text((full_seller_refund*refund/100).toFixed(2));
                if (full_buyer_refund) {
                    $('#buyer_refund').text((full_buyer_refund*refund/100).toFixed(2));
                };
            }
            $(document).ready( function(){recalculate()});
            </script>
            """
            context["extra_script"] = mark_safe(extra_script)
            context["tooltip"] = mark_safe(tooltip)
        context["modal_title"] = f"Remove or refund lot {self.lot.lot_number_display}"
        return context


class PayPalRequestError(Exception):
    """Raised when a PayPal API request fails in a recoverable way for the caller."""


class PayPalAPIMixin:
    """PayPal API methods for platform partner integration.

    Required settings:
      - PAYPAL_API_BASE: API base URL (sandbox or live)
      - PAYPAL_CLIENT_ID, PAYPAL_SECRET: OAuth credentials
      - PARTNER_MERCHANT_ID: Platform's PayPal merchant ID
      - PAYPAL_BN_CODE: Partner attribution code (for revenue tracking)
      - PAYPAL_WEBHOOK_ID: Registered webhook ID (for webhook verification)
    """

    def _paypal_auth(self):
        """Return ``(client_id, secret)`` for the current PayPal request.

        A club in non-OAuth mode supplies its own app credentials via
        ``self.club_paypal_credentials`` (set by callers from the invoice); every other
        caller falls back to the site's platform app from settings.
        """
        creds = getattr(self, "club_paypal_credentials", None)
        if creds and creds[0] and creds[1]:
            return creds[0], creds[1]
        return settings.PAYPAL_CLIENT_ID, settings.PAYPAL_SECRET

    def _get_access_token(self):
        client_id, secret = self._paypal_auth()
        token_resp = requests.post(
            f"{settings.PAYPAL_API_BASE}/v1/oauth2/token",
            auth=(client_id, secret),
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        token_resp.raise_for_status()
        return token_resp.json()["access_token"]

    def _build_paypal_headers(self, merchant_id="", include_bn_code=True, token=None):
        """Common header builder for PayPal API calls"""
        headers = {
            "Authorization": f"Bearer {token or self._get_access_token()}",
            "Content-Type": "application/json",
        }
        # The BN code is our platform partner-attribution id; it's meaningless (and
        # potentially rejected) when calling with a club's own standalone app credentials.
        using_club_creds = bool(getattr(self, "club_paypal_credentials", None))
        if include_bn_code and not using_club_creds and getattr(settings, "PAYPAL_BN_CODE", None):
            headers["PayPal-Partner-Attribution-Id"] = settings.PAYPAL_BN_CODE
        if merchant_id:
            headers["PayPal-Auth-Assertion"] = self._build_auth_assertion(merchant_id)
        return headers

    def _build_auth_assertion(self, merchant_payer_id):
        """Build unsigned PayPal-Auth-Assertion JWT for acting on behalf of a merchant."""
        header = {"alg": "none"}
        payload = {
            "iss": self._paypal_auth()[0],
            "payer_id": merchant_payer_id,  # obtained from partner referral flow
        }

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")

        unsigned_jwt = f"{b64(header)}.{b64(payload)}."
        return unsigned_jwt

    def _paypal_request(self, method, endpoint, *, json=None, params=None, merchant_id="", include_bn_code=True):
        """Single request helper used by get_from_paypal and post_to_paypal."""
        url = f"{settings.PAYPAL_API_BASE}/{str(endpoint).lstrip('/')}"
        headers = self._build_paypal_headers(merchant_id=merchant_id, include_bn_code=include_bn_code)
        self.paypal_debug = ""
        try:
            resp = requests.request(method, url, headers=headers, json=json, params=params, timeout=10)
            resp.raise_for_status()
            self.paypal_debug = resp.headers.get("Paypal-Debug-Id")
            return resp.json()
        except requests.HTTPError:
            debug_id = resp.headers.get("Paypal-Debug-Id", "")
            self.paypal_debug = debug_id
            safe_headers = dict(headers or {})
            if "Authorization" in safe_headers:
                safe_headers["Authorization"] = "Bearer ****"
            logger.error(
                "PayPal API call failed: %s %s status=%s debug_id=%s req_headers=%s req_params=%s req_json=%s resp_text=%s",
                method,
                url,
                resp.status_code,
                debug_id,
                safe_headers,
                params,
                json,
                resp.text[:1000],
            )
            msg = f"PayPal API call failed: {method} {url} status={resp.status_code} debug_id={debug_id}"
            raise PayPalRequestError(msg)

    def post_to_paypal(self, endpoint, payload, include_bn_code=True):
        """POST JSON to a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("POST", endpoint, json=payload, include_bn_code=include_bn_code)

    def get_from_paypal(self, endpoint, include_bn_code=True, params=None):
        """GET from a PayPal API endpoint and return parsed JSON."""
        return self._paypal_request("GET", endpoint, params=params, include_bn_code=include_bn_code)

    def create_order(self, invoice, member_pk=""):
        """Pass an invoice object and create an order for it.
        Returns an approval URL or None if the request failed"""
        # A club using its own (non-OAuth) PayPal app pays through its own credentials,
        # exactly as the site keys are used for the platform account. None => site app.
        self.club_paypal_credentials = invoice.paypal_credentials
        currency = invoice.currency

        items = []
        for lot in invoice.bought_lots_queryset:
            items.append(
                {
                    "name": f"{lot.lot_number_display} - {lot.lot_name}",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{lot.winning_price:.2f}"},
                    "category": "PHYSICAL_GOODS",
                    "url": lot.full_lot_link,
                    "tax": {"currency_code": currency, "value": f"{lot.tax:.2f}"},
                }
            )

        # Charge the rounded balance so the amount matches the invoice total the buyer sees; the
        # breakdown below absorbs the rounding delta as an adjustment/discount line. Falls back to
        # the exact amount when invoice rounding is off (rounded_net_after_payments handles that).
        target_total = (Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)).quantize(Decimal("0.01"))
        # Base components from items
        item_total = Decimal(str(invoice.total_bought)).quantize(Decimal("0.01"))
        tax_total = Decimal(str(invoice.tax)).quantize(Decimal("0.01"))

        # Adjustment needed to make breakdown sum to target_total
        # target_total = item_total + tax_total + handling/shipping/insurance - discount
        # We’ll use:
        #  - discount for negative adjustments
        #  - an explicit “Adjustments” line item for positive adjustments (and include it in item_total)
        adjustment = (target_total - (item_total + tax_total)).quantize(Decimal("0.01"))

        discount_value = Decimal("0.00")
        if adjustment > 0:
            # Add an adjustment item and include in item_total
            items.append(
                {
                    "name": "Adjustments",
                    "quantity": "1",
                    "unit_amount": {"currency_code": currency, "value": f"{adjustment:.2f}"},
                    "category": "PHYSICAL_GOODS",
                }
            )
            item_total = (item_total + adjustment).quantize(Decimal("0.01"))
            adjustment = Decimal("0.00")
        elif adjustment < 0:
            # Use discount as a positive number
            discount_value = abs(adjustment)

        breakdown = {
            "item_total": {"currency_code": currency, "value": f"{item_total:.2f}"},
            "tax_total": {"currency_code": currency, "value": f"{tax_total:.2f}"},
        }
        if discount_value > 0:
            breakdown["discount"] = {"currency_code": currency, "value": f"{discount_value:.2f}"}

        if invoice.club:
            description = f"Club membership fee for {invoice.club.name}"[:127]
        elif invoice.auctiontos_user and invoice.auction:
            description = f"Bidder {invoice.auctiontos_user.bidder_number} in {invoice.auction.title}"[:127]
        else:
            description = "Membership fee"[:127]
        purchase_unit = {
            "description": description,
            "reference_id": str(invoice.pk),
            "amount": {
                "currency_code": currency,
                # Must equal the breakdown sum (which is built to total target_total), or PayPal rejects it.
                "value": f"{target_total:.2f}",
                "breakdown": breakdown,
            },
            "items": items,
        }
        if invoice.soft_descriptor:
            purchase_unit["soft_descriptor"] = invoice.soft_descriptor[:22]
        if invoice.club:
            if invoice.club.uses_own_paypal_credentials:
                # The club's own app receives the payment directly -- no payee override and
                # no platform fee, exactly as the site keys behave for the site account.
                paypal_merchant_id = None
            elif invoice.club.uses_site_paypal:
                paypal_merchant_id = "admin"
            else:
                club_seller = invoice.club.effective_paypal_seller
                paypal_merchant_id = (
                    club_seller.paypal_merchant_id if club_seller and club_seller.paypal_merchant_id else None
                )
        elif invoice.auction:
            paypal_merchant_id = invoice.auction.paypal_information
        else:
            paypal_merchant_id = None
        if paypal_merchant_id and paypal_merchant_id != "admin":
            # if this is not set, payment will go to the platform account whose keys are in the .env
            purchase_unit["payee"] = {"merchant_id": paypal_merchant_id}
            if settings.PAYPAL_PLATFORM_FEE and settings.PAYPAL_PLATFORM_FEE > 0:
                amt_value = Decimal(purchase_unit["amount"]["value"])
                fee_amount = (amt_value * settings.PAYPAL_PLATFORM_FEE / Decimal(100)).quantize(Decimal(0.01))
                if fee_amount > 0:
                    purchase_unit["payment_instruction"] = {
                        "platform_fees": [
                            {
                                "amount": {
                                    "currency_code": currency,
                                    "value": str(fee_amount),
                                }
                            }
                        ],
                        "disbursement_mode": "INSTANT",
                    }

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            # This code forces payment from the auctiontos.email and will fail if the user
            # doesn't have that email address as their primary PayPal address
            # "payment_source": {
            #     "paypal": {
            #         "email_address": invoice.auctiontos_user.email,
            #     },
            # },
            "application_context": {
                "brand_name": settings.NAVBAR_BRAND,
                # Include the invoice uuid so PayPalSuccessView can resolve the club's own
                # credentials (if any) before capturing -- the capture must use the same app
                # that created the order.
                "return_url": self.request.build_absolute_uri(
                    reverse("paypal_success")
                    + "?"
                    + urlencode(
                        {"invoice": str(invoice.no_login_link), **({"member_pk": member_pk} if member_pk else {})}
                    )
                ),
                "cancel_url": self.request.build_absolute_uri(
                    reverse("club_detail", kwargs={"slug": invoice.club.slug})
                    if invoice.club and not invoice.auction
                    else reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link})
                ),
            },
        }

        order_data = self.post_to_paypal("v2/checkout/orders", payload)
        approval_url = None
        for link in order_data.get("links") or []:
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break
        self.order_id = order_data.get("id", "")
        if not approval_url:
            logger.error("PayPal order creation failed (platform): %s, debug_id %s", order_data, self.paypal_debug)
        return approval_url

    def handle_order(self, order_id):
        """Capture a PayPal order and process it. Returns (error_str, invoice)."""
        order_data = self.post_to_paypal(f"v2/checkout/orders/{order_id}/capture", {})
        if order_data.get("status") != "COMPLETED":
            return (
                "PayPal payment has not yet been completed, please ask the auction administrator to manually confirm payment.",
                None,
            )
        return self._process_captured_order(order_data)

    def _process_captured_order(self, order_data):
        """Process an already-captured PayPal order. Returns (error_str, invoice).

        Accepts both PayPal API response data and webhook event resource data so
        that CHECKOUT.ORDER.COMPLETED webhook events can be handled without making
        a redundant capture API call.
        """
        purchase_unit = order_data.get("purchase_units", [{}])[0]
        invoice_id = purchase_unit.get("reference_id")

        # Load invoice
        invoice = Invoice.objects.filter(pk=invoice_id).first()
        if not invoice:
            return (
                "No invoice associated with this PayPal order, please ask the auction administrator to manually confirm payment.",
                None,
            )

        # Safely extract capture info (amount, currency, external id, payer info)
        capture = None
        try:
            capture = purchase_unit.get("payments", {}).get("captures", [None])[0]
        except Exception:
            capture = None

        amount_value = None
        currency = "USD"
        external_id = order_data.get("id")
        if capture:
            amount_value = capture.get("amount", {}).get("value")
            currency = capture.get("amount", {}).get("currency_code", currency)
            external_id = capture.get("id") or external_id

        # fallback to purchase_unit.amount
        if not amount_value:
            pu_amount = purchase_unit.get("amount", {}) or {}
            amount_value = pu_amount.get("value")
            currency = pu_amount.get("currency_code", currency)

        # payer info
        payer = order_data.get("payer", {}) or {}
        payer_name = None
        try:
            payer_name_parts = payer.get("name", {}) or {}
            given = payer_name_parts.get("given_name", "")
            surname = payer_name_parts.get("surname", "")
            payer_name = " ".join(p for p in (given, surname) if p).strip() or None
        except Exception:
            payer_name = None
        payer_email = payer.get("email_address")

        payer_address = None
        # prefer purchase_unit.shipping.address, fallback to payer.address
        shipping = purchase_unit.get("shipping", {}) or {}
        address_obj = (shipping.get("address") or {}) or (payer.get("address") or {})
        if address_obj:
            parts = []
            for k in (
                "address_line_1",
                "address_line_2",
                "admin_area_2",
                "admin_area_1",
                "postal_code",
                "country_code",
            ):
                v = address_obj.get(k)
                if v:
                    parts.append(v)
            if parts:
                payer_address = ", ".join(parts)

        if invoice.auctiontos_user:
            if payer_email and not invoice.auctiontos_user.email:
                invoice.auctiontos_user.email = payer_email
                invoice.auctiontos_user.save()
                if invoice.auction:
                    invoice.auction.create_history(
                        applies_to="USERS",
                        action=f"Added email {payer_email} to user {invoice.auctiontos_user.name} from PayPal payment",
                        user=None,
                    )
            if payer_address and payer_address != invoice.auctiontos_user.address:
                if invoice.auction:
                    invoice.auction.create_history(
                        applies_to="USERS",
                        action=f"Updated address for user {invoice.auctiontos_user.name} from PayPal payment.  Old address {invoice.auctiontos_user.address}",
                        user=None,
                    )
                invoice.auctiontos_user.address = payer_address[:500]
                invoice.auctiontos_user.save()
                if invoice.auctiontos_user.user and not invoice.auctiontos_user.user.userdata.address:
                    invoice.auctiontos_user.user.userdata.address = payer_address[:500]
                    invoice.auctiontos_user.user.userdata.save()

        amt = Decimal(str(amount_value)) if amount_value else Decimal("0.00")
        if not amt:
            return (
                "Unable to determine payment amount from PayPal order, please ask the auction administrator to manually confirm payment.",
                invoice,
            )
        payment, created = InvoicePayment.objects.update_or_create(
            external_id=external_id,
            defaults={
                "invoice": invoice,
                "amount": amt,
                "currency": currency,
                "payer_name": payer_name,
                "payer_email": payer_email,
                "payer_address": payer_address,
                "payment_method": "PayPal",
                "amount_available_to_refund": amt,
            },
        )
        try:
            invoice.recalculate()
        except Exception:
            logger.exception("recalculate failed for invoice %s after PayPal payment", invoice.pk)
        if created and invoice.auctiontos_user and invoice.auction:
            try:
                action = f"Payment received via PayPal for {invoice.auctiontos_user.name} ${payment.amount} ({payment.external_id})"
                invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
            except Exception:
                logger.exception("create_history failed for PayPal payment on invoice %s", invoice.pk)
        # If the total owed is zero or less and invoice is DRAFT/UNPAID, mark PAID. Use the rounded
        # balance so a rounded-down charge (the amount we actually billed) still settles the invoice.
        if invoice.rounded_net_after_payments >= 0 and invoice.status in ("DRAFT", "UNPAID"):
            if not invoice.renewal_needed:
                try:
                    _ensure_invoice_renewal_state(invoice)
                except Exception:
                    logger.exception("Failed to ensure renewal state for invoice %s before PayPal PAID", invoice.pk)
            invoice.status = "PAID"
            invoice.save()
            try:
                _process_invoice_membership_renewal(invoice, payment_method="PayPal", external_id=payment.external_id)
            except Exception:
                logger.exception("membership renewal failed after PayPal payment on invoice %s", invoice.pk)
            if invoice.auction and invoice.auctiontos_user:
                try:
                    invoice.auction.create_history(
                        applies_to="INVOICES",
                        action=f"Invoice {invoice.auctiontos_user.name} automatically marked PAID after PayPal payment",
                    )
                except Exception:
                    logger.exception("create_history failed after PayPal payment on invoice %s", invoice.pk)
            # I have given some thought to putting this in a model property instead
            # Putting it here only sends the message when an invoice is paid via PayPal
            if invoice.auction:
                try:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "invoice_paid", "pk": invoice.pk},
                    )
                except Exception:
                    logger.exception("Failed to send invoice_paid websocket for invoice %s (PayPal)", invoice.pk)

        return None, invoice

    def can_refund_invoice(self, invoice, amount):
        """Check if we can refund the given amount on this invoice via PayPal."""
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        # if multiple payments have been made, we will only refund the largest one
        # I am too lazy to implement partial refunds across multiple payments right now
        total_available = payment.amount_available_to_refund if payment else Decimal("0.00")
        if total_available >= amount:
            return True
        return False

    def refund_invoice(self, invoice, amount):
        """Refund the given amount on this invoice via PayPal.
        Returns error or none on success"""
        # Clubs using their own (non-OAuth) credentials have no webhook wired up, so an
        # automated refund here would never be recorded as a negative InvoicePayment
        # (handle_refund only runs from the PayPal webhook). Force these to be done manually
        # in the club's own PayPal account so our records never silently drift.
        if invoice.paypal_credentials:
            return (
                "Automatic refunds aren't available for this club's PayPal account. "
                "Please issue the refund manually in PayPal."
            )
        if not self.can_refund_invoice(invoice, amount):
            return "Unable to automatically refund payment"
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method="PayPal")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )
        payload = {"amount": {"value": str(amount), "currency_code": str(payment.currency)}}
        result = self.post_to_paypal(f"v2/payments/captures/{payment.external_id}/refund", payload)
        if result.get("status") != "COMPLETED":
            logger.exception("PayPal refund failed: %s, debug_id: %s", result, self.paypal_debug)
            return "PayPal refund failed"
        # no database recording happens here, that goes through the webhook, see handle_refund()
        return None

    def handle_refund(self, refund_resource):
        """
        Process a refund webhook resource:
          - find the capture id (payment reference) from resource.links where rel == 'up'
          - find the InvoicePayment with external_id == capture_id
          - create a new InvoicePayment with negative amount and external_id == refund_id
        Returns: (invoice, refund_payment) or (None, None) on failure
        """
        refund_id = refund_resource.get("id")
        note_to_payer = refund_resource.get("note_to_payer") or refund_resource.get("note") or ""
        amount_obj = refund_resource.get("amount") or {}
        amount_value = amount_obj.get("value")
        currency = amount_obj.get("currency_code") or amount_obj.get("currency")

        # find capture id from the `up` link in the resource
        capture_id = None
        for item in refund_resource.get("links") or []:
            if item.get("rel") == "up" and item.get("href"):
                try:
                    capture_id = urlparse(item["href"]).path.rstrip("/").split("/")[-1]
                except Exception:
                    capture_id = None
                break

        if not capture_id:
            logger.warning("Refund resource missing capture 'up' link; resource=%s", refund_resource)
            return None, None

        # Find the original InvoicePayment by external_id == capture_id
        original_payment = InvoicePayment.objects.filter(external_id=capture_id).first()
        if not original_payment:
            logger.warning("No InvoicePayment found for capture id %s (refund %s)", capture_id, refund_id)
            return None, None
        invoice = original_payment.invoice

        # parse amount as Decimal and make negative
        try:
            refund_amt = Decimal(str(amount_value)) if amount_value is not None else None
        except Exception:
            logger.exception("Invalid refund amount in resource: %s", amount_value)
            refund_amt = None

        if refund_amt is None:
            logger.warning("Refund amount missing or invalid in refund resource %s", refund_id)
            return invoice, None

        refund_amt_signed = -abs(refund_amt)  # ensure negative

        original_payment.amount_available_to_refund = original_payment.amount_available_to_refund - abs(refund_amt)
        original_payment.save()
        # Create a new InvoicePayment record for the refund.
        refund_payment, created = InvoicePayment.objects.update_or_create(
            external_id=refund_id,
            defaults={
                "invoice": invoice,
                "amount": refund_amt_signed,
                "currency": currency or original_payment.currency,
                "payer_name": (refund_resource.get("payer") or {}).get("name") or None,
                "payer_email": (refund_resource.get("payer") or {}).get("email_address") or None,
                "payer_address": None,
                "payment_method": "PayPal Refund",
                "memo": note_to_payer[:500],
            },
        )

        invoice.recalculate()
        action = f"Refund received via PayPal {refund_id} for capture {capture_id}: {refund_amt_signed} {currency}. Note: {note_to_payer}"
        invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
        return invoice, refund_payment


class PayPalConnectView(LoginRequiredMixin, PayPalAPIMixin, View):
    """Start the PayPal onboarding process for a seller"""

    def get(self, request):
        # PayPal must be enabled for this user before they can onboard a seller account.
        # The connect button is hidden in the UI when it isn't, but guard the endpoint too.
        if not request.user.userdata.paypal_enabled:
            messages.error(request, "PayPal isn't enabled for your account.")
            return redirect(reverse("home"))
        _stash_club_for_payment_oauth(request)
        tracking_id = request.user.userdata.unsubscribe_link
        payload = {
            "tracking_id": tracking_id,
            "operations": [
                {
                    "operation": "API_INTEGRATION",
                    "api_integration_preference": {
                        "rest_api_integration": {
                            "integration_method": "PAYPAL",
                            "integration_type": "THIRD_PARTY",
                            "third_party_details": {"features": ["PAYMENT", "REFUND", "ACCESS_MERCHANT_INFORMATION"]},
                        }
                    },
                }
            ],
            "products": ["EXPRESS_CHECKOUT"],
            "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
            # take us to PayPalCallbackView when we are done
            "partner_config_override": {
                "return_url": request.build_absolute_uri(reverse("paypal_callback")),
                # "return_url_description": f"Continue on {settings.NAVBAR_BRAND}",
            },
        }
        data = self.post_to_paypal("v2/customer/partner-referrals", payload)

        # Extract the action_url from the links list
        action_url = next((link["href"] for link in data.get("links", []) if link.get("rel") == "action_url"), None)
        if not action_url:
            logger.exception("PayPal onboarding failed %s, debug_id %s", data, self.paypal_debug)
            messages.error(request, "Unable to start PayPal onboarding process, please try again later.")
            return redirect(reverse("home"))
        # Redirect seller to PayPal to complete onboarding
        return redirect(action_url)


class PayPalCallbackView(LoginRequiredMixin, PayPalAPIMixin, View):
    """After onboarding, PayPal redirects here"""

    def get_success_url(self):
        # If the user started the connect flow from a club's membership settings page,
        # we already attached the seller to the club in self.get() — send them back there.
        if getattr(self, "linked_club", None):
            if self.error:
                messages.error(self.request, self.error)
            else:
                messages.success(
                    self.request,
                    f"PayPal account linked to {self.linked_club.name}. Members can now pay dues directly on this site.",
                )
            return redirect(reverse("club_membership_settings", kwargs={"slug": self.linked_club.slug}))
        success_url = reverse("home")
        if self.request.user.userdata.last_auction_created:
            success_url = self.request.user.userdata.last_auction_created.get_absolute_url()
        if self.error:
            messages.error(self.request, self.error)
        else:
            messages.success(
                self.request,
                "You're all set - PayPal account linked!  Your users will see a PayPal button on invoices.",
            )
            success_url += "?enable_online_payments=True"
        return redirect(success_url)

    def get(self, request):
        self.error = None
        self.valid = False
        self.linked_club = None
        tracking_id = request.GET.get("merchantId")
        merchant_id = request.GET.get("merchantIdInPayPal")
        partner_merchant_id = settings.PARTNER_MERCHANT_ID

        if not tracking_id or not merchant_id:
            self.error = "Missing ID from PayPal callback"
            return self.get_success_url()

        data = UserData.objects.filter(unsubscribe_link=tracking_id).first()
        if not data:
            self.error = "Could not find user for PayPal onboarding"
            return self.get_success_url()
        else:
            user = data.user

        # Validate that the tracking_id belongs to the currently authenticated user to
        # prevent cross-account linking (an attacker supplying another user's tracking_id).
        if user != request.user:
            logger.warning(
                "PayPal callback tracking_id mismatch: tracking_id belongs to user %s but request.user is %s",
                user.pk,
                request.user.pk,
            )
            self.error = "PayPal account does not match the logged-in user"
            return self.get_success_url()

        # Fetch referral info from PayPal
        merchant_info = self.get_from_paypal(
            f"v1/customer/partners/{partner_merchant_id}/merchant-integrations/{merchant_id}"
        )
        # Integration checklist: ensure payments_receivable, email confirmed and oauth_third_party present
        currency = merchant_info.get("primary_currency", "USD")
        if not merchant_info.get("payments_receivable"):
            self.error = "Attention: You currently cannot receive payments due to restriction on your PayPal account. Please resolve any issues with PayPal and re-link your account here."
            return self.get_success_url()
        if not merchant_info.get("primary_email_confirmed"):
            self.error = "Attention: Please confirm your email address on https://www.paypal.com/businessprofile/settings in order to receive payments! You currently cannot receive payments.  Re-link your account here when finished."
            return self.get_success_url()
        oauth_integrations = merchant_info.get("oauth_integrations") or []
        oauth_ok = any(
            oi.get("integration_type") == "OAUTH_THIRD_PARTY" and oi.get("oauth_third_party")
            for oi in oauth_integrations
        )
        if not oauth_ok:
            self.error = "It doesn't look like you've granted us the third-party oauth integrations permissions. Please re-link your PayPal account and be sure to accept all requested permissions."
            return self.get_success_url()

        # update model
        seller, _ = PayPalSeller.objects.get_or_create(user=user)
        seller.paypal_merchant_id = merchant_id
        seller.payer_email = merchant_info.get("primary_email") or seller.payer_email
        seller.currency = currency
        # If the connect flow originated from a club's settings page, link the seller to that club.
        club = _pop_club_for_payment_oauth(request)
        if club:
            # If another seller is already linked to this club, detach it first to honor the
            # OneToOneField uniqueness constraint.
            existing = PayPalSeller.objects.filter(club=club).exclude(pk=seller.pk).first()
            if existing:
                existing.club = None
                existing.save(update_fields=["club"])
                ClubHistory.objects.create(
                    club=club,
                    user=request.user,
                    action=f"Replaced PayPal account {existing.payer_email or existing.user}",
                    applies_to="SETTINGS",
                )
            seller.club = club
            self.linked_club = club
        seller.save()
        if club:
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Connected PayPal account {seller.payer_email or seller.user}",
                applies_to="SETTINGS",
            )
        return self.get_success_url()


def _club_membership_success_url(club, member_pk):
    """Pick the best post-payment URL for a club membership invoice.

    Prefers the member-number page (the canonical landing per product spec),
    falls back to the UUID page (works without a membership number), and
    finally to the club detail page.
    """
    if club and member_pk:
        member = ClubMember.objects.filter(pk=member_pk, club=club, is_deleted=False).first()
        if member and member.membership_number:
            return reverse("club_member_by_number", kwargs={"slug": club.slug, "number": member.membership_number})
        if member:
            return reverse("club_member_by_uuid", kwargs={"slug": club.slug, "uuid": member.uuid})
    if club:
        return reverse("club_detail", kwargs={"slug": club.slug})
    return None


class CreatePayPalOrderView(PayPalAPIMixin, View):
    """Create a PayPal order for an invoice and redirect to PayPal checkout"""

    def _invoice_error_redirect(self, invoice):
        if invoice.club:
            return redirect(reverse("club_membership_pay", kwargs={"slug": invoice.club.slug}))
        return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))

    def dispatch(self, request, *args, **kwargs):
        self.invoice = get_object_or_404(Invoice, no_login_link=kwargs.pop("uuid"))
        error = self.invoice.reason_for_payment_not_available
        if not self.invoice.show_paypal_button:
            error = "PayPal payments are not available"
        if error:
            messages.error(request, error)
            return self._invoice_error_redirect(self.invoice)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Create the order"""
        member_pk = request.POST.get("member_pk") or request.GET.get("member_pk") or ""
        try:
            approval_url = self.create_order(self.invoice, member_pk=member_pk)
        except (requests.RequestException, PayPalRequestError):
            messages.error(request, "Payment provider rejected the order. Please try again or contact the organizer.")
            return self._invoice_error_redirect(self.invoice)
        if not approval_url:
            messages.error(request, "Payment provider rejected the order. Please try again or contact the organizer.")
            return self._invoice_error_redirect(self.invoice)
        return redirect(approval_url)


class PayPalSuccessView(PayPalAPIMixin, View):
    """Capture PayPal order after approval and mark payment complete"""

    def get(self, request, *args, **kwargs):
        order_id = request.GET.get("token")
        # Resolve the club's own (non-OAuth) credentials before capturing -- only the app
        # that created the order can capture it. The invoice uuid is carried in the return URL
        # set by create_order(); the captured order's reference_id remains authoritative for
        # which invoice is actually credited.
        invoice_uuid = request.GET.get("invoice")
        if invoice_uuid:
            invoice_for_creds = Invoice.objects.filter(no_login_link=invoice_uuid).first()
            if invoice_for_creds:
                self.club_paypal_credentials = invoice_for_creds.paypal_credentials
        error, invoice = self.handle_order(order_id)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Payment completed successfully. Thank you!")
        member_pk = request.GET.get("member_pk") or ""
        if invoice and invoice.club:
            return redirect(_club_membership_success_url(invoice.club, member_pk))
        if invoice:
            return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))
        return redirect(reverse("home"))


class PayPalInfoView(TemplateView):
    template_name = "auctions/paypal_seller.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["seller"] = PayPalSeller.objects.filter(user=self.request.user).first()
            context["auction"] = self.request.user.userdata.last_auction_created
        else:
            context["seller"] = None
            context["auction"] = None
        return context


class PayPalSellerDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "auctions/paypal_seller_confirm_delete.html"
    model = PayPalSeller

    def get_object(self, queryset=None):
        return get_object_or_404(PayPalSeller, user=self.request.user)

    def get_success_url(self):
        return reverse("paypal_seller")


class SquareAPIMixin:
    """Mixin for Square payment link creation
    Delegates to SquareSeller model methods for Square API operations
    All operations require OAuth - no platform credentials"""

    def create_payment_link(self, invoice, member_pk=""):
        """Create a Square payment link using SquareSeller model methods
        Returns tuple: (payment_url, error_message)
        """
        from auctions.models import SquareSeller

        if invoice.club:
            seller = invoice.club.effective_square_seller
        elif invoice.auction:
            seller = SquareSeller.objects.filter(user=invoice.auction.created_by).first()
        else:
            seller = None
        if not seller:
            return None, "Seller has not connected their Square account"

        return seller.create_payment_link(invoice, self.request, member_pk=member_pk)


class SquareConnectView(LoginRequiredMixin, View):
    """Start the Square OAuth process for a seller"""

    def get(self, request):
        # Square must be enabled for this user before they can onboard a seller account.
        # The connect button is hidden in the UI when it isn't, but guard the endpoint too.
        if not request.user.userdata.square_enabled:
            messages.error(request, "Square isn't enabled for your account.")
            return redirect(reverse("home"))
        _stash_club_for_payment_oauth(request)
        # Build Square OAuth URL
        # Use the user's unsubscribe_link as state parameter for security
        state = request.user.userdata.unsubscribe_link

        # Square OAuth authorization endpoint - use SQUARE_ENVIRONMENT setting
        square_auth_url = (
            "https://connect.squareupsandbox.com/oauth2/authorize"
            if settings.SQUARE_ENVIRONMENT == "sandbox"
            else "https://connect.squareup.com/oauth2/authorize"
        )

        # Build redirect URI - must match what's configured in Square app and what we send in token exchange
        redirect_uri = request.build_absolute_uri(reverse("square_callback"))
        # Build OAuth parameters
        params = {
            "client_id": settings.SQUARE_APPLICATION_ID,
            "scope": " ".join(SQUARE_OAUTH_SCOPES),
            "state": state,
            # "session": "false",  # Don't require login if already logged in
            "redirect_uri": redirect_uri,
        }

        # Build redirect URL
        oauth_url = f"{square_auth_url}?{urlencode(params)}"
        return redirect(oauth_url)


class SquareCallbackView(LoginRequiredMixin, View):
    """After OAuth, Square redirects here
    Uses new Square SDK v42+ API"""

    def get(self, request):
        # Get authorization code and state from Square
        code = request.GET.get("code")
        state = request.GET.get("state")
        error = request.GET.get("error")
        error_description = request.GET.get("error_description")
        if error:
            messages.error(request, f"Square authorization failed: {error_description or error}")
            return redirect(reverse("square_seller"))

        if not code or not state:
            messages.error(request, "Missing authorization code from Square")
            return redirect(reverse("square_seller"))

        # Verify state matches user's unsubscribe_link for security
        if state != request.user.userdata.unsubscribe_link:
            messages.error(request, "Invalid state parameter - please try again")
            return redirect(reverse("square_seller"))

        # Exchange authorization code for access token
        try:
            from square import Square
            from square.client import SquareEnvironment

            # Determine environment
            env = (
                SquareEnvironment.SANDBOX if settings.SQUARE_ENVIRONMENT == "sandbox" else SquareEnvironment.PRODUCTION
            )
            # For OAuth token exchange, we don't need a token
            # Don't pass empty string as it causes "Illegal header value" error
            client = Square(environment=env)

            # Build redirect URI - must match what was sent in authorization request
            redirect_uri = request.build_absolute_uri(reverse("square_callback"))

            result = client.o_auth.obtain_token(
                client_id=settings.SQUARE_APPLICATION_ID,
                client_secret=settings.SQUARE_CLIENT_SECRET,
                code=code,
                grant_type="authorization_code",
                redirect_uri=redirect_uri,
            )
            # Successful response
            # New API returns response object directly (no is_error check needed, raises on error)
            # Extract token info from response
            access_token = result.access_token
            refresh_token = result.refresh_token if hasattr(result, "refresh_token") else None
            expires_at = result.expires_at if hasattr(result, "expires_at") else None
            merchant_id = result.merchant_id if hasattr(result, "merchant_id") else None

            if not access_token or not merchant_id:
                logger.error("Square OAuth token exchange failed: Missing required fields in response")
                messages.error(request, "Failed to connect Square account. Please try again.")
                return redirect(reverse("square_seller"))

            merchant_client = Square(
                environment=env,
                token=access_token,
            )

            list_resp = merchant_client.merchants.get("me")
            email = getattr(list_resp, "owner_email", None)
            currency = getattr(list_resp, "currency", "USD")

            # Save or update SquareSeller
            seller, created = SquareSeller.objects.get_or_create(user=request.user)
            seller.square_merchant_id = merchant_id
            seller.access_token = access_token
            seller.refresh_token = refresh_token
            # Record what this token was granted. Square OAuth is all-or-nothing for the requested
            # set, so the scopes we asked for are the scopes the merchant approved. This is what
            # supports_tap_to_pay reads, and reconnecting an old account refreshes it here.
            seller.scopes = " ".join(SQUARE_OAUTH_SCOPES)
            if expires_at:
                from datetime import datetime

                # Handle ISO 8601 format
                try:
                    seller.token_expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    # If expires_at is already a datetime object
                    if isinstance(expires_at, datetime):
                        seller.token_expires_at = expires_at
            seller.currency = currency
            seller.payer_email = email
            club = _pop_club_for_payment_oauth(request)
            if club:
                existing = SquareSeller.objects.filter(club=club).exclude(pk=seller.pk).first()
                if existing:
                    existing.club = None
                    existing.save(update_fields=["club"])
                    ClubHistory.objects.create(
                        club=club,
                        user=request.user,
                        action=f"Replaced Square account {existing.payer_email or existing.user}",
                        applies_to="SETTINGS",
                    )
                seller.club = club
            seller.save()
            if club:
                ClubHistory.objects.create(
                    club=club,
                    user=request.user,
                    action=f"Connected Square account {seller.payer_email or seller.user}",
                    applies_to="SETTINGS",
                )
                messages.success(
                    request,
                    f"Square account linked to {club.name}. Members can now pay dues directly on this site.",
                )
                return redirect(reverse("club_membership_settings", kwargs={"slug": club.slug}))

            messages.success(
                request,
                "You're all set - Square account linked! Your users will see a Square button on invoices.",
            )

            # Redirect to last auction or home
            if request.user.userdata.last_auction_created:
                return redirect(
                    request.user.userdata.last_auction_created.get_absolute_url() + "?enable_square_payments=True"
                )
            return redirect(reverse("square_seller"))

        except Exception as e:
            logger.exception("Error during Square OAuth: %s", e)
            # Provide more specific error message if it's an API error
            if hasattr(e, "body") and isinstance(e.body, dict):
                error_msg = e.body.get("message", str(e))
                error_type = e.body.get("type", "unknown")
                logger.error("Square OAuth API Error: type=%s, message=%s", error_type, error_msg)
                messages.error(
                    request, f"Square OAuth failed: {error_msg}. Please check your Square application settings."
                )
            else:
                messages.error(request, "An error occurred connecting your Square account. Please try again.")
            return redirect(reverse("square_seller"))


MAILCHIMP_OAUTH_CLUB_SESSION_KEY = "mailchimp_oauth_club_slug"


class MailchimpConnectView(LoginRequiredMixin, View):
    """Start the Mailchimp OAuth flow for a club (requires permission_edit_club)."""

    def get(self, request, slug):
        club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_edit_club"):
            raise PermissionDenied()
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        if not settings.MAILCHIMP_CLIENT_ID:
            messages.error(request, "Mailchimp is not configured on this site. Contact your site administrator.")
            return redirect(config_url)
        # Stash the club so the callback (which has no slug) knows what we're connecting.
        request.session[MAILCHIMP_OAUTH_CLUB_SESSION_KEY] = club.slug
        params = {
            "response_type": "code",
            "client_id": settings.MAILCHIMP_CLIENT_ID,
            "redirect_uri": request.build_absolute_uri(reverse("mailchimp_callback")),
            # Reuse the per-user unsubscribe UUID as the anti-CSRF state, same as Square.
            "state": request.user.userdata.unsubscribe_link,
        }
        return redirect("https://login.mailchimp.com/oauth2/authorize?" + urlencode(params))


class MailchimpCallbackView(LoginRequiredMixin, View):
    """Mailchimp redirects here after the user authorizes. Stores the token, then sends the
    admin back to the config page to pick an audience."""

    def get(self, request):
        from auctions import mailchimp as mc

        slug = request.session.get(MAILCHIMP_OAUTH_CLUB_SESSION_KEY)
        club = Club.objects.filter(slug=slug).first() if slug else None
        if not club or not check_club_permission(request.user, club, "permission_edit_club"):
            messages.error(request, "Your Mailchimp connection session expired. Please try again.")
            return redirect(reverse("home"))

        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        error = request.GET.get("error")
        if error:
            messages.error(request, f"Mailchimp authorization failed: {request.GET.get('error_description', error)}")
            return redirect(config_url)

        code = request.GET.get("code")
        state = request.GET.get("state")
        if not code or state != request.user.userdata.unsubscribe_link:
            messages.error(request, "Invalid Mailchimp authorization response. Please try again.")
            return redirect(config_url)

        try:
            token, dc = mc.exchange_oauth_code(code, request.build_absolute_uri(reverse("mailchimp_callback")))
        except mc.MailchimpError:
            logger.exception("Mailchimp token exchange failed for club %s", club.pk)
            messages.error(request, "Could not connect to Mailchimp. Please try again.")
            return redirect(config_url)

        club.mailchimp_access_token = token
        club.mailchimp_server_prefix = dc
        club.mailchimp_connected_on = timezone.now()
        club.mailchimp_connected_by = request.user
        if not club.mailchimp_webhook_secret:
            club.mailchimp_webhook_secret = secrets.token_urlsafe(32)
        club.save(
            update_fields=[
                "mailchimp_access_token",
                "mailchimp_server_prefix",
                "mailchimp_connected_on",
                "mailchimp_connected_by",
                "mailchimp_webhook_secret",
            ]
        )
        request.session.pop(MAILCHIMP_OAUTH_CLUB_SESSION_KEY, None)
        messages.success(request, "Mailchimp connected! Now choose which audience to sync your members into.")
        return redirect(config_url)


class MailchimpAudienceSelectView(LoginRequiredMixin, ClubViewMixin, View):
    """Pick an existing audience or create '{club} Members', then provision + backfill."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import mailchimp as mc

        club = self.club
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        client = mc.get_client(club)
        if not client:
            messages.error(request, "Mailchimp is not connected. Please connect first.")
            return redirect(config_url)

        choice = request.POST.get("audience_id", "")
        try:
            if choice == "__new__":
                from_email = club.contact_email or settings.DEFAULT_FROM_EMAIL
                contact = {
                    "company": club.name,
                    "address1": club.location or "",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "country": "US",
                }
                audience_id, audience_name = mc.create_audience(client, club, from_email, contact)
            else:
                audience_id = choice
                audience_name = next((a["name"] for a in mc.list_audiences(client) if a["id"] == choice), "")
        except Exception:
            logger.exception("Mailchimp audience selection failed for club %s", club.pk)
            messages.error(
                request,
                "Couldn't create a new Mailchimp audience (Mailchimp requires a mailing address). "
                "Create an audience in Mailchimp, then come back and pick it from the list.",
            )
            return redirect(config_url)

        if not audience_id:
            messages.error(request, "Please choose an audience.")
            return redirect(config_url)

        club.mailchimp_audience_id = audience_id
        club.mailchimp_audience_name = audience_name
        club.save(update_fields=["mailchimp_audience_id", "mailchimp_audience_name"])

        mc.ensure_merge_fields(club)
        mc.ensure_segments(club)
        mc.ensure_webhook(club)
        count = mc.backfill(club)

        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Connected Mailchimp audience '{audience_name}'",
            applies_to="SETTINGS",
        )
        messages.success(request, f"Syncing {count} member(s) into the '{audience_name}' Mailchimp audience.")
        return redirect(config_url)


class MailchimpSyncNowView(LoginRequiredMixin, ClubViewMixin, View):
    """Re-queue a sync for every in-scope member."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import mailchimp as mc

        club = self.club
        config_url = reverse("club_mailchimp_config", kwargs={"slug": club.slug})
        if not club.mailchimp_connected:
            messages.error(request, "Mailchimp is not connected.")
            return redirect(config_url)
        count = mc.backfill(club)
        messages.success(request, f"Queued {count} member(s) for syncing to Mailchimp.")
        return redirect(config_url)


class MailchimpDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Mailchimp connection. Leaves the audience itself untouched in Mailchimp."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        club = self.club
        club.mailchimp_access_token = None
        club.mailchimp_server_prefix = ""
        club.mailchimp_audience_id = ""
        club.mailchimp_audience_name = ""
        club.mailchimp_connected_on = None
        club.mailchimp_connected_by = None
        club.mailchimp_webhook_secret = ""
        club.mailchimp_last_error = ""
        club.save(
            update_fields=[
                "mailchimp_access_token",
                "mailchimp_server_prefix",
                "mailchimp_audience_id",
                "mailchimp_audience_name",
                "mailchimp_connected_on",
                "mailchimp_connected_by",
                "mailchimp_webhook_secret",
                "mailchimp_last_error",
            ]
        )
        ClubHistory.objects.create(club=club, user=request.user, action="Disconnected Mailchimp", applies_to="SETTINGS")
        messages.success(request, "Mailchimp disconnected.")
        return redirect(reverse("club_mailchimp_config", kwargs={"slug": club.slug}))


class ClubMailchimpConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Mailchimp settings/status panel for a club."""

    active_tab = "mailchimp"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        from auctions import mailchimp as mc
        from auctions.models import ClubMember

        club = self.club
        audiences = []
        # Connected (token) but no audience chosen yet -> offer the chooser.
        if club.mailchimp_access_token and not club.mailchimp_audience_id:
            client = mc.get_client(club)
            if client:
                try:
                    audiences = mc.list_audiences(client)
                except Exception:
                    logger.exception("Could not list Mailchimp audiences for club %s", club.pk)
                    messages.error(request, "Could not load your Mailchimp audiences. Try reconnecting.")

        synced = ClubMember.objects.filter(club=club, is_deleted=False)
        not_synced_count = (
            mc.in_scope_members(club).filter(mailchimp_last_synced__isnull=True).count()
            if club.mailchimp_audience_id
            else 0
        )
        has_emails_enabled = any(
            [
                club.send_welcome_email_to_new_members,
                club.send_membership_expiration_reminders_30_days,
                club.send_membership_expiration_reminders,
                club.send_membership_renewal_confirmation,
            ]
        )
        context = {
            "club": club,
            "view": self,
            "mailchimp_configured": bool(settings.MAILCHIMP_CLIENT_ID),
            "audiences": audiences,
            "in_scope_count": mc.in_scope_members(club).count(),
            "subscribed_count": synced.filter(mailchimp_status="subscribed").count(),
            "unsubscribed_count": synced.filter(mailchimp_status__in=["unsubscribed", "cleaned"]).count(),
            "not_synced_count": not_synced_count,
            "has_emails_enabled": has_emails_enabled,
            "tags": ClubMember.MAILCHIMP_TAGS,
        }
        return render(request, "auctions/club_mailchimp_settings.html", context)


class MailchimpWebhookView(View):
    """Receive Mailchimp unsubscribe/cleaned/upemail/profile callbacks.

    One-way sync means we only honor unsubscribe-style events here: we record the member's
    Mailchimp status so we never re-subscribe them, but we never touch their site email prefs.
    The shared secret lives in the URL path (Mailchimp does not sign webhooks).
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.mailchimp_webhook_secret or club.mailchimp_webhook_secret != secret:
            return None
        return club

    def get(self, request, slug, secret):
        # Mailchimp GETs the URL once to verify it when the webhook is created.
        if not self._get_club(slug, secret):
            return HttpResponseForbidden("invalid")
        return HttpResponse("ok")

    def post(self, request, slug, secret):
        from auctions.models import ClubMember

        club = self._get_club(slug, secret)
        if not club:
            return HttpResponseForbidden("invalid")

        event_type = request.POST.get("type")
        members = ClubMember.objects.filter(club=club, is_deleted=False)

        if event_type == "upemail":
            # Mailchimp sends old_email/new_email for address changes.
            old_email = request.POST.get("data[old_email]") or request.POST.get("data[email]")
            new_email = request.POST.get("data[new_email]")
            if old_email and new_email:
                # Reflect the new address locally; explicitly NOT a site-wide account change.
                members.filter(email__iexact=old_email).update(email=new_email)
            return HttpResponse("ok")

        email = request.POST.get("data[email]") or request.POST.get("data[email_address]")
        if email:
            members = members.filter(email__iexact=email)
        if event_type == "unsubscribe":
            members.update(mailchimp_status="unsubscribed")
        elif event_type == "cleaned":
            members.update(mailchimp_status="cleaned")
        # 'profile' events need no action under one-way sync.
        return HttpResponse("ok")


class ClubMemberSelfServiceView(View):
    """Public, UUID-keyed self-service email-preference links embedded in Mailchimp merge fields.

    These only change the member's *club* contact status; they never touch the user's site
    account or other clubs. ``action`` is set per-URL.
    """

    action = None  # "unsubscribe" | "resubscribe" | "nocomm"

    def get(self, request, slug, uuid):
        from auctions.models import ClubMember
        from auctions.tasks import sync_club_member_to_brevo, sync_club_member_to_mailchimp

        member = get_object_or_404(ClubMember, uuid=uuid, club__slug=slug, is_deleted=False)
        if self.action == "unsubscribe":
            member.contact_status = "non_essential"
            member.save(update_fields=["contact_status"])
            heading, body = "Unsubscribed", f"You will no longer receive marketing emails from {member.club.name}."
            history_action = f"{member} unsubscribed from marketing emails (self-service)"
        elif self.action == "resubscribe":
            member.contact_status = "contact"
            # Clear the remembered opt-out so the next sync actually re-subscribes them.
            member.mailchimp_status = ""
            member.brevo_status = ""
            member.save(update_fields=["contact_status", "mailchimp_status", "brevo_status"])
            heading, body = "Resubscribed", f"You will once again receive emails from {member.club.name}."
            history_action = f"{member} resubscribed to emails (self-service)"
        else:  # nocomm
            member.contact_status = "do_not_contact"
            member.save(update_fields=["contact_status"])
            heading, body = "Done", f"{member.club.name} will no longer contact you."
            history_action = f"{member} opted out of all contact (self-service)"
        ClubHistory.objects.create(
            club=member.club,
            user=None,
            action=history_action,
            applies_to="MEMBERS",
        )
        transaction.on_commit(lambda: sync_club_member_to_mailchimp.delay(member.pk))
        transaction.on_commit(lambda: sync_club_member_to_brevo.delay(member.pk))
        return render(
            request,
            "auctions/mailchimp_self_service.html",
            {"club": member.club, "heading": heading, "body": body, "member": member},
        )


# --- Brevo: built the same way as the Mailchimp views above (OAuth connect, list select,
# sync/disconnect, status page, and an inbound unsubscribe webhook). See auctions/brevo.py. ---


class BrevoConnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Store the club's Brevo API key (validated against Brevo) — the API-key analog of an OAuth
    connect, since Brevo's public OAuth program is private/org-scoped. The key is held encrypted."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        api_key = (request.POST.get("api_key") or "").strip()
        if not api_key:
            messages.error(request, "Please paste your Brevo API key.")
            return redirect(config_url)

        # Validate the key with a lightweight authenticated call before saving it.
        club.brevo_api_key = api_key
        try:
            brevo.list_contact_lists(brevo.get_client(club))
        except brevo.BrevoApiError as e:
            blocked_ip = brevo.blocked_ip_from_error(e)
            if blocked_ip is not None:
                # Valid-looking key, but Brevo is blocking this server's IP. Tell them what to allow.
                where = blocked_ip or brevo.outbound_ip() or "this server's IP address"
                logger.warning("Brevo blocked IP for club %s: %s", club.pk, e.detail)
                messages.error(
                    request,
                    "Your key looks valid, but Brevo is blocking this server's IP address. In Brevo, go to "
                    f"Settings → Security → Authorized IPs and add {where}, wait ~5 minutes, then try again.",
                )
            else:
                logger.warning("Brevo API key validation failed for club %s", club.pk)
                messages.error(request, "That Brevo API key didn't work. Double-check it and try again.")
            return redirect(config_url)
        except Exception:
            logger.exception("Brevo connect failed for club %s", club.pk)
            messages.error(request, "Couldn't reach Brevo right now. Please try again in a moment.")
            return redirect(config_url)

        club.brevo_connected_on = timezone.now()
        club.brevo_connected_by = request.user
        if not club.brevo_webhook_secret:
            club.brevo_webhook_secret = secrets.token_urlsafe(32)
        club.save(update_fields=["brevo_api_key", "brevo_connected_on", "brevo_connected_by", "brevo_webhook_secret"])
        ClubHistory.objects.create(club=club, user=request.user, action="Connected Brevo", applies_to="SETTINGS")
        messages.success(request, "Brevo connected! Now choose which list to sync your members into.")
        return redirect(config_url)


class BrevoListSelectView(LoginRequiredMixin, ClubViewMixin, View):
    """Pick an existing list or create '{club} Members', then provision + backfill."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        client = brevo.get_client(club)
        if not client:
            messages.error(request, "Brevo is not connected. Please connect first.")
            return redirect(config_url)

        choice = request.POST.get("list_id", "")
        try:
            if choice == "__new__":
                list_id, list_name = brevo.create_contact_list(client, club)
            else:
                list_id = choice
                list_name = next(
                    (lst["name"] for lst in brevo.list_contact_lists(client) if str(lst["id"]) == choice), ""
                )
        except (brevo.BrevoApiError, brevo.BrevoError):
            logger.exception("Brevo list selection failed for club %s", club.pk)
            messages.error(
                request, "Couldn't set up your Brevo list. Please try again, or create a list in Brevo first."
            )
            return redirect(config_url)

        if not list_id:
            messages.error(request, "Please choose a list.")
            return redirect(config_url)

        if not list_name:
            messages.error(request, "That list was not found in your Brevo account. Please choose a valid list.")
            return redirect(config_url)

        club.brevo_list_id = str(list_id)
        club.brevo_list_name = list_name
        club.save(update_fields=["brevo_list_id", "brevo_list_name"])

        brevo.ensure_attributes(club)
        brevo.ensure_webhook(club)
        count = brevo.backfill(club)

        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Connected Brevo list '{list_name}'",
            applies_to="SETTINGS",
        )
        messages.success(request, f"Syncing {count} member(s) into the '{list_name}' Brevo list.")
        return redirect(config_url)


class BrevoSyncNowView(LoginRequiredMixin, ClubViewMixin, View):
    """Re-queue a sync for every in-scope member."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        from auctions import brevo

        club = self.club
        config_url = reverse("club_brevo_config", kwargs={"slug": club.slug})
        if not club.brevo_connected:
            messages.error(request, "Brevo is not connected.")
            return redirect(config_url)
        count = brevo.backfill(club)
        messages.success(request, f"Queued {count} member(s) for syncing to Brevo.")
        return redirect(config_url)


class BrevoDisconnectView(LoginRequiredMixin, ClubViewMixin, View):
    """Forget the Brevo connection. Leaves the list itself untouched in Brevo."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        club = self.club
        club.brevo_api_key = None
        club.brevo_list_id = ""
        club.brevo_list_name = ""
        club.brevo_folder_id = ""
        club.brevo_connected_on = None
        club.brevo_connected_by = None
        club.brevo_webhook_secret = ""
        club.brevo_webhook_id = ""
        club.brevo_last_error = ""
        club.save(
            update_fields=[
                "brevo_api_key",
                "brevo_list_id",
                "brevo_list_name",
                "brevo_folder_id",
                "brevo_connected_on",
                "brevo_connected_by",
                "brevo_webhook_secret",
                "brevo_webhook_id",
                "brevo_last_error",
            ]
        )
        ClubHistory.objects.create(club=club, user=request.user, action="Disconnected Brevo", applies_to="SETTINGS")
        messages.success(request, "Brevo disconnected.")
        return redirect(reverse("club_brevo_config", kwargs={"slug": club.slug}))


class ClubBrevoConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Brevo settings/status panel for a club."""

    active_tab = "brevo"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug):
        from auctions import brevo
        from auctions.models import ClubMember

        club = self.club
        lists = []
        # Connected (API key) but no list chosen yet -> offer the chooser.
        if club.brevo_api_key and not club.brevo_list_id:
            client = brevo.get_client(club)
            if client:
                try:
                    lists = brevo.list_contact_lists(client)
                except (brevo.BrevoApiError, brevo.BrevoError):
                    logger.exception("Could not list Brevo lists for club %s", club.pk)
                    messages.error(request, "Could not load your Brevo lists. Try reconnecting.")

        synced = ClubMember.objects.filter(club=club, is_deleted=False)
        not_synced_count = (
            brevo.in_scope_members(club).filter(brevo_last_synced__isnull=True).count() if club.brevo_list_id else 0
        )
        has_emails_enabled = any(
            [
                club.send_welcome_email_to_new_members,
                club.send_membership_expiration_reminders_30_days,
                club.send_membership_expiration_reminders,
                club.send_membership_renewal_confirmation,
            ]
        )
        context = {
            "club": club,
            "view": self,
            "lists": lists,
            # Only looked up while showing the connect form, so admins can pre-authorize the IP.
            "server_ip": brevo.outbound_ip() if not club.brevo_api_key else "",
            "in_scope_count": brevo.in_scope_members(club).count(),
            "subscribed_count": synced.filter(brevo_status="subscribed").count(),
            "unsubscribed_count": synced.filter(brevo_status__in=["unsubscribed", "cleaned"]).count(),
            "not_synced_count": not_synced_count,
            "has_emails_enabled": has_emails_enabled,
            "tags": ClubMember.MAILCHIMP_TAGS,
        }
        return render(request, "auctions/club_brevo_settings.html", context)


class BrevoWebhookView(View):
    """Receive Brevo marketing unsubscribe/bounce/spam/delete callbacks.

    One-way sync means we only honor opt-out-style events here: we record the member's Brevo
    status so we never re-subscribe them, but we never touch their site email prefs. Brevo sends
    a JSON body and does not sign it, so (like Mailchimp) the shared secret lives in the URL path.
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def _get_club(self, slug, secret):
        club = Club.objects.filter(slug=slug).first()
        if not club or not club.brevo_webhook_secret or club.brevo_webhook_secret != secret:
            return None
        return club

    def get(self, request, slug, secret):
        if not self._get_club(slug, secret):
            return HttpResponseForbidden("invalid")
        return HttpResponse("ok")

    def post(self, request, slug, secret):
        from auctions.models import ClubMember

        club = self._get_club(slug, secret)
        if not club:
            return HttpResponseForbidden("invalid")

        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            return HttpResponse("ok")

        # Brevo's inbound event names use snake_case (unsubscribe / hard_bounce / contact_deleted),
        # unlike the camelCase used when registering the webhook. Normalize before matching.
        event = (payload.get("event") or "").lower().replace("_", "")
        email = payload.get("email")
        if not email:
            return HttpResponse("ok")
        members = ClubMember.objects.filter(club=club, is_deleted=False, email__iexact=email)

        if event in ("unsubscribe", "unsubscribed"):
            members.update(brevo_status="unsubscribed")
        elif event in ("hardbounce", "spam"):
            members.update(brevo_status="cleaned")
        elif event == "contactdeleted":
            members.update(brevo_status="archived")
        return HttpResponse("ok")


class CreateSquarePaymentLinkView(SquareAPIMixin, View):
    """Create a Square payment link for an invoice"""

    def _invoice_error_redirect(self, invoice):
        if invoice.club:
            return redirect(reverse("club_membership_pay", kwargs={"slug": invoice.club.slug}))
        return redirect(reverse("invoice_no_login", kwargs={"uuid": invoice.no_login_link}))

    def dispatch(self, request, *args, **kwargs):
        self.invoice = get_object_or_404(Invoice, no_login_link=kwargs.pop("uuid"))
        if not self.invoice.show_square_button:
            messages.error(request, "Square payments are not available for this invoice")
            return self._invoice_error_redirect(self.invoice)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Create the payment link"""
        member_pk = request.POST.get("member_pk") or request.GET.get("member_pk") or ""
        payment_url, error_message = self.create_payment_link(self.invoice, member_pk=member_pk)
        if not payment_url:
            messages.error(
                request, error_message or "Failed to create Square payment link. Please try again or contact support."
            )
            return self._invoice_error_redirect(self.invoice)

        # Add processing message and redirect to invoice to show status
        messages.info(
            request,
            "You'll see the payment confirmation on your invoice.  Payment generally confirms within a few minutes.",
        )
        return redirect(payment_url)


class SquareSuccessView(View):
    """Handle redirect after Square payment"""

    def get(self, request, *args, **kwargs):
        # Square payment link can include order_id or reference_id in query params
        # For now, show processing message and redirect to home
        # The webhook will update the invoice status
        messages.info(request, "Square payment processing... Your invoice will be updated shortly.")

        # Try to get invoice reference if available
        # Square may pass back custom data in query params depending on configuration
        return redirect(reverse("home"))


class SquareInfoView(TemplateView):
    template_name = "auctions/square_seller.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["seller"] = SquareSeller.objects.filter(user=self.request.user).first()
            context["auction"] = self.request.user.userdata.last_auction_created
        else:
            context["seller"] = None
            context["auction"] = None
        return context


class SquareSellerDeleteView(LoginRequiredMixin, DeleteView):
    template_name = "auctions/square_seller_confirm_delete.html"
    model = SquareSeller

    def get_object(self, queryset=None):
        return get_object_or_404(SquareSeller, user=self.request.user)

    def get_success_url(self):
        return reverse("square_seller")


class UserView(DetailView):
    """View information about a single user"""

    template_name = "user.html"
    model = User

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data"] = self.object.userdata
        try:
            context["banned"] = UserBan.objects.get(user=self.request.user.pk, banned_user=self.object.pk)
        except UserBan.DoesNotExist:
            context["banned"] = False
        context["seller_feedback"] = (
            context["data"].my_lots_qs.exclude(feedback_text__isnull=True).order_by("-date_posted")
        )
        context["buyer_feedback"] = (
            context["data"].my_won_lots_qs.exclude(winner_feedback_text__isnull=True).order_by("-date_posted")
        )
        return context


class UserByName(UserView):
    """/user/username storefront view"""

    def dispatch(self, request, *args, **kwargs):
        self.username = kwargs["slug"]
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(username=unquote(self.username))
        except User.DoesNotExist:
            pass
        # try:
        #     return User.objects.get(pk=self.username)
        # except:
        #     pass
        raise Http404


class UsernameUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_username.html"
    model = User
    success_message = "Username updated"
    form_class = ChangeUsernameForm

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(pk=self.request.user.pk)
        except User.DoesNotExist:
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            # "/users/" + str(self.kwargs['pk'])
            data["next"] = reverse("account")
        return data["next"]


class UserLabelPrefsView(UpdateView, SuccessMessageMixin):
    template_name = "user_labels.html"
    model = UserLabelPrefs
    success_message = "Printing preferences updated"
    form_class = UserLabelPrefsForm
    user_pk = None

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
        return data["next"]

    def get_object(self, *args, **kwargs):
        label_prefs, created = UserLabelPrefs.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        return label_prefs

    def _show_print_method(self):
        """The print-method dropdown only makes sense to someone who can use the app to print. Show
        it in the app, or on web if the user has ever registered a device (so they can pre-configure)."""
        return bool(self.request.is_mobile_app) or MobileDevice.objects.filter(user=self.request.user).exists()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["show_print_method"] = self._show_print_method()
        return kwargs

    def get_context_data(self, **kwargs):
        from auctions.printing import label_prefs_warnings, warning_matrix

        context = super().get_context_data(**kwargs)
        context["active_tab"] = "printing"
        prefs = self.object
        context["label_prefs"] = prefs
        context["show_print_method"] = self._show_print_method()
        context["warnings"] = label_prefs_warnings(prefs)
        # A plain dict; the template embeds it safely with |json_script for the live-warning JS.
        context["warning_map"] = warning_matrix()
        userData = self.request.user.userdata
        context["last_auction_used"] = userData.last_auction_used
        context["last_admin_auction"] = (
            Auction.objects.filter(
                Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                is_deleted=False,
            )
            .order_by("-date_start")
            .first()
        )
        return context


class UserPreferencesUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_preferences.html"
    model = UserData
    success_message = "User preferences updated"
    form_class = ChangeUserPreferencesForm
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        # self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
            # data['next'] = "/users/" + str(self.kwargs['pk'])
        return data["next"]

    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk)  # get the hack

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "preferences"
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class UserLocationUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_location.html"
    model = UserData
    success_message = "Contact info updated"
    form_class = UserLocation
    # such a hack...UserData and User do not have the same pks.
    # This means that if we go to /users/1/edit, we'll get the wrong UserData
    # The fix is to have a self.user_pk, which is set in dispatch and called in get_object
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        # self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
            # "/users/" + str(self.kwargs['pk'])
        return data["next"]

    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk)  # get the hack

    def get_initial(self):
        user = User.objects.get(pk=self.get_object().user.pk)
        return {"first_name": user.first_name, "last_name": user.last_name}

    def get_recent_auctiontos(self):
        """Get AuctionTOS records created in the last 30 days that are not manually added"""
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return AuctionTOS.objects.filter(
            user=self.request.user,
            manually_added=False,
            createdon__gte=thirty_days_ago,
        ).select_related("auction")

    def form_valid(self, form):
        userData = form.save(commit=False)
        user = User.objects.get(pk=self.get_object().user.pk)
        user.first_name = form.cleaned_data["first_name"]
        user.last_name = form.cleaned_data["last_name"]
        user.save()
        userData.last_activity = timezone.now()
        userData.save()

        # Update recent AuctionTOS records with new contact info
        new_name = f"{user.first_name} {user.last_name}"
        new_phone = userData.phone_number
        new_address = userData.address

        for tos in self.get_recent_auctiontos():
            # Track what changed
            changes = []
            if tos.name != new_name:
                changes.append(f"name from '{tos.name}' to '{new_name}'")
                tos.name = new_name
            if tos.phone_number != new_phone:
                changes.append(f"phone from '{tos.phone_number}' to '{new_phone}'")
                tos.phone_number = new_phone
            if tos.address != new_address:
                changes.append(f"address from '{tos.address}' to '{new_address}'")
                tos.address = new_address

            if changes:
                tos.save()
                # Create auction admin history
                AuctionHistory.objects.create(
                    auction=tos.auction,
                    user=user,
                    action=f"Updated contact info for {new_name}: " + ", ".join(changes),
                    applies_to="USERS",
                )

        for club_member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club"):
            changes = []
            if club_member.name != new_name:
                changes.append(f"name to '{new_name}'")
                club_member.name = new_name
            if club_member.phone_number != new_phone:
                changes.append(f"phone to '{new_phone}'")
                club_member.phone_number = new_phone
            if club_member.address != new_address:
                changes.append(f"address to '{new_address}'")
                club_member.address = new_address
            if changes:
                club_member.save()
                ClubHistory.objects.create(
                    club=club_member.club,
                    user=user,
                    action=f"Contact info updated for {user.get_full_name()}: " + ", ".join(changes),
                    applies_to="MEMBERS",
                )

        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "contact"

        # Add message about auctions that will be updated
        recent_auctiontos = self.get_recent_auctiontos()
        count = recent_auctiontos.count()
        if count == 1:
            tos = recent_auctiontos.first()
            context["auctiontos_update_message"] = f"Updating your contact info will also update it in {tos.auction}"
        elif count > 1:
            context["auctiontos_update_message"] = f"Updating your contact info will also update it in {count} auctions"

        club_memberships = ClubMember.objects.filter(user=self.request.user, is_deleted=False).select_related("club")
        club_count = club_memberships.count()
        if club_count == 1:
            club = club_memberships.first().club
            context["club_membership_message"] = (
                f"Updating your contact info will also update your contact info in the {club.name}"
            )
        elif club_count > 1:
            context["club_membership_message"] = (
                f"Updating your contact info will also update your contact info in {club_count} clubs"
            )

        return context


class UserChartView(APIView):
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied()
        user = kwargs.get("pk", None)
        allBids = (
            Bid.objects.exclude(is_deleted=True)
            .select_related("lot_number__species_category")
            .filter(user=user, lot_number__species_category__isnull=False)
        )
        pageViews = PageView.objects.select_related("lot_number__species_category").filter(
            user=user, lot_number__species_category__isnull=False
        )
        # This is extremely inefficient
        # Almost all of it could be done in SQL with a more complex join and a count
        # However, I keep changing attributes (views, view duration, bids) and sorting here
        # This code is also only run for admins (and async of page load), so the server load is pretty low

        categories = {}
        for item in allBids:
            category = str(item.lot_number.species_category)
            categories.setdefault(category, {"bids": 0, "views": 0})["bids"] += 1
        for item in pageViews:
            category = str(item.lot_number.species_category)
            categories.setdefault(category, {"bids": 0, "views": 0})["views"] += 1
        # sort the result
        sortedCategories = sorted(categories, key=lambda t: -categories[t]["views"])
        # sortedCategories = sorted(categories, key=lambda t: -categories[t]['bids'] )
        # format for chart.js
        labels = []
        bids = []
        views = []
        for item in sortedCategories:
            labels.append(item)
            bids.append(categories[item]["bids"])
            views.append(categories[item]["views"])
        return JsonResponse(data={"labels": labels, "bids": bids, "views": views})


class LotChartView(APIView):
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied()
        lot_number = kwargs.get("pk", None)
        queryset = (
            PageView.objects.filter(lot_number=lot_number)
            .exclude(user_id__isnull=True)
            .order_by("-total_time")
            .values("user_id", "total_time")[:20]
        )
        user_ids = [entry["user_id"] for entry in queryset]
        users_by_id = User.objects.in_bulk(user_ids)
        labels = []
        data = []
        for entry in queryset:
            labels.append(str(users_by_id.get(entry["user_id"], entry["user_id"])))
            data.append(int(entry["total_time"]))

        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class AdminErrorPage(AdminOnlyViewMixin, TemplateView):
    """A sanity check to make sure the 500 error emails are working as they should be"""

    template_name = "dashboard.html"

    def get(self, request, *args, **kwargs):
        return 1 / 0


class AdminSetupChecklistView(AdminOnlyViewMixin, TemplateView):
    template_name = "auctions/admin_setup_checklist.html"

    @staticmethod
    def _yes_no(value):
        return "True" if value else "False"

    def get_context_data(self, **kwargs):
        from urllib.parse import urlsplit

        from fishauctions._env import env_has_real_value

        context = super().get_context_data(**kwargs)

        server_ip = get_server_public_ip()
        if server_ip:
            domain_help = (
                "Register a domain name with a DNS provider, then create DNS records "
                f"(an A record) that point to this server's IP: <code>{server_ip}</code>"
            )
        else:
            domain_help = (
                "Register a domain name with a DNS provider, then create DNS records "
                "(an A record) that point to this server's public IP address."
            )

        # Build https://your-host from SITE_DOMAIN so the payment redirect/webhook
        # URLs below are copy-paste ready for this install.
        raw_domain = (settings.SITE_DOMAIN or "example.com").strip() or "example.com"
        site_host = urlsplit(raw_domain if "://" in raw_domain else f"//{raw_domain}").hostname or raw_domain
        base_url = f"https://{site_host}"

        email_configured = (
            settings.POST_OFFICE_EMAIL_BACKEND == "django_ses.SESBackend"
            and env_has_real_value(settings.AWS_ACCESS_KEY_ID)
            and env_has_real_value(settings.AWS_SECRET_ACCESS_KEY)
        ) or (env_has_real_value(settings.EMAIL_HOST_USER) and env_has_real_value(settings.EMAIL_HOST_PASSWORD))

        context["setup_items"] = [
            # -- Core setup -------------------------------------------------------
            {
                "section": "Core setup",
                "name": "Site domain",
                "configured": bool((settings.SITE_DOMAIN or "").strip() and settings.SITE_DOMAIN != "example.com"),
                "what_it_does": (
                    "Used in absolute URLs, routed email senders, and the production HTTPS certificate. "
                    "Enter just the hostname &mdash; no <code>https://</code> and no trailing slash."
                ),
                "where_to_get_it": domain_help,
                "snippets": [{"code": 'SITE_DOMAIN="example.com"'}],
            },
            {
                "section": "Core setup",
                "name": "Single club mode",
                # A preference, not a credential: either value is valid, so always "Done".
                "configured": True,
                "what_it_does": (
                    "On by default: the site runs as one club (named after <code>NAVBAR_BRAND</code>) with every user "
                    "auto-added as a member. Set to <code>False</code> only if you host multiple clubs on one install. "
                    f"Currently <strong>{'on' if getattr(settings, 'SINGLE_CLUB_MODE', False) else 'off'}</strong>."
                ),
                "snippets": [
                    {"code": f'SINGLE_CLUB_MODE="{self._yes_no(getattr(settings, "SINGLE_CLUB_MODE", False))}"'}
                ],
            },
            {
                "section": "Core setup",
                "name": "Site identity & branding",
                # A preference, not a credential: always "Done", the help text explains it.
                "configured": True,
                "what_it_does": (
                    "Branding shown across the site and on emails:"
                    "<ul class='mb-0'>"
                    "<li><code>NAVBAR_BRAND</code> &mdash; shown at the top of every page (also the single club's name).</li>"
                    "<li><code>COPYRIGHT_MESSAGE</code> &mdash; shown in the footer. HTML is allowed.</li>"
                    "<li><code>MAILING_ADDRESS</code> &mdash; your physical address, shown next to the unsubscribe link on promo emails (required by anti-spam law).</li>"
                    "<li><code>WEBSITE_FOCUS</code> &mdash; the plural, lowercase noun your site is about, e.g. <code>fish</code>, <code>birds</code>, <code>items</code>.</li>"
                    "<li><code>I_BRED_THIS_FISH_LABEL</code> &mdash; the label shown next to the &ldquo;breeder points&rdquo; checkbox.</li>"
                    "<li><code>WEEKLY_PROMO_MESSAGE</code> &mdash; extra text included in the weekly promotional email (plain text only; usually left blank).</li>"
                    "</ul>"
                ),
                "snippets": [
                    {
                        "code": (
                            f'NAVBAR_BRAND="{settings.NAVBAR_BRAND}"\n'
                            'COPYRIGHT_MESSAGE="Website copyright your club"\n'
                            'MAILING_ADDRESS="123 Your Street, Anytown, USA"\n'
                            f'WEBSITE_FOCUS="{settings.WEBSITE_FOCUS}"\n'
                            f'I_BRED_THIS_FISH_LABEL="{settings.I_BRED_THIS_FISH_LABEL}"\n'
                            'WEEKLY_PROMO_MESSAGE=""'
                        )
                    }
                ],
            },
            {
                "section": "Core setup",
                "name": "Who can create auctions, lots, and promotions",
                "configured": True,
                "what_it_does": (
                    "Controls what regular (non-admin) users can do and which optional pages appear. "
                    "The snippet below shows your current values."
                    "<ul class='mb-0'>"
                    "<li><code>ALLOW_USERS_TO_CREATE_AUCTIONS</code> &mdash; let users create club auctions. "
                    "False = Django admins only.</li>"
                    "<li><code>ALLOW_USERS_TO_CREATE_LOTS</code> &mdash; let newly created users create standalone lots "
                    "(not attached to an auction). Everyone can still add lots to club auctions.</li>"
                    "<li><code>USERS_ARE_TRUSTED_BY_DEFAULT</code> &mdash; trusted users can promote auctions, manage "
                    "payments, and send invoice notifications. Keep this False so an admin vets accounts first. A common "
                    "setup is <code>ALLOW_USERS_TO_CREATE_AUCTIONS=True</code> with <code>USERS_ARE_TRUSTED_BY_DEFAULT=False</code>.</li>"
                    "<li><code>UNTRUSTED_MESSAGE</code> &mdash; the message untrusted users see when they try to promote an auction.</li>"
                    "<li><code>ENABLE_PROMO_PAGE</code> &mdash; True shows a marketing landing page instead of the "
                    "auctions list as the home page.</li>"
                    "<li><code>ENABLE_CLUB_FINDER</code> &mdash; True adds the &ldquo;find a club&rdquo; map to the menu.</li>"
                    "<li><code>ENABLE_HELP</code> &mdash; True shows the in-auction help button and auction.fish tutorial videos.</li>"
                    "</ul>"
                ),
                "snippets": [
                    {
                        "code": (
                            f'ALLOW_USERS_TO_CREATE_AUCTIONS="{self._yes_no(settings.ALLOW_USERS_TO_CREATE_AUCTIONS)}"\n'
                            f'ALLOW_USERS_TO_CREATE_LOTS="{self._yes_no(settings.ALLOW_USERS_TO_CREATE_LOTS)}"\n'
                            f'USERS_ARE_TRUSTED_BY_DEFAULT="{self._yes_no(settings.USERS_ARE_TRUSTED_BY_DEFAULT)}"\n'
                            'UNTRUSTED_MESSAGE="You cannot currently promote auctions. Please contact the website administrator for access."\n'
                            f'ENABLE_PROMO_PAGE="{self._yes_no(settings.ENABLE_PROMO_PAGE)}"\n'
                            f'ENABLE_CLUB_FINDER="{self._yes_no(settings.ENABLE_CLUB_FINDER)}"\n'
                            f'ENABLE_HELP="{self._yes_no(settings.ENABLE_HELP)}"'
                        )
                    },
                    {
                        "label": (
                            "ALLOW_USERS_TO_CREATE_LOTS only affects new accounts. To toggle standalone-lot creation "
                            "for existing users, run (use off to disable):"
                        ),
                        "code": "docker exec -it django python3 manage.py change_standalone_lots on",
                    },
                ],
            },
            {
                "section": "Core setup",
                "name": "Email delivery",
                "configured": email_configured,
                "what_it_does": "Sends sign-in, invoice, and notification emails. Pick one of the two options below.",
                "snippets": [
                    {
                        "label": "Option A — Gmail (simplest). Turn on 2-step verification first, then create an app password.",
                        "code": (
                            'POST_OFFICE_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"\n'
                            'EMAIL_HOST="smtp.gmail.com"\n'
                            'EMAIL_PORT="587"\n'
                            'EMAIL_USE_TLS="True"\n'
                            'EMAIL_HOST_USER="you@gmail.com"\n'
                            'EMAIL_HOST_PASSWORD="your-16-character-app-password"\n'
                            'DEFAULT_FROM_EMAIL="Notifications <you@gmail.com>"'
                        ),
                    },
                    {
                        "label": (
                            "Option B — Amazon SES (recommended for production; also enables reply routing). "
                            "INBOUND_ROUTING_SECRET is a random secret you make up — it is this app's own value, not from AWS. "
                            "With SES, mail is sent from info@your-domain automatically (DEFAULT_FROM_EMAIL is ignored)."
                        ),
                        "code": (
                            'POST_OFFICE_EMAIL_BACKEND="django_ses.SESBackend"\n'
                            'AWS_ACCESS_KEY_ID="your-access-key"\n'
                            'AWS_SECRET_ACCESS_KEY="your-secret-key"\n'
                            'AWS_SES_REGION_NAME="us-east-1"\n'
                            'AWS_SES_REGION_ENDPOINT="email.us-east-1.amazonaws.com"\n'
                            'AWS_SES_CONFIGURATION_SET="your-configuration-set"\n'
                            'INBOUND_ROUTING_SECRET="a-long-random-secret"'
                        ),
                    },
                ],
                "links": [
                    {
                        "label": "Gmail: enable 2-step verification",
                        "url": "https://myaccount.google.com/signinoptions/two-step-verification",
                    },
                    {"label": "Gmail: create an app password", "url": "https://myaccount.google.com/apppasswords"},
                    {
                        "label": "Full SES setup guide (SES.md)",
                        "url": "https://github.com/iragm/fishauctions/blob/master/SES.md",
                    },
                ],
            },
            {
                "section": "Core setup",
                "name": "Terms of service",
                "configured": Path(settings.BASE_DIR / "tos.html").exists(),
                "what_it_does": (
                    "Your site needs a terms-of-service page at <code>/tos/</code>. Create a <code>tos.html</code> file "
                    "in the project root (next to your <code>.env</code>) with your terms."
                ),
                "snippets": [
                    {
                        "label": "Create the file in the project root and paste in your terms:",
                        "code": "nano tos.html",
                    }
                ],
            },
            # -- PayPal -----------------------------------------------------------
            {
                "section": "PayPal",
                "name": "PayPal",
                "hide_title": True,
                "configured": env_has_real_value(settings.PAYPAL_CLIENT_ID)
                and env_has_real_value(settings.PAYPAL_SECRET),
                "what_it_does": (
                    "Lets your club collect online payments via PayPal (use Square instead unless you have PayPal "
                    "platform-seller approval)."
                ),
                "where_to_get_it": (
                    "Create a Live REST app under <code>Apps &amp; Credentials</code>, copy its client ID and secret, then "
                    "set the return/webhook URLs below."
                ),
                "snippets": [
                    {"code": 'PAYPAL_CLIENT_ID="your-client-id"\nPAYPAL_SECRET="your-secret"'},
                    {
                        "label": "URLs to configure in your PayPal app",
                        "code": (
                            f"# Return URL:\n{base_url}/paypal/onboard/success/\n"
                            f"# Webhook URL (subscribe to order & payment events):\n{base_url}/paypal/webhook/"
                        ),
                    },
                    {
                        "label": (
                            "To let users connect their own PayPal accounts you need an approved platform partner BN "
                            "code from PayPal, then add these (PAYPAL_ENABLED_FOR_USERS defaults to False, which hides "
                            "the connect-PayPal button for new accounts):"
                        ),
                        "code": (
                            'PARTNER_MERCHANT_ID="your-partner-merchant-id"\n'
                            'PAYPAL_BN_CODE="your-bn-code"\n'
                            'PAYPAL_ENABLED_FOR_USERS="True"'
                        ),
                    },
                    {
                        "label": "Once your integration is tested, enable PayPal for existing accounts:",
                        "code": "docker exec -it django python3 manage.py change_paypal on",
                    },
                    {
                        "label": (
                            "To test against the PayPal sandbox, point the API at it (if unset, sandbox is used in "
                            "development and live in production):"
                        ),
                        "code": 'PAYPAL_API_BASE="https://api-m.sandbox.paypal.com"',
                    },
                ],
                "links": [
                    {
                        "label": "PayPal developer dashboard (get API keys)",
                        "url": "https://developer.paypal.com/dashboard/applications/live",
                    },
                ],
            },
            # -- Square -----------------------------------------------------------
            {
                "section": "Square",
                "name": "Square",
                "hide_title": True,
                "configured": env_has_real_value(settings.SQUARE_APPLICATION_ID)
                and env_has_real_value(settings.SQUARE_CLIENT_SECRET),
                "what_it_does": (
                    "Lets sellers connect Square accounts to collect online payments &mdash; the recommended option. "
                    "Set <code>SQUARE_ENVIRONMENT=production</code> for live payments (blank uses the sandbox)."
                ),
                "where_to_get_it": (
                    "Create an app, then copy the Application ID, OAuth secret, and webhook signature key, and set the "
                    "redirect/webhook URLs below."
                ),
                "snippets": [
                    {
                        "code": (
                            'SQUARE_ENABLED_FOR_USERS="True"\n'
                            'SQUARE_ENVIRONMENT="production"\n'
                            'SQUARE_APPLICATION_ID="sq0idp-xxxxx"\n'
                            'SQUARE_CLIENT_SECRET="sq0csp-xxxxx"\n'
                            'SQUARE_WEBHOOK_SIGNATURE_KEY="your-webhook-key"\n'
                            'FIELD_ENCRYPTION_KEY="your-fernet-key"'
                        )
                    },
                    {
                        "label": "URLs to configure in your Square app",
                        "code": (
                            f"# OAuth redirect URL:\n{base_url}/square/onboard/success/\n"
                            f"# Webhook subscription URL:\n{base_url}/square/webhook/"
                        ),
                    },
                    {
                        "label": "To enable Square for existing users (new users follow SQUARE_ENABLED_FOR_USERS):",
                        "code": "docker exec -it django python3 manage.py change_square on",
                    },
                ],
                "links": [
                    {
                        "label": "Square developer dashboard (get API keys)",
                        "url": "https://developer.squareup.com/apps",
                    },
                ],
            },
            # -- Google Maps ------------------------------------------------------
            {
                "section": "Google Maps",
                "name": "Google Maps",
                "hide_title": True,
                "configured": getattr(settings, "GOOGLE_MAPS_ENABLED", False),
                "what_it_does": "Enables maps on auction and club pages plus location pickers.",
                "where_to_get_it": (
                    "Enable the <code>Maps JavaScript API</code> in Google Cloud and create the key(s) below."
                ),
                "snippets": [
                    {
                        "label": "Browser key — shows the maps and location pickers. Restrict it to your domain (include your port if it isn't 80).",
                        "code": 'GOOGLE_MAPS_API_KEY="your-browser-key"',
                    },
                    {
                        "label": "Server key (optional) — only needed to geocode club member addresses onto the member map. Restrict it to the Geocoding API.",
                        "code": 'GOOGLE_MAPS_SERVER_API_KEY="your-server-key"',
                    },
                ],
                "links": [
                    {
                        "label": "Google Cloud — Maps API keys",
                        "url": "https://console.cloud.google.com/google/maps-apis/credentials",
                    },
                ],
            },
            # -- Google sign-in ---------------------------------------------------
            {
                "section": "Google sign-in",
                "name": "Google sign-in on the website",
                "configured": env_has_real_value(settings.GOOGLE_OAUTH_LINK),
                "what_it_does": (
                    "Adds one-click Google sign-in to the website. The button stays hidden until a real "
                    "client ID is set, so a placeholder degrades gracefully."
                ),
                "where_to_get_it": (
                    "Create an OAuth web application and copy its client ID (ends in "
                    "<code>.apps.googleusercontent.com</code>). Add your site to the authorized origins."
                ),
                "snippets": [{"code": 'GOOGLE_OAUTH_LINK="your-client-id.apps.googleusercontent.com"'}],
                "links": [
                    {
                        "label": "Google Cloud — OAuth credentials",
                        "url": "https://console.cloud.google.com/apis/credentials",
                    },
                    {
                        "label": "Setup guide (django-allauth)",
                        "url": "https://docs.allauth.org/en/latest/socialaccount/providers/google.html",
                    },
                ],
            },
            {
                "section": "Google sign-in",
                "name": "Google sign-in in the mobile app",
                "configured": env_has_real_value(settings.GOOGLE_OAUTH_CLIENT_ID),
                "what_it_does": (
                    "Adds &ldquo;Continue with Google&rdquo; to the mobile app's login screen (separate from the "
                    "website button, since Google blocks OAuth in WebViews)."
                ),
                "where_to_get_it": (
                    "Reuse the website's Web-application OAuth client ID, and create an Android OAuth client for each app "
                    "package name (no secret, just has to exist in the project)."
                ),
                "snippets": [{"code": 'GOOGLE_OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"'}],
                "links": [
                    {
                        "label": "Google Cloud — OAuth credentials",
                        "url": "https://console.cloud.google.com/apis/credentials",
                    },
                ],
            },
            # -- reCAPTCHA --------------------------------------------------------
            {
                "section": "reCAPTCHA",
                "name": "reCAPTCHA",
                "hide_title": True,
                "configured": getattr(settings, "RECAPTCHA_ENABLED", False),
                "what_it_does": "Protects signup and password reset forms from abuse.",
                "where_to_get_it": (
                    "Register a site of type <strong>reCAPTCHA v2 &ldquo;Invisible&rdquo;</strong> (other types won't "
                    "work here) and copy the site key and secret key."
                ),
                "snippets": [{"code": 'RECAPTCHA_PUBLIC_KEY="your-site-key"\nRECAPTCHA_PRIVATE_KEY="your-secret-key"'}],
                "links": [
                    {"label": "reCAPTCHA admin console (get keys)", "url": "https://www.google.com/recaptcha/admin"},
                ],
            },
            # -- Analytics & ads --------------------------------------------------
            {
                "section": "Analytics & ads",
                "name": "Analytics & ads",
                "hide_title": True,
                "configured": env_has_real_value(settings.GOOGLE_MEASUREMENT_ID)
                or env_has_real_value(settings.GOOGLE_TAG_ID)
                or env_has_real_value(settings.GOOGLE_ADSENSE_ID),
                "what_it_does": (
                    "Optional Google Analytics (<code>GOOGLE_MEASUREMENT_ID</code>), Tag Manager "
                    "(<code>GOOGLE_TAG_ID</code>), and AdSense (<code>GOOGLE_ADSENSE_ID</code>); "
                    "<code>SHOW_ADS</code> is the master ad on/off switch."
                ),
                "snippets": [
                    {
                        "code": (
                            'GOOGLE_MEASUREMENT_ID="G-XXXXXXXXXX"\n'
                            'GOOGLE_TAG_ID="GTM-XXXXXXX"\n'
                            'GOOGLE_ADSENSE_ID="ca-pub-XXXXXXXXXXXXXXXX"\n'
                            f'SHOW_ADS="{self._yes_no(settings.SHOW_ADS)}"'
                        )
                    }
                ],
                "links": [
                    {"label": "Google Analytics", "url": "https://analytics.google.com/"},
                    {"label": "Google Tag Manager", "url": "https://tagmanager.google.com/"},
                    {"label": "Google AdSense", "url": "https://www.google.com/adsense/"},
                ],
            },
            # -- Mailchimp --------------------------------------------------------
            {
                "section": "Mailchimp",
                "name": "Mailchimp",
                "hide_title": True,
                "configured": env_has_real_value(settings.MAILCHIMP_CLIENT_ID)
                and env_has_real_value(settings.MAILCHIMP_CLIENT_SECRET),
                "what_it_does": (
                    "Lets clubs connect a Mailchimp account to sync members to an audience. "
                    "Each club connects its own account from its club settings page once these keys are set."
                ),
                "where_to_get_it": "Register an OAuth2 app and copy its client ID and secret.",
                "snippets": [
                    {"code": 'MAILCHIMP_CLIENT_ID="your-client-id"\nMAILCHIMP_CLIENT_SECRET="your-client-secret"'}
                ],
                "links": [
                    {"label": "Register a Mailchimp OAuth app", "url": "https://admin.mailchimp.com/account/oauth2/"},
                ],
            },
            # -- Discord ----------------------------------------------------------
            {
                "section": "Discord bot",
                "name": "Discord bot",
                "hide_title": True,
                "configured": env_has_real_value(settings.DISCORD_BOT_TOKEN)
                and env_has_real_value(getattr(settings, "DISCORD_PUBLIC_KEY", ""))
                and env_has_real_value(getattr(settings, "DISCORD_BOT_CLIENT_ID", "")),
                "what_it_does": (
                    "Lets members join a Discord server and automatically receive a role and club membership. "
                    "The server keys are set here; each club then links its server from its Discord settings page. "
                    "Discord needs a public HTTPS URL, so this won't work on localhost."
                ),
                "where_to_get_it": (
                    "Create an application with a bot (enable the <code>Server Members Intent</code>), then copy the "
                    "token, public key, and client ID."
                ),
                "setup_steps": [
                    "At the Discord developer portal, create an application and add a bot.",
                    "Under the bot settings, enable the <strong>Server Members Intent</strong>.",
                    "Copy the bot token, public key, and client ID into the <code>.env</code> snippet below.",
                    (
                        "In the application's <strong>General Information</strong> tab, set the "
                        f"<strong>Interactions Endpoint URL</strong> to <code>{base_url}/discord/interactions/</code>."
                    ),
                    (
                        "When adding the bot to a server it needs <strong>Manage Roles</strong> and <strong>Send "
                        "Messages</strong>, and its role must sit <strong>above</strong> any role it assigns in the "
                        "server's role hierarchy."
                    ),
                    (
                        "Register the <code>/connect</code> slash command once (see the command below). Global "
                        "registration can take up to an hour to propagate."
                    ),
                    (
                        "Each club admin then links a server from <code>/clubs/&lt;slug&gt;/discord/</code>: run "
                        "<code>/connect club_uuid:&lt;uuid&gt;</code> in the channel where the join button should appear, "
                        "then use <strong>Fetch roles</strong> to sync role names and configure paid/unpaid roles and "
                        "BAP/HAP thresholds."
                    ),
                ],
                "snippets": [
                    {
                        "code": (
                            'DISCORD_PUBLIC_KEY="your-application-public-key"\n'
                            'DISCORD_BOT_TOKEN="your-bot-token"\n'
                            'DISCORD_BOT_CLIENT_ID="your-application-client-id"'
                        )
                    },
                    {
                        "label": "Register the /connect slash command (run once after the keys are set):",
                        "code": "docker exec -it django python3 manage.py register_discord_commands",
                    },
                ],
                "links": [
                    {"label": "Discord developer portal", "url": "https://discord.com/developers/applications"},
                ],
            },
            # -- Google Wallet ----------------------------------------------------
            {
                "section": "Google Wallet membership cards",
                "name": "Google Wallet membership cards",
                "hide_title": True,
                "configured": bool(
                    settings.GOOGLE_WALLET_ISSUER_ID
                    and settings.GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL
                    and settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY
                ),
                "what_it_does": (
                    "Adds an &ldquo;Add to Google Wallet&rdquo; button on member cards so members can save their "
                    "membership card to their phone; anyone with a member's UUID link can add it."
                ),
                "where_to_get_it": (
                    "Get a Wallet Issuer ID from the issuer console, then drop the Google Cloud service-account key JSON "
                    "next to your <code>.env</code> and set the two vars below."
                ),
                "snippets": [
                    {"code": 'GOOGLE_WALLET_ISSUER_ID="issuer-id"\nGOOGLE_WALLET_KEYFILE="google-wallet-key.json"'},
                ],
                "links": [
                    {"label": "Google Wallet issuer console", "url": "https://pay.google.com/business/console"},
                ],
            },
            # -- Apple Wallet -----------------------------------------------------
            {
                "section": "Apple Wallet membership cards",
                "name": "Apple Wallet membership cards",
                "hide_title": True,
                "configured": bool(
                    settings.APPLE_WALLET_CERT_FILE
                    and settings.APPLE_WALLET_WWDR_FILE
                    and settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER
                    and settings.APPLE_WALLET_TEAM_IDENTIFIER
                ),
                "what_it_does": (
                    "Adds an &ldquo;Add to Apple Wallet&rdquo; button next to the Google Wallet one; anyone with a "
                    "member's UUID link can add it. Requires a paid Apple Developer account ($99/yr)."
                ),
                "where_to_get_it": (
                    "Create a Pass Type ID and certificate, download the Apple WWDR cert, and drop the <code>.p12</code> "
                    "and <code>.pem</code> next to your <code>.env</code>, then set the vars below."
                ),
                "snippets": [
                    {
                        "code": (
                            'APPLE_WALLET_CERT_FILE="pass-type-id.p12"\n'
                            'APPLE_WALLET_CERT_PASSWORD="your-p12-password"\n'
                            'APPLE_WALLET_WWDR_FILE="AppleWWDRCAG4.pem"\n'
                            'APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership"\n'
                            'APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345"\n'
                            'APPLE_WALLET_ORGANIZATION_NAME="Your Club"'
                        )
                    }
                ],
                "links": [
                    {
                        "label": "Apple Developer — Pass Type IDs",
                        "url": "https://developer.apple.com/account/resources/identifiers/list/passTypeId",
                    },
                    {"label": "Apple PKI — WWDR certificate", "url": "https://www.apple.com/certificateauthority/"},
                ],
            },
        ]
        return context


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
                .exclude(ip_address="", ip_address__isnull=True)
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
        # view_qs = PageView.objects.exclude(latitude=0)
        qs = User.objects.filter(userdata__latitude__isnull=False, is_active=True).annotate(
            lots_sold=Count("lot"), lots_bought=Count("winner")
        )
        if view == "club" and filter1:
            # Users from a club
            qs = qs.filter(userdata__club__name=filter1)
        elif view == "buyers_and_sellers" and filter1:
            # Users who sold and bought
            qs = qs.filter(lots_sold__gte=filter1, lots_bought__gte=filter1)
        elif view == "volume" and filter1:
            # users by top volume_percentile
            qs = qs.filter(userdata__volume_percentile__lte=filter1)
        elif view == "recent" and filter1:
            # view_qs = view_qs.filter(date_start__gte=timezone.now() - timedelta(hours=int(filter1)))
            qs = qs.filter(userdata__last_activity__gte=timezone.now() - timedelta(hours=int(filter1)))
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
        from auctions.tasks import compute_user_flow_all

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


class ClubMap(AdminEmailMixin, TemplateView):
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


class IgnoreCategoriesView(TemplateView):
    template_name = "ignore_categories.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "ignore"
        return context


class CreateUserIgnoreCategory(APIView):
    """Add category with given pk to ignore list"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to ignore categories")
            return redirect(reverse("home"))
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        result, created = UserIgnoreCategory.objects.update_or_create(category=category, user=request.user)
        return JsonResponse(data={"pk": result.pk})


class DeleteUserIgnoreCategory(APIView):
    """Allow users to see lots in a given category again."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to show categories")
            return redirect(reverse("home"))
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        try:
            exists = UserIgnoreCategory.objects.get(category=category, user=request.user)
            exists.delete()
            return JsonResponse(data={"result": "deleted"})
        except UserIgnoreCategory.DoesNotExist:
            return JsonResponse(data={"error": "Category was not ignored."}, status=404)
        except Exception:
            logger.exception("Failed deleting ignored category for user %s", request.user.pk)
            return JsonResponse(data={"error": "Unable to update ignored categories."}, status=500)


class GetUserIgnoreCategory(APIView):
    """Get a list of all user ignore categories for the request user"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        categories = Category.objects.all().order_by("name")
        results = []
        for category in categories:
            item = {
                "id": category.pk,
                "text": category.name,
            }
            try:
                UserIgnoreCategory.objects.get(user=request.user, category=category.pk)
                item["selected"] = True
            except UserIgnoreCategory.DoesNotExist:
                pass
            results.append(item)
        return JsonResponse({"results": results}, safe=False)


class BlogPostView(DetailView):
    """Render a blog post"""

    model = BlogPost
    template_name = "blog_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        blogpost = self.get_object()
        # this is to allow the chart# syntax
        context["formatted_contents"] = re.sub(r"chart\d", r"<canvas id=\g<0>></canvas>", blogpost.body_rendered)
        return context


class UnsubscribeView(TemplateView):
    """
    Match a UUID in the URL to a UserData, and unsubscribe that user
    """

    template_name = "unsubscribe.html"

    def get_context_data(self, **kwargs):
        userData = UserData.objects.filter(unsubscribe_link=kwargs["slug"]).first()
        if not userData:
            raise Http404
        else:
            userData.unsubscribe_from_all()
        context = super().get_context_data(**kwargs)
        return context


class AuctionChartView(View, AuctionStatsPermissionsMixin):
    """GET methods for generating auction charts"""

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)


class AuctionFunnelChartData(AuctionChartView):
    """
    Inverted funnel chart showing user participation
    """

    def get(self, *args, **kwargs):
        all_views = PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
        anonymous_views = all_views.values("session_id").annotate(session_count=Count("session_id")).count()
        user_views = all_views.values("user").annotate(user_count=Count("user")).count()
        total_views = anonymous_views + user_views
        total_bidders = User.objects.filter(bid__lot_number__auction=self.auction).annotate(dcount=Count("id")).count()
        total_winners = User.objects.filter(winner__auction=self.auction).annotate(dcount=Count("id")).count()
        labels = [
            "Total unique views",
            "Views from users with accounts",
            "Users who bid on at least one item",
            "Users who won at least one item",
        ]
        data = [
            total_views,
            user_views,
            total_bidders,
            total_winners,
        ]
        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class AuctionLotBiddersChartData(AuctionChartView):
    """How many bidders were there per lot?"""

    def get(self, *args, **kwargs):
        lots = self.auction.lots_qs
        labels = [
            "Not sold",
            "Lots with bids from 1 user",
            "Lots with bids from 2 users",
            "Lots with bids from 3 users",
            "Lots with bids from 4 users",
            "Lots with bids from 5 users",
            "Lots with bids from 6 or more users",
        ]
        data = [0, 0, 0, 0, 0, 0, 0]
        for lot in lots:
            if not lot.winning_price:
                data[0] += 1
            else:
                bids = len(Bid.objects.exclude(is_deleted=True).filter(lot_number=lot))
                if bids > 6:
                    bids = 6
                else:
                    data[bids] += 1
        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class AuctionCategoriesChartData(AuctionChartView):
    """Categories by views and lots sold"""

    number_of_categories_to_show = 20

    def process_stat(self, n, d):
        """Divide and catch div/0, round result"""
        if n is not None and d:
            result = round(((n / d) * 100), 2)
        else:
            result = 0
        return result

    def get(self, *args, **kwargs):
        labels = []
        views = []
        bids = []
        lots = []
        volumes = []
        categories = (
            Category.objects.filter(lot__auction=self.auction).annotate(num_lots=Count("lot")).order_by("-num_lots")
        )
        lot_count = self.auction.lots_qs.count()
        allViews = PageView.objects.filter(lot_number__auction=self.auction).count()
        allBids = Bid.objects.exclude(is_deleted=True).filter(lot_number__auction=self.auction).count()
        allVolume = (
            Lot.objects.exclude(is_deleted=True)
            .filter(auction=self.auction)
            .aggregate(Sum("winning_price"))["winning_price__sum"]
        )
        if lot_count:
            for category in categories[: self.number_of_categories_to_show]:
                labels.append(str(category))
                thisViews = PageView.objects.filter(
                    lot_number__auction=self.auction,
                    lot_number__species_category=category,
                ).count()
                thisBids = (
                    Bid.objects.exclude(is_deleted=True)
                    .filter(
                        lot_number__auction=self.auction,
                        lot_number__species_category=category,
                    )
                    .count()
                )
                thisVolume = (
                    Lot.objects.exclude(is_deleted=True)
                    .filter(auction=self.auction, species_category=category)
                    .aggregate(Sum("winning_price"))["winning_price__sum"]
                )
                percentOfLots = self.process_stat(category.num_lots, lot_count)
                percentOfViews = self.process_stat(thisViews, allViews)
                percentOfBids = self.process_stat(thisBids, allBids)
                percentOfVolume = self.process_stat(thisVolume, allVolume)
                lots.append(percentOfLots)
                views.append(percentOfViews)
                bids.append(percentOfBids)
                volumes.append(percentOfVolume)
        return JsonResponse(
            data={
                "labels": labels,
                "lots": lots,
                "views": views,
                "bids": bids,
                "volumes": volumes,
            }
        )


class AuctionStatsActivityJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    # these will no doubt need to be tweaked, perhaps differnt for in-person and online auctions?
    bins = 21
    days_before = 16
    days_after = bins - days_before
    dates_messed_with = False

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        if self.auction.is_online:
            self.date_start = self.auction.date_end - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_end + timezone.timedelta(days=self.days_after)
        else:  # in person
            self.date_start = self.auction.date_start - timezone.timedelta(days=self.days_before)
            self.date_end = self.auction.date_start + timezone.timedelta(days=self.days_after)
        # if date_end is in the future, shift the graph to show the same range, but for the present
        if self.date_end > timezone.now():
            time_difference = self.date_end - self.date_start
            self.date_end = timezone.now()
            self.date_start = self.date_end - time_difference
            self.dates_messed_with = True
        # self.bin_size = (self.date_end - self.date_start).total_seconds() / self.bins
        # self.bin_edges = [self.date_start + timezone.timedelta(seconds=self.bin_size * i) for i in range(self.bins + 1)]
        return super().dispatch(request, *args, **kwargs)

    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            return self.auction.cached_stats["activity"]["labels"]

        # Fallback to original calculation
        if self.dates_messed_with:
            return [(f"{i - 1} days ago") for i in range(self.bins, 0, -1)]
        before = [(f"{i} days before") for i in range(self.days_before, 0, -1)]
        after = [(f"{i} days after") for i in range(1, self.days_after)]
        midpoint = "start"
        if self.auction.is_online:
            midpoint = "end"
        return before + [midpoint] + after

    def get_providers(self):
        # Check if we have cached stats
        providers = []
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            providers = self.auction.cached_stats["activity"]["providers"]
        else:
            # Fallback to original calculation
            providers = ["Views", "Joins", "New lots", "Searches", "Bids", "Watches"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "activity" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["activity"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        """Return activity data from cache if available, otherwise calculate it"""
        # Get main auction data
        if self.auction.cached_stats and "activity" in self.auction.cached_stats:
            data = self.auction.cached_stats["activity"]["data"]
        else:
            # Fallback to original calculation if cache is not available
            views = PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
            joins = AuctionTOS.objects.filter(auction=self.auction)
            new_lots = Lot.objects.filter(auction=self.auction)
            searches = SearchHistory.objects.filter(auction=self.auction)
            bids = LotHistory.objects.filter(lot__auction=self.auction, changed_price=True)
            watches = Watch.objects.filter(lot_number__auction=self.auction)

            data = [
                bin_data(views, "date_start", self.bins, self.date_start, self.date_end),
                bin_data(joins, "createdon", self.bins, self.date_start, self.date_end),
                bin_data(new_lots, "date_posted", self.bins, self.date_start, self.date_end),
                bin_data(searches, "createdon", self.bins, self.date_start, self.date_end),
                bin_data(bids, "timestamp", self.bins, self.date_start, self.date_end),
                bin_data(watches, "createdon", self.bins, self.date_start, self.date_end),
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "activity" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["activity"]["data"]
            data = data + compare_data

        return data


class AuctionStatsAttritionJSONView(BaseLineChartView, AuctionStatsPermissionsMixin):
    ignore_percent = 10

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        self.lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
            .filter(auction=self.auction, winning_price__isnull=False)
            .order_by("-date_end")
        )
        self.total_lots = self.lots.count()
        start_index = int(self.ignore_percent / 100 * self.total_lots)
        end_index = (
            int((1 - (self.ignore_percent / 100)) * self.total_lots) - 1
        )  # Subtract 1 because indexing is zero-based
        if self.total_lots > 0:
            self.start_date = self.lots[start_index].date_end
            self.end_date = (
                self.lots[end_index].date_end if self.total_lots > 1 else self.start_date
            )  # Handle case with only one lot
            self.total_runtime = self.end_date - self.start_date
            add_back_on = self.total_runtime / self.ignore_percent
            self.start_date = self.start_date - (add_back_on * 2)
            self.end_date = self.end_date + (add_back_on * 2)
            self.lots = self.lots.filter(date_end__lte=self.start_date, date_end__gte=self.end_date)
        result = super().dispatch(request, *args, **kwargs)
        return result

    def get_labels(self):
        """Not used for scatter plots"""
        # Check if we have cached stats
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            return self.auction.cached_stats["attrition"]["labels"]
        return []

    def get_providers(self):
        """Return names of datasets."""
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            providers = self.auction.cached_stats["attrition"]["providers"]
        else:
            providers = ["Lots"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "attrition" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["attrition"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "attrition" in self.auction.cached_stats:
            data = self.auction.cached_stats["attrition"]["data"]
        else:
            # Fallback to original calculation
            data = [
                [
                    {
                        "x": (lot.date_end - self.end_date).total_seconds() // 60,  # minutes after auction start
                        # 'x': lot.date_end.timestamp() * 1000, # this one gives js timestamps and would need moment.js to convert to date
                        "y": lot.winning_price,
                    }
                    for lot in self.lots
                ]
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "attrition" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["attrition"]["data"]
            data = data + compare_data

        return data


class AuctionStatsBarChartJSONView(LoginRequiredMixin, AuctionViewMixin, BaseColumnsHighChartsView):
    """This is needed because of https://github.com/peopledoc/django-chartjs/issues/56"""

    # allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.get(slug=kwargs["slug"], is_deleted=False)
        if not self.is_auction_admin:
            return redirect(reverse("home"))

        # Load comparison auction if provided
        self.compare_auction = None
        compare_slug = request.GET.get("compare")
        if compare_slug:
            try:
                compare_auction = Auction.objects.get(slug=compare_slug, is_deleted=False)
                # Verify user has access to this auction
                if (
                    compare_auction.created_by == request.user
                    or AuctionTOS.objects.filter(auction=compare_auction, user=request.user, is_admin=True).exists()
                ):
                    self.compare_auction = compare_auction
            except Auction.DoesNotExist:
                pass

        result = super().dispatch(request, *args, **kwargs)
        return result

    def get_yUnit(self):
        return ""

    def get_colors(self):
        return next_color()

    def get_context_data(self, **kwargs):
        """Return graph configuration."""
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "labels": self.get_labels(),
                "chart": self.get_type(),
                "title": self.get_title(),
                "subtitle": self.get_subtitle(),
                "xAxis": self.get_xAxis(),
                "yAxis": self.get_yAxis(),
                "tooltip": self.get_tooltip(),
                "plotOptions": self.get_plotOptions(),
                "datasets": self.get_series(),
                "credits": self.credits,
            }
        )
        return context

    def get_series(self):
        datasets = []
        color_generator = self.get_colors()
        data = self.get_data()
        providers = self.get_providers()
        if len(data) is not len(providers):
            msg = f"self.get_data() return a {len(data)} long array, self.get_providers() returned a {len(providers)} long array.  These need to return the same length array."
            raise ValueError(msg)
        for i, entry in enumerate(data):
            color = tuple(next(color_generator))
            dataset = {
                "data": entry,
                "label": providers[i],
            }
            dataset.update(self.get_dataset_options(i, color))
            datasets.append(dataset)
        return datasets

    def get_dataset_options(self, index, color):
        default_opt = {
            "backgroundColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 0.5)",
            "borderColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 1)",
            "pointBackgroundColor": f"rgba({color[0]}, {color[1]}, {color[2]}, 1)",
        }
        return default_opt

    def get_title(self):
        return ""


class AuctionStatsLotSellPricesJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            return self.auction.cached_stats["lot_sell_prices"]["labels"]

        # Fallback: generate dynamic labels based on actual prices
        sold_lots = self.auction.lots_qs.filter(winning_price__isnull=False)
        if sold_lots.exists():
            max_price = sold_lots.aggregate(max_price=Max("winning_price"))["max_price"] or 40
            max_price = int((max_price + 9) // 10 * 10)

            # Create bins matching the logic in set_stat_lot_sell_prices
            # Use whole number bin boundaries
            bin_width = 2  # Each bin covers $2
            num_bins = min((max_price - 1) // bin_width, 30)
            if num_bins < 10:
                num_bins = 10
                bin_width = max((max_price - 1) // num_bins, 1)

            start_bin = 1
            end_bin = start_bin + num_bins * bin_width

            labels = ["Not sold"]
            for i in range(num_bins):
                bin_start = start_bin + i * bin_width
                bin_end = start_bin + (i + 1) * bin_width
                labels.append(f"{self.auction.currency_symbol}{bin_start}-{bin_end}")
            labels.append(f"{self.auction.currency_symbol}{end_bin}+")
            return labels
        else:
            # No sold lots, use default with whole number boundaries
            start_bin = 1
            bin_width = 2
            num_bins = 19
            end_bin = start_bin + num_bins * bin_width

            labels = ["Not sold"]
            for i in range(num_bins):
                bin_start = start_bin + i * bin_width
                bin_end = start_bin + (i + 1) * bin_width
                labels.append(f"{self.auction.currency_symbol}{bin_start}-{bin_end}")
            labels.append(f"{self.auction.currency_symbol}{end_bin}+")
            return labels

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            providers = self.auction.cached_stats["lot_sell_prices"]["providers"]
        else:
            providers = ["Number of lots"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lot_sell_prices" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["lot_sell_prices"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "lot_sell_prices" in self.auction.cached_stats:
            data = self.auction.cached_stats["lot_sell_prices"]["data"]
        else:
            # Fallback: calculate dynamically
            sold_lots = self.auction.lots_qs.filter(winning_price__isnull=False)
            if sold_lots.exists():
                max_price = sold_lots.aggregate(max_price=Max("winning_price"))["max_price"] or 40
                max_price = int((max_price + 9) // 10 * 10)
                num_bins = min(max_price // 2, 30)
                if num_bins < 10:
                    num_bins = 10

                histogram = bin_data(
                    sold_lots,
                    "winning_price",
                    number_of_bins=num_bins,
                    start_bin=1,
                    end_bin=max_price - 1,
                    add_column_for_high_overflow=True,
                )
                data = [[self.auction.total_unsold_lots] + histogram]
            else:
                # No sold lots, use default
                histogram = bin_data(
                    sold_lots,
                    "winning_price",
                    number_of_bins=19,
                    start_bin=1,
                    end_bin=39,
                    add_column_for_high_overflow=True,
                )
                data = [[self.auction.total_unsold_lots] + histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lot_sell_prices" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["lot_sell_prices"]["data"]
            data = data + compare_data

        return data


class AuctionStatsReferrersJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            return self.auction.cached_stats["referrers"]["labels"]

        # Fallback to original calculation
        self.views = (
            PageView.objects.filter(Q(auction=self.auction) | Q(lot_number__auction=self.auction))
            .exclude(referrer__isnull=True)
            .exclude(referrer__startswith=Site.objects.get_current().domain)
            .exclude(referrer__exact="")
            .values("referrer")
            .annotate(count=Count("referrer"))
        )
        result = []
        for view in self.views:
            if view["count"] > 1:
                result.append(view["referrer"])
        result.append("Other")
        return result

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            providers = self.auction.cached_stats["referrers"]["providers"]
        else:
            providers = ["Number of clicks"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "referrers" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["referrers"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "referrers" in self.auction.cached_stats:
            data = self.auction.cached_stats["referrers"]["data"]
        else:
            # Fallback to original calculation
            result = []
            other = 0
            for view in self.views:
                if view["count"] > 1:
                    result.append(view["count"])
                else:
                    other += 1
            result.append(other)
            data = [result]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "referrers" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["referrers"]["data"]
            data = data + compare_data

        return data


# # this view and the following collect specific data for the tutorial videos
# class AdminStatsImages(AuctionStatsBarChartJSONView):
#     def get_labels(self):
#         return ['No images', 'Has image']

#     def get_providers(self):
#         return ['Median sell price', "Average sell price"]

#     def get_data(self):
#         lots = Lot.objects.filter(auction__slug__in=['njas-in-person-spring-auction-april-2024','nec-2024-auction'], winning_price__isnull=False).annotate(num_images=Count('lotimage'))
#         lots_with_no_images = lots.filter(num_images=0)
#         lots_with_one_image = lots.filter(num_images__gt=0)
#         medians = []
#         averages = []
#         counts = []
#         for lots in [lots_with_no_images, lots_with_one_image]:
#             try:
#                 medians.append(median_value(lots, 'winning_price'))
#             except:
#                 medians.append(0)
#             averages.append(lots.aggregate(avg_value=Avg('winning_price'))['avg_value'])
#         return [medians, averages ]

#     def dispatch(self, request, *args, **kwargs):
#         # little hack for permissions
#         return super().dispatch(request, *args, slug="tfcb-2023-annual-auction", **kwargs)

# class AdminStatsDistanceTraveled(AuctionStatsBarChartJSONView):
#     def get_labels(self):
#         return ['Less than 10 miles', '10-20 miles', '21-30 miles', '31-40 miles', '41-50 miles', '51+ miles']

#     def get_providers(self):
#         return ['Number of people']
#         return ['Sellers', 'Buyers']

#     def get_data(self):
#         slugs_list = ['tfcb-annual', 'acm', 'ovas', 'njas', 'nec', 'scas']
#         q_object = Q()
#         for slug in slugs_list:
#             q_object |= Q(auction__slug__icontains=slug)

#         buyers = AuctionTOS.objects.filter(q_object, auctiontos_winner__isnull=False, auction__promote_this_auction=True)
#         #sellers = AuctionTOS.objects.filter(q_object, auctiontos_seller__isnull=False, auction__promote_this_auction=True)
#         #auctiontos = AuctionTOS.objects.filter(auction__promote_this_auction=True, user__isnull=False)
#         buyer_histogram = bin_data(buyers, 'distance_traveled', number_of_bins=5, start_bin=1, end_bin=51, add_column_for_high_overflow=True,)
#         #seller_histogram = bin_data(sellers, 'distance_traveled', number_of_bins=5, start_bin=1, end_bin=51, add_column_for_high_overflow=True,)
#         logger.debug(buyers.count())
#         return [buyer_histogram]

#     def dispatch(self, request, *args, **kwargs):
#         # little hack for permissions
#         return super().dispatch(request, *args, slug="tfcb-2023-annual-auction", **kwargs)
# # the two previous views collect specific data for the tutorial videos


class AuctionStatsImagesJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            return self.auction.cached_stats["images"]["labels"]

        return ["No images", "One image", "More than one image"]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            providers = self.auction.cached_stats["images"]["providers"]
        else:
            providers = ["Median sell price", "Average sell price", "Number of lots"]

        # Add comparison auction providers if available
        if self.compare_auction and self.compare_auction.cached_stats and "images" in self.compare_auction.cached_stats:
            compare_providers = [
                f"{p} ({self.compare_auction.title})" for p in self.compare_auction.cached_stats["images"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "images" in self.auction.cached_stats:
            data = self.auction.cached_stats["images"]["data"]
        else:
            # Fallback to original calculation
            lots = Lot.objects.filter(auction=self.auction, winning_price__isnull=False).annotate(
                num_images=Count("lotimage")
            )
            lots_with_no_images = lots.filter(num_images=0)
            lots_with_one_image = lots.filter(num_images=1)
            lots_with_one_or_more_images = lots.filter(num_images__gt=1)
            medians = []
            averages = []
            counts = []
            for lots in [
                lots_with_no_images,
                lots_with_one_image,
                lots_with_one_or_more_images,
            ]:
                try:
                    medians.append(median_value(lots, "winning_price"))
                except:
                    medians.append(0)
                averages.append(lots.aggregate(avg_value=Avg("winning_price"))["avg_value"])
                counts.append(lots.count())
            data = [medians, averages, counts]

        # Add comparison auction data if available
        if self.compare_auction and self.compare_auction.cached_stats and "images" in self.compare_auction.cached_stats:
            compare_data = self.compare_auction.cached_stats["images"]["data"]
            data = data + compare_data

        return data


class AuctionStatsTravelDistanceJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            return self.auction.cached_stats["travel_distance"]["labels"]

        return [
            "Less than 10 miles",
            "10-20 miles",
            "21-30 miles",
            "31-40 miles",
            "41-50 miles",
            "51+ miles",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            providers = self.auction.cached_stats["travel_distance"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "travel_distance" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["travel_distance"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "travel_distance" in self.auction.cached_stats:
            data = self.auction.cached_stats["travel_distance"]["data"]
        else:
            # Fallback to original calculation
            auctiontos = AuctionTOS.objects.filter(auction=self.auction, user__isnull=False)
            histogram = bin_data(
                auctiontos,
                "distance_traveled",
                number_of_bins=5,
                start_bin=1,
                end_bin=51,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "travel_distance" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["travel_distance"]["data"]
            data = data + compare_data

        return data


class AuctionStatsPreviousAuctionsJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            return self.auction.cached_stats["previous_auctions"]["labels"]

        return ["First auction", "1 previous auction", "2+ previous auctions"]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            providers = self.auction.cached_stats["previous_auctions"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "previous_auctions" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["previous_auctions"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "previous_auctions" in self.auction.cached_stats:
            data = self.auction.cached_stats["previous_auctions"]["data"]
        else:
            # Fallback to original calculation
            auctiontos = AuctionTOS.objects.filter(auction=self.auction, email__isnull=False)
            histogram = bin_data(
                auctiontos,
                "previous_auctions_count",
                number_of_bins=2,
                start_bin=0,
                end_bin=2,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "previous_auctions" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["previous_auctions"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLotsSubmittedJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            return self.auction.cached_stats["lots_submitted"]["labels"]

        return [
            "Buyer only (0 lots sold)",
            "1-2 lots",
            "3-4 lots",
            "5-6 lots",
            "7-8 lots",
            "9+ lots",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            providers = self.auction.cached_stats["lots_submitted"]["providers"]
        else:
            providers = ["Number of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lots_submitted" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["lots_submitted"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "lots_submitted" in self.auction.cached_stats:
            data = self.auction.cached_stats["lots_submitted"]["data"]
        else:
            # Fallback to original calculation
            invoices = Invoice.objects.filter(auction=self.auction)
            histogram = bin_data(
                invoices,
                "lots_sold",
                number_of_bins=4,
                start_bin=1,
                end_bin=9,
                add_column_for_low_overflow=True,
                add_column_for_high_overflow=True,
            )
            data = [histogram]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "lots_submitted" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["lots_submitted"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLocationVolumeJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            return self.auction.cached_stats["location_volume"]["labels"]

        # Fallback to original calculation
        locations = []
        for location in self.auction.location_qs:
            locations.append(location.name)
        return locations

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            providers = self.auction.cached_stats["location_volume"]["providers"]
        else:
            providers = ["Total bought", "Total sold"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "location_volume" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["location_volume"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "location_volume" in self.auction.cached_stats:
            data = self.auction.cached_stats["location_volume"]["data"]
        else:
            # Fallback to original calculation
            sold = []
            bought = []
            for location in self.auction.location_qs:
                sold.append(location.total_sold)
                bought.append(location.total_bought)
            data = [bought, sold]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "location_volume" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["location_volume"]["data"]
            data = data + compare_data

        return data


class AuctionStatsLocationFeatureUseJSONView(AuctionStatsBarChartJSONView):
    def get_labels(self):
        # Check if we have cached stats
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            return self.auction.cached_stats["feature_use"]["labels"]

        return [
            "An account",
            "Mobile app",
            "Search",
            "Watch",
            "Push notifications as lots sell",
            "Proxy bidding",
            "Chat",
            "Buy now",
            "View invoice",
            "Leave feedback for sellers",
        ]

    def get_providers(self):
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            providers = self.auction.cached_stats["feature_use"]["providers"]
        else:
            providers = ["Percent of users"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "feature_use" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["feature_use"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "feature_use" in self.auction.cached_stats:
            data = self.auction.cached_stats["feature_use"]["data"]
        else:
            # Fallback to original calculation
            auctiontos = AuctionTOS.objects.filter(auction=self.auction)
            auctiontos_with_account = auctiontos.filter(user__isnull=False)
            searches = (
                SearchHistory.objects.filter(user__isnull=False, auction=self.auction).values("user").distinct().count()
            )
            seach_percent = (
                int(searches / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
            )
            watch_qs = Watch.objects.filter(lot_number__auction=self.auction).values("user").distinct()
            watches = watch_qs.count()
            watch_percent = int(watches / auctiontos_with_account.count() * 100)
            notifications = (
                PushInformation.objects.filter(
                    user__in=watch_qs, user__userdata__push_notifications_when_lots_sell=True
                )
                .values("user")
                .distinct()
                .count()
            )
            notification_percent = int(notifications / auctiontos_with_account.count() * 100)
            has_used_proxy_bidding = UserData.objects.filter(
                has_used_proxy_bidding=True,
                user__in=auctiontos_with_account.values_list("user"),
            ).count()
            has_used_proxy_bidding_percent = int(has_used_proxy_bidding / auctiontos_with_account.count() * 100)
            chat = (
                LotHistory.objects.filter(
                    changed_price=False,
                    lot__auction=self.auction,
                    user__in=auctiontos_with_account.values_list("user"),
                )
                .values("user")
                .distinct()
                .count()
            )
            chat_percent = int(chat / auctiontos_with_account.count() * 100)
            mobile_app = (
                auctiontos_with_account.filter(user__mobile_devices__isnull=False).values("user").distinct().count()
            )
            if self.auction.is_online:
                lot_with_buy_now = (
                    Lot.objects.filter(auction=self.auction, buy_now_used=True)
                    .values("auctiontos_winner")
                    .distinct()
                    .count()
                )
            else:
                lot_with_buy_now = (
                    Lot.objects.filter(auction=self.auction, winning_price=F("buy_now_price"))
                    .values("auctiontos_winner")
                    .distinct()
                    .count()
                )
            if auctiontos.count() == 0:
                lot_with_buy_now_percent = 0
                account_percent = 0
                mobile_app_percent = 0
            else:
                account_percent = int(auctiontos_with_account.count() / auctiontos.count() * 100)
                lot_with_buy_now_percent = int(lot_with_buy_now / auctiontos.count() * 100)
                mobile_app_percent = int(mobile_app / auctiontos.count() * 100)
            invoices = Invoice.objects.filter(auction=self.auction)
            viewed_invoices = invoices.filter(opened=True)
            if invoices.count():
                view_invoice_percent = int(viewed_invoices.count() / invoices.count() * 100)
            else:
                view_invoice_percent = 0
            sold_lots = Lot.objects.filter(auction=self.auction, auctiontos_winner__isnull=False)
            leave_feedback = sold_lots.filter(~Q(feedback_rating=0)).values("auctiontos_winner").distinct().count()
            all_sold_lots = sold_lots.values("auctiontos_winner").distinct().count()
            if all_sold_lots == 0:
                leave_feedback_percent = 0
            else:
                leave_feedback_percent = int(leave_feedback / all_sold_lots * 100)
            data = [
                [
                    account_percent,
                    mobile_app_percent,
                    seach_percent,
                    watch_percent,
                    notification_percent,
                    has_used_proxy_bidding_percent,
                    chat_percent,
                    lot_with_buy_now_percent,
                    view_invoice_percent,
                    leave_feedback_percent,
                ]
            ]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "feature_use" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["feature_use"]["data"]
            data = data + compare_data

        return data


class AuctionStatsAuctioneerSpeedJSONView(AuctionStatsAttritionJSONView):
    def get_providers(self):
        """Return names of datasets."""
        providers = []
        # Check if we have cached stats
        if self.auction.cached_stats and "auctioneer_speed" in self.auction.cached_stats:
            providers = self.auction.cached_stats["auctioneer_speed"]["providers"]
        else:
            providers = ["Minutes per lot"]

        # Add comparison auction providers if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "auctioneer_speed" in self.compare_auction.cached_stats
        ):
            compare_providers = [
                f"{p} ({self.compare_auction.title})"
                for p in self.compare_auction.cached_stats["auctioneer_speed"]["providers"]
            ]
            providers = providers + compare_providers

        return providers

    def get_data(self):
        # Get main auction data
        if self.auction.cached_stats and "auctioneer_speed" in self.auction.cached_stats:
            data = self.auction.cached_stats["auctioneer_speed"]["data"]
        else:
            # Fallback to original calculation
            data_points = []
            for i in range(1, len(self.lots)):
                minutes = (self.lots[i - 1].date_end - self.lots[i].date_end).total_seconds() / 60
                ignore_if_more_than = 3  # minutes
                if minutes <= ignore_if_more_than:
                    data_points.append({"x": i, "y": minutes})
            data = [data_points]

        # Add comparison auction data if available
        if (
            self.compare_auction
            and self.compare_auction.cached_stats
            and "auctioneer_speed" in self.compare_auction.cached_stats
        ):
            compare_data = self.compare_auction.cached_stats["auctioneer_speed"]["data"]
            data = data + compare_data

        return data


class AuctionLabelConfig(LoginRequiredMixin, AuctionViewMixin, FormView):
    form_class = LabelPrintFieldsForm
    template_name = "auction_print_setup.html"

    def get_success_url(self):
        return reverse("auction_printing", kwargs={"slug": self.auction.slug})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auction"] = self.auction
        return kwargs

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionBulkPrinting(AuctionViewMixin, FormView):
    model = Auction
    template_name = "auction_printing.html"
    form_class = MultiAuctionTOSPrintLabelForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["all_labels_count"] = self.auction.labels_qs.count()
        context["unprinted_label_count"] = self.auction.unprinted_labels_qs.count()
        context["printed_labels_count"] = context["all_labels_count"] - context["unprinted_label_count"]
        context["auction"] = self.auction
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auctiontos"] = (
            AuctionTOS.objects.filter(auction=self.auction)
            .annotate(
                lots_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
                unprinted_labels_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__label_printed=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
            )
            .filter(lots_count__gt=0)
        )
        return kwargs

    def form_valid(self, form):
        print_only_unprinted = form.cleaned_data["print_only_unprinted"]
        selected_tos = []
        for key, value in form.cleaned_data.items():
            if key.startswith("tos_") and value:
                pk = key.split("_")[1]
                selected_tos.append(pk)
        data = {
            "selected_tos": selected_tos,
            "print_only_unprinted": print_only_unprinted,
        }
        url = reverse("auction_printing_pdf", kwargs={"slug": self.auction.slug})
        url_with_params = f"{url}?{urlencode(data)}"
        return HttpResponseRedirect(url_with_params)


class AuctionBulkPrintingPDF(LotLabelView):
    """Admin page to print labels for multiple users at once"""

    allow_non_admins = False

    def get_queryset(self):
        return self.queryset

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.is_auction_admin

        self.selected_tos = request.GET.get("selected_tos", None)
        self.print_only_unprinted = request.GET.get("print_only_unprinted", "True") == "True"
        if not self.selected_tos:
            self.queryset = self.auction.unprinted_labels_qs
        else:
            self.selected_tos = ast.literal_eval(self.selected_tos)
            if self.print_only_unprinted:
                self.queryset = self.auction.unprinted_labels_qs
            else:
                self.queryset = self.auction.lots_qs
            self.queryset = self.queryset.filter(auctiontos_seller__pk__in=self.selected_tos)
        if not self.get_queryset():
            if not self.selected_tos:
                messages.error(request, "No users selected")
            else:
                messages.error(request, "Couldn't find any labels to print")
            return redirect(reverse("auction_printing", kwargs={"slug": self.auction.slug}))
        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)


class PickupLocationsIncoming(View, AuctionViewMixin):
    """All lots destined for this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.incoming_lots.order_by("-auctiontos_seller__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="incoming_lots_destined_for_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(
            [
                "Lot number",
                "Lot name",
                "Winner name",
                "Origin",
                "Seller name",
            ]
        )
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.winner_name,
                    lot.location,
                    lot.seller_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of incoming lots", user=request.user)
        return response


class PickupLocationsOutgoing(View, AuctionViewMixin):
    """CSV of all lots coming from this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        queryset = self.location.outgoing_lots.order_by("-auctiontos_winner__pickup_location__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="outgoing_lots_coming_from_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(["Lot number", "Seller name", "Lot name", "Destination", "Winner name"])
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.seller_name,
                    lot.lot_name,
                    lot.winner_location,
                    lot.winner_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of outgoing lots", user=request.user)
        return response


class AddToCalendarView(LoginRequiredMixin, View):
    """Redirect or generate an 'Add to Calendar' link for a pickup location"""

    def dispatch(self, request, *args, **kwargs):
        # Extract query params
        self.calendar_type = request.GET.get("type")
        self.second = request.GET.get("second") in ("1", "true", "yes", "True")
        self.location_pk = request.GET.get("location")

        # Validate location exists
        self.location = get_object_or_404(PickupLocation, pk=self.location_pk)
        self.auction = self.location.auction

        if self.second and not self.location.second_pickup_time:
            messages.error(
                request,
                "This location does not have a second pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        if not self.second and not self.location.pickup_time:
            messages.error(
                request,
                "This location does not have a pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        # Confirm user has joined this auction
        self.tos = AuctionTOS.objects.filter(
            auction=self.location.auction,
            user=request.user,
        ).first()

        if not self.tos:
            messages.error(
                request,
                "You haven't joined this auction yet",
            )
            return redirect(self.auction.get_absolute_url())

        # if self.tos.pickup_location.pk is not self.location.pk:
        #     messages.error(
        #         request,
        #         "You can't add a location to your calendar unless you've selected it",
        #     )
        #     return redirect(self.auction.get_absolute_url())

        # "native" returns the event as JSON for the mobile app's native "add to device calendar"
        # bridge; "google"/"outlook" redirect to web calendars; "ics" downloads an .ics file.
        if self.calendar_type not in ("google", "outlook", "ics", "native"):
            messages.error(
                request,
                "Unknown calendar type requested",
            )
            return redirect(self.auction.get_absolute_url())

        self.tos.add_to_calendar = self.calendar_type
        self.tos.save()

        return super().dispatch(request, *args, **kwargs)

    def _build_event(self):
        """Return the shared event fields (title, details, start, end, location) for this pickup."""
        start = self.location.second_pickup_time if self.second else self.location.pickup_time
        if not start:
            msg = "Pickup time not available"
            raise Http404(msg)

        # Convert to UTC and define end
        end = start + timedelta(hours=1)

        # Build common event info
        title = f"{self.location.auction.title}"
        if self.second:
            title += " – second pickup"

        details = f"{self.location.auction.title}\n{self.location.description or ''}".strip()
        loc = self.location.address or f"{self.location.latitude},{self.location.longitude}"
        return title, details, start, end, loc

    def get(self, request, *args, **kwargs):
        """Handle GET: redirect user, return ICS, or return event JSON for the native app."""

        title, details, start, end, loc = self._build_event()

        if self.calendar_type == "native":
            # Consumed by the mobile app's addToCalendar JS bridge (see location_fragment_short.html),
            # which hands these fields to a native "add to device calendar" plugin.
            return JsonResponse(
                {
                    "title": title,
                    "details": details,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "location": loc,
                }
            )

        if self.calendar_type == "google":
            params = {
                "action": "TEMPLATE",
                "text": title,
                "dates": f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}",
                "details": details,
                "location": loc,
            }
            url = f"https://calendar.google.com/calendar/render?{urlencode(params)}"
            return redirect(url)

        elif self.calendar_type == "outlook":
            # Outlook supports ISO 8601 with UTC Z
            params = {
                "subject": title,
                "body": details,
                "startdt": start.isoformat().replace("+00:00", "Z"),
                "enddt": end.isoformat().replace("+00:00", "Z"),
                "location": loc,
            }
            url = f"https://outlook.live.com/calendar/0/deeplink/compose?{urlencode(params)}"
            return redirect(url)

        else:
            ics_content = self._generate_ics(title, details, start, end, loc)
            filename = f"{self.location.auction.slug}.ics"
            response = HttpResponse(ics_content, content_type="text/calendar")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

    def _generate_ics(self, title, description, start, end, location):
        """Return a valid ICS file string (UTC-based, RFC5545 compliant)"""
        uid = uuid.uuid4()
        now_utc = timezone.now()
        escaped_description = description.replace("\n", "\\n")
        return (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//YourSite//Auction Pickup//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            "METHOD:PUBLISH\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}@yourdomain.com\r\n"
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"SUMMARY:{title}\r\n"
            f"DESCRIPTION:{escaped_description}\r\n"
            f"LOCATION:{location}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )


class CategoryFinder(APIView):
    """API view which will return a category (or none) based on POST keyword lot_name"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        lot_name = request.POST["lot_name"]
        result = guess_category(lot_name)
        if result:
            result = {"name": result.name, "value": result.pk}
        else:
            result = {"value": None}
        return JsonResponse(result)


class AuctionFinder(APIView):
    """API view which will return information about an auction based on POST keyword auction.  Expects a pk."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        try:
            self.auction = Auction.objects.filter(pk=request.POST["auction"]).first()
        except ValueError:
            self.auction = None
        if not self.auction or not AuctionTOS.objects.filter(user=request.user, auction=self.auction):
            # you don't get to query auctions you haven't joined
            result = {}
        else:
            result = {
                "use_categories": self.auction.use_categories,
                "reserve_price": self.auction.reserve_price,
                "buy_now": self.auction.buy_now,
                "use_quantity_field": self.auction.use_quantity_field,
                "custom_checkbox_name": self.auction.custom_checkbox_name,
                "custom_field_1": self.auction.custom_field_1,
                "custom_field_1_name": self.auction.custom_field_1_name,
                "use_donation_field": self.auction.use_donation_field,
                "use_i_bred_this_fish_field": self.auction.use_i_bred_this_fish_field,
                "use_custom_checkbox_field": self.auction.use_custom_checkbox_field,
                "use_custom_dropdown_field": self.auction.use_custom_dropdown_field,
                "custom_dropdown_required": self.auction.use_custom_dropdown_field == "required",
                "custom_dropdown_name": self.auction.custom_dropdown_name,
                "custom_dropdown_options": list(
                    AuctionDropdown.objects.filter(auction=self.auction)
                    .order_by("createdon")
                    .values_list("value", flat=True)
                ),
                "use_reference_link": self.auction.use_reference_link,
                "use_description": self.auction.use_description,
            }
        return JsonResponse(result)


class LotChatSubscribe(APIView):
    """Called when a user sends a chat message about a lot to create a ChatSubscription model"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        try:
            lot = Lot.objects.filter(pk=request.POST["lot"]).first()
        except ValueError:
            lot = None
        if not lot:
            msg = f"No lot found with key {lot}"
            raise Http404(msg)
        else:
            subscription = ChatSubscription.objects.filter(
                user=request.user,
                lot=lot,
            ).first()
            if not subscription:
                subscription = ChatSubscription.objects.create(
                    user=request.user,
                    lot=lot,
                )
            unsubscribed = request.POST["unsubscribed"]
            if unsubscribed == "true":  # classic javascript, again
                subscription.unsubscribed = True
            else:
                subscription.unsubscribed = False
            subscription.save()
        return JsonResponse({"unsubscribed": subscription.unsubscribed})


class ChatSubscriptions(LoginRequiredMixin, TemplateView):
    """Show chat messages on your lots and other lots"""

    template_name = "chat_subscriptions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriptions"] = self.request.user.userdata.subscriptions_with_new_message_annotation.order_by(
            "-new_message_count", "-lot__date_posted"
        )
        context["data"] = self.request.user.userdata
        return context


class AddTosMemo(APIView, AuctionViewMixin):
    """API view to update the memo field of an auctiontos"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        memo = request.POST["memo"]
        if memo or memo == "":
            self.auctiontos.memo = memo
            self.auctiontos.save()
            # Sync memo back to the linked ClubMember when the auction manages users through the club
            if self.auction.is_club_managed and self.auctiontos.clubmember_id:
                ClubMember.objects.filter(pk=self.auctiontos.clubmember_id).update(memo=memo)
            return JsonResponse({"result": "ok"})
        raise Http404


class AuctionNoShow(TemplateView, LoginRequiredMixin, AuctionViewMixin):
    """When someone doesn't show up for an auction, offer some tools to clean up the situation"""

    template_name = "auctions/noshow.html"

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.tos = get_object_or_404(AuctionTOS, auction=self.auction, bidder_number=kwargs.pop("tos"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["tos"] = self.tos
        context["bought_lots"] = add_price_info(self.tos.bought_lots_qs)
        context["sold_lots"] = self.tos.lots_qs.annotate(
            full_buyer_refund=ExpressionWrapper(
                F("winning_price") + (F("winning_price") * F("auction__tax") / 100),
                output_field=FloatField(),
            )
        )
        return context


class AuctionNoShowAction(AuctionNoShow, FormMixin):
    """Refund lots, leave feedback, and ban this user"""

    template_name = "auctions/generic_admin_form.html"
    form_class = AuctionNoShowForm

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["tos"] = self.tos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = f"Check any actions you wish to take against {self.tos.name}"
        context["unsold_lot_warning"] = "These actions cannot be undone!"
        context["modal_title"] = f"Take action against {self.tos.name}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            actions = f"Took actions against {self.tos.name}: "
            refund_sold_lots = form.cleaned_data["refund_sold_lots"]
            refund_bought_lots = form.cleaned_data["refund_bought_lots"]
            leave_negative_feedback = form.cleaned_data["leave_negative_feedback"]
            ban_this_user = form.cleaned_data["ban_this_user"]
            if refund_sold_lots:
                actions += "refunded sold lots, "
                for lot in self.tos.lots_qs:
                    if lot.winning_price:
                        lot.refund(100, request.user)
                    else:
                        lot.remove(True, request.user)
            if refund_bought_lots:
                actions += "refunded bought lots, "
                for lot in self.tos.bought_lots_qs:
                    lot.refund(100, request.user)
            if leave_negative_feedback:
                actions += "left negative feedback, "
                for lot in self.tos.bought_lots_qs:
                    lot.winner_feedback_rating = -1
                    lot.winner_feedback_text = "Did not pay"
                    lot.save()
                for lot in self.tos.lots_qs:
                    lot.feedback_rating - 1
                    lot.feedback_text = "Did not provide lot"
                    lot.save()
            if ban_this_user:
                actions += "banned user from future auctions, "
                # we will ban the user whether or not the tos was manually added
                # do not return any evidence to the caller of this request that the ban worked or didn't
                # as that could be used to determine if someone has an account on the site
                user = User.objects.filter(email=self.tos.email).first()
                if self.tos.email and user:
                    obj, created = UserBan.objects.update_or_create(
                        banned_user=user,
                        user=request.user,
                        defaults={},
                    )
            self.auction.create_history(applies_to="USERS", action=actions[:-2], user=request.user)
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)


@method_decorator(csrf_exempt, name="dispatch")
class PayPalWebhookView(PayPalAPIMixin, View):
    """
    Minimal PayPal webhook handler that:
      - validates the webhook signature with PayPal (verify-webhook-signature)
      - processes a few important event types (onboarding, consent revoke, capture/refund, disputes)
    Requirements:
      - settings.PAYPAL_API_BASE (e.g. https://api-m.sandbox.paypal.com)
      - settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET
      - settings.PAYPAL_WEBHOOK_ID (the webhook id you registered in PayPal dashboard)
      - (optional) settings.PAYPAL_PARTNER_ATTRIBUTION_ID (BN code) if you want to include it in calls
    """

    def post(self, request, *args, **kwargs):
        # Read raw body and parse JSON
        raw_body = request.body
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            logger.exception("Invalid JSON in PayPal webhook: %s", exc)
            return HttpResponseBadRequest("invalid json")

        # Extract PayPal transmission headers (case-insensitive)
        # Django exposes headers as HTTP_<HEADER_NAME> in request.META
        def hdr(name):
            return request.META.get(f"HTTP_{name.upper().replace('-', '_')}", request.headers.get(name))

        transmission_id = hdr("PayPal-Transmission-Id")
        transmission_time = hdr("PayPal-Transmission-Time")
        cert_url = hdr("PayPal-Cert-Url")
        auth_algo = hdr("PayPal-Auth-Algo")
        transmission_sig = hdr("PayPal-Transmission-Sig")

        # Check webhook_id is configured
        webhook_id = getattr(settings, "PAYPAL_WEBHOOK_ID", None)
        if not webhook_id:
            logger.warning("PAYPAL_WEBHOOK_ID not configured, rejecting webhook")
            return HttpResponseBadRequest("webhook not configured")

        if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig]):
            logger.warning(
                "Missing PayPal webhook headers; rejecting. headers=%s webhook_id=%s",
                {
                    k: hdr(k)
                    for k in [
                        "PayPal-Transmission-Id",
                        "PayPal-Transmission-Time",
                        "PayPal-Cert-Url",
                        "PayPal-Auth-Algo",
                        "PayPal-Transmission-Sig",
                    ]
                },
                webhook_id,
            )
            return HttpResponseBadRequest("missing verification headers")

        # Build verification payload with webhook_id
        verify_payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": event,
        }

        # Get access token using client credentials (for webhook verification)
        try:
            access_token = self._get_access_token()
        except Exception as exc:
            logger.error("Failed to obtain PayPal access token for webhook verification: %s", exc)
            return HttpResponse(status=500)

        # Call PayPal verify webhook signature endpoint
        verify_resp = requests.post(
            f"{settings.PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=verify_payload,
            timeout=10,
        )
        try:
            verify_resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "PayPal verify-webhook-signature returned non-2xx: status=%s debug_id=%s body=%s exc=%s",
                verify_resp.status_code,
                verify_resp.headers.get("Paypal-Debug-Id"),
                verify_resp.text[:500],
                exc,
            )
            return HttpResponse(status=500)
        try:
            verify_data = verify_resp.json()
        except ValueError as exc:
            logger.error(
                "PayPal verify-webhook-signature returned non-JSON: status=%s debug_id=%s body=%s exc=%s",
                verify_resp.status_code,
                verify_resp.headers.get("Paypal-Debug-Id"),
                verify_resp.text[:500],
                exc,
            )
            return HttpResponse(status=500)
        verification_status = verify_data.get("verification_status")
        if verification_status != "SUCCESS":
            logger.warning(
                "PayPal webhook signature verification failed: status=%s response=%s",
                verification_status,
                verify_data,
            )
            return HttpResponseBadRequest("webhook verification failed")

        # At this point, the webhook is verified as coming from PayPal.
        event_type = event.get("event_type")
        resource = event.get("resource", {}) or {}

        logger.info("Verified PayPal webhook: %s", event_type)

        if event_type == "MERCHANT.ONBOARDING.COMPLETED":
            # Example: merchant onboarding completed
            # resource may contain merchantId / merchantIdInPayPal / tracking_id
            merchant_id_in_paypal = resource.get("merchant_id")
            tracking_id = resource.get("tracking_id")
            # try find user via tracking_id first
            user = None
            if tracking_id:
                ud = UserData.objects.filter(unsubscribe_link=tracking_id).first()
                if ud:
                    user = ud.user
            # fallback: attempt to find PayPalSeller by merchant id
            if not user and merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
                user = seller.user if seller else None

            # Create or update PayPalSeller record (if user found)
            if user:
                seller, _ = PayPalSeller.objects.get_or_create(user=user)
                if merchant_id_in_paypal:
                    seller.paypal_merchant_id = merchant_id_in_paypal
                # email may not be present in the resource; if present, update
                email = resource.get("payerEmail") or resource.get("primary_email") or resource.get("primaryEmail")
                if email:
                    seller.payer_email = email
                seller.save()
                logger.info("Updated PayPalSeller for user %s after onboarding webhook", user.pk)
            else:
                logger.info(
                    "Onboarding webhook: no local user found for merchant %s tracking_id=%s",
                    merchant_id_in_paypal,
                    tracking_id,
                )

        elif event_type == "MERCHANT.PARTNER-CONSENT.REVOKED":
            # Mark seller as disconnected / revoke tokens
            merchant_id_in_paypal = resource.get("merchant_id")
            seller = None
            if merchant_id_in_paypal:
                seller = PayPalSeller.objects.filter(paypal_merchant_id=merchant_id_in_paypal).first()
            if seller:
                seller.delete()
                logger.info("Revoked selling for merchant_id=%s", merchant_id_in_paypal)
            else:
                logger.info("Partner-consent revoked for unknown merchant %s", merchant_id_in_paypal)

        elif event_type == "CHECKOUT.ORDER.COMPLETED":
            # Order is already captured - process the order data from the webhook resource
            # without making a redundant capture API call
            if resource.get("status") == "COMPLETED":
                error, _ = self._process_captured_order(resource)
                if error:
                    logger.error("Error handling completed order webhook: %s", error)
                else:
                    logger.info("Payment processed via CHECKOUT.ORDER.COMPLETED webhook")

        elif event_type == "CHECKOUT.CAPTURE.COMPLETED":
            """This one doesn't save the invoice"""
            try:
                order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
                # Fetch the order details to get purchase_units[0].reference_id (our invoice pk)
                order_data = self.get_from_paypal(f"v2/checkout/orders/{order_id}")
                purchase_unit = (order_data.get("purchase_units") or [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("Capture webhook resolved order_id=%s reference_id=%s", order_id, reference_id)

                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "capture_complete", "pk": invoice.pk},
                    )
            except Exception:
                logger.exception(
                    "Error processing capture webhook for resource: %s, debug_id %s", resource, self.paypal_debug
                )
            return JsonResponse({"status": "ok"})
        elif event_type == "CHECKOUT.ORDER.APPROVED":
            # Extract the reference_id (our invoice reference) from the approved order and print/log it.
            try:
                purchase_unit = resource.get("purchase_units", [{}])[0]
                reference_id = purchase_unit.get("reference_id")
                logger.info("PayPal webhook CHECKOUT.ORDER.APPROVED reference_id=%s", reference_id)
                invoice = Invoice.objects.filter(pk=reference_id).first()
                if invoice:
                    channel_layer = channels.layers.get_channel_layer()
                    async_to_sync(channel_layer.group_send)(
                        f"auctions_{invoice.auction.pk}",
                        {"type": "invoice_approved", "pk": invoice.pk},
                    )
            except ValueError:
                logger.exception("Failed to extract reference_id for CHECKOUT.ORDER.APPROVED: %s", resource)
            return JsonResponse({"status": "ok"})

        elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.SALE.REFUNDED"):
            self.handle_refund(resource)
            logger.info("Refund received")

        else:
            # Unhandled event types: log for inspection
            logger.info("Unhandled PayPal webhook event_type=%s resource_keys=%s", event_type, list(resource.keys()))

        # Return success to PayPal
        return JsonResponse({"status": "ok"})


class SquareWebhookView(SquareAPIMixin, View):
    """Handle Square webhook events for payment notifications
    Implements webhook signature verification using HMAC-SHA256
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def verify_signature(self, request, raw_body, signature):
        """Verify Square webhook signature using Square SDK
        Square signs webhooks with: base64(HMAC-SHA256(signature_key, notification_url + request_body))
        Returns True if signature is valid, False otherwise
        """
        if not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            logger.warning("SQUARE_WEBHOOK_SIGNATURE_KEY not configured - skipping signature verification")
            if settings.DEBUG:
                return True  # Allow webhook if signature key not configured
            else:
                return False  # Reject webhook if signature key not configured in production

        try:
            from square.utils.webhooks_helper import verify_signature as square_verify_signature

            # Prefer an explicit configured URL if provided (useful behind proxies)
            notification_url = getattr(settings, "SQUARE_WEBHOOK_PUBLIC_URL", "").strip()
            if not notification_url:
                # Fallback: absolute URL of this request (no query string per Square docs)
                notification_url = request.build_absolute_uri(request.path)

            # Ensure raw_body is a string as expected by Square SDK
            body_str = raw_body if isinstance(raw_body, str) else raw_body.decode("utf-8")

            # Use Square SDK's signature verification
            return square_verify_signature(
                request_body=body_str,
                signature_header=signature,
                signature_key=settings.SQUARE_WEBHOOK_SIGNATURE_KEY,
                notification_url=notification_url,
            )
        except Exception as e:
            logger.exception("Error verifying Square webhook signature: %s", e)
            return False

    def post(self, request, *args, **kwargs):
        # In production, require SQUARE_WEBHOOK_SIGNATURE_KEY if Square is configured
        if not settings.DEBUG and not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            if settings.SQUARE_APPLICATION_ID or settings.SQUARE_CLIENT_SECRET:
                msg = "SQUARE_WEBHOOK_SIGNATURE_KEY must be set in production when Square is configured"
                raise ImproperlyConfigured(msg)

        # Read raw body
        try:
            raw_body = request.body.decode("utf-8")
            event = json.loads(raw_body)
        except Exception as exc:
            logger.exception("Invalid JSON in Square webhook: %s", exc)
            return HttpResponseBadRequest("invalid json")

        # Verify webhook signature if configured
        if settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
            signature = request.headers.get("X-Square-Hmacsha256-Signature", "")
            if not signature:
                logger.error("Square webhook missing signature header")
                return HttpResponseForbidden("missing signature")

            if not self.verify_signature(request, raw_body, signature):
                logger.error("Square webhook signature verification failed")
                return HttpResponseForbidden("invalid signature")

        event_type = event.get("type")
        logger.info("Received Square webhook: %s", event_type)

        if event_type == "payment.updated":
            # Payment completed or updated
            data = event.get("data", {})
            payment_object = data.get("object", {})
            payment = payment_object.get("payment", {})

            payment_status = payment.get("status")
            payment_id = payment.get("id")
            order_id = payment.get("order_id")
            reference_id = None
            # Handle COMPLETED status - create payment record and mark invoice paid
            if payment_status == "COMPLETED":
                merchant_id = event.get("merchant_id", "")
                seller = SquareSeller.objects.filter(square_merchant_id=merchant_id).first()
                if not seller:
                    logger.warning("Square webhook: SquareSeller not found for merchant_id: %s", merchant_id)
                elif seller.square_merchant_id:
                    client = seller.get_square_client()
                    if client:
                        try:
                            order_response = client.orders.get(order_id=order_id)
                            # Square SDK returns response objects with attributes
                            if hasattr(order_response, "order") and order_response.order:
                                reference_id = getattr(order_response.order, "reference_id", None)
                            if not reference_id:
                                logger.error("reference id not found for Square order %s", order_id)
                                logger.error(order_response)
                        except Exception as e:
                            logger.exception("Error retrieving Square order %s: %s", order_id, e)
                    else:
                        logger.error("Could not get Square client for user %s", seller.user.pk)

                # Only proceed if we have a reference_id to look up the invoice. reference_id is our
                # invoice pk as a string; guard against any non-numeric value so a stray payment
                # can't raise (Invoice.pk is an int) and 500 the webhook.
                if reference_id:
                    try:
                        invoice = Invoice.objects.filter(pk=int(reference_id)).first()
                    except (TypeError, ValueError):
                        invoice = None
                        logger.warning("Square webhook: non-numeric reference_id: %s", reference_id)
                    if invoice:
                        amount_money = payment.get("amount_money", {})
                        amount_value = Decimal(amount_money.get("amount", 0)) / 100
                        currency = amount_money.get("currency", "USD")
                        receipt_number = payment.get("receipt_number", "")

                        payment_record, created = InvoicePayment.objects.get_or_create(
                            invoice=invoice,
                            external_id=payment_id,
                            defaults={
                                "amount": amount_value,
                                "amount_available_to_refund": amount_value,
                                "currency": currency,
                                "payment_method": "Square",
                                "receipt_number": receipt_number,
                            },
                        )
                        # If payment already existed, make sure amount_available_to_refund is set
                        if not created:
                            if payment_record.amount_available_to_refund == Decimal("0.00"):
                                payment_record.amount_available_to_refund = amount_value
                            # Update receipt_number if it wasn't set before
                            if receipt_number and not payment_record.receipt_number:
                                payment_record.receipt_number = receipt_number
                            payment_record.save()
                        if invoice.auctiontos_user and invoice.auction:
                            try:
                                action = f"Payment via Square for bidder {invoice.auctiontos_user.bidder_number} in the amount of {amount_value} {currency}"
                                invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
                            except Exception:
                                logger.exception("create_history failed for Square payment on invoice %s", invoice.pk)
                        # Use the rounded balance so a rounded-down charge (the amount we billed)
                        # still settles the invoice.
                        if invoice.rounded_net_after_payments >= 0:
                            if not invoice.renewal_needed:
                                try:
                                    _ensure_invoice_renewal_state(invoice)
                                except Exception:
                                    logger.exception(
                                        "Failed to ensure renewal state for invoice %s before Square PAID",
                                        invoice.pk,
                                    )
                            invoice.status = "PAID"
                            invoice.save()
                            try:
                                _process_invoice_membership_renewal(
                                    invoice, payment_method="Square", external_id=payment_id
                                )
                            except Exception:
                                logger.exception(
                                    "membership renewal failed after Square payment on invoice %s", invoice.pk
                                )

                            # Send websocket notification for payment completion
                            try:
                                channel_layer = channels.layers.get_channel_layer()
                                async_to_sync(channel_layer.group_send)(
                                    f"invoice_{invoice.pk}",
                                    {
                                        "type": "invoice_status",
                                        "message": "paid",
                                    },
                                )
                                if invoice.auction:
                                    async_to_sync(channel_layer.group_send)(
                                        f"auctions_{invoice.auction.pk}",
                                        {
                                            "type": "invoice_paid",
                                            "pk": invoice.pk,
                                        },
                                    )
                            except Exception:
                                logger.exception(
                                    "Failed to send websocket notification for Square payment on invoice %s",
                                    invoice.pk,
                                )
                        logger.info("Square payment completed for invoice %s", invoice.pk)
                    else:
                        logger.warning("Square webhook: Invoice not found for reference_id: %s", reference_id)

        elif event_type == "refund.updated":
            # Refund processed
            data = event.get("data", {})
            refund_object = data.get("object", {})
            refund = refund_object.get("refund", {})
            refund_id = refund.get("id", {})

            if refund.get("status") == "COMPLETED":
                payment_id = refund.get("payment_id")
                # Find the original payment and mark refund
                payment_record = InvoicePayment.objects.filter(external_id=payment_id).first()
                if payment_record and refund_id:
                    refund_amount = Decimal(refund.get("amount_money", {}).get("amount", 0)) / 100
                    payment_record.amount_available_to_refund -= refund_amount
                    payment_record.save()

                    refund_payment, _ = InvoicePayment.objects.update_or_create(
                        external_id=refund_id,
                        defaults={
                            "invoice": payment_record.invoice,
                            "amount": -abs(refund_amount),  # Ensure negative for refund
                            "currency": payment_record.currency,
                            "payment_method": "Square Refund",
                            "memo": refund.get("reason", "")[:500],
                        },
                    )
                    payment_record.invoice.recalculate()
                    action = f"Refund via Square for bidder {payment_record.invoice.auctiontos_user.bidder_number} in the amount of {refund_amount} {payment_record.currency}"
                    payment_record.invoice.auction.create_history(applies_to="INVOICES", action=action, user=None)
                    logger.info("Square refund completed for payment %s", payment_id)

        elif event_type == "oauth.authorization.revoked":
            # Merchant revoked OAuth authorization - delete SquareSeller instance
            merchant_id = event.get("merchant_id")
            if merchant_id:
                square_seller = SquareSeller.objects.filter(square_merchant_id=merchant_id).first()
                if square_seller:
                    square_seller.delete()
        return HttpResponse(status=200)


class QuickCheckout(AuctionViewMixin, TemplateView):
    """Enter a bidder number or name and mark their invoice as paid
    For https://github.com/iragm/fishauctions/issues/292"""

    template_name = "auctions/quick_checkout.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # The camera scanner (scan a bidder number or member card to pull up the invoice) is meant
        # for small-screen devices and the native app -- not desktop. The server can't see the
        # browser viewport, so we always ship the script and let a Bootstrap responsive class hide
        # the camera on larger screens (see the d-md-none wrapper in the template). The app's WebView
        # reports a phone-sized viewport, so it's covered by the same small-screen rule.
        context["show_camera_scanner"] = True
        return context


class QuickCheckoutHTMX(AuctionViewMixin, PayPalAPIMixin, SquareAPIMixin, TemplateView):
    """For use with HTMX calls on QuickCheckout"""

    template_name = "auctions/quick_checkout_htmx.html"

    def _normalize_scanned_term(self, term):
        """Translate a scanned barcode into something the checkout search can match.

        The checkout search matches a bidder number directly but knows nothing about paddle
        barcodes (11111 + bidder number) or membership card numbers, so map those to the
        bidder number here. Returns the term unchanged if it isn't a recognizable barcode."""
        term = (term or "").strip()
        if not term:
            return term
        # Paddle barcode: 11111 followed by the bidder number
        if term.startswith("11111") and len(term) > 5 and term[5:].isdigit():
            return term[5:]
        # Membership card: a bare number matching a ClubMember in this auction's club
        if term.isdigit() and self.auction.club_id:
            member = ClubMember.objects.filter(
                club=self.auction.club, membership_number=int(term), is_deleted=False
            ).first()
            if member and member.bidder_number:
                return member.bidder_number
        return term

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        qs = AuctionTOS.objects.filter(auction=self.auction)
        search_term = kwargs.get("filter")
        # The camera scanner posts ?barcode=1 so paddle/membership-card barcodes get translated to a
        # bidder number; typed searches are left alone so a numeric name search still behaves normally.
        if self.request.GET.get("barcode"):
            search_term = self._normalize_scanned_term(search_term)
        filtered_qs = AuctionTOSFilter.generic(self, qs, search_term)
        invoice = None
        if filtered_qs.count() > 1:
            context["multiple_tos"] = True
        else:
            context["multiple_tos"] = False
            context["tos"] = filtered_qs.first()
            if context["tos"]:
                invoice = context["tos"].invoice
                context["invoice"] = invoice
                _ensure_invoice_renewal_state(invoice)
            if invoice:
                # Generate PayPal QR code if available.
                # The in-person QR flow relies on PayPal webhooks (CHECKOUT.ORDER.APPROVED /
                # CHECKOUT.CAPTURE.COMPLETED) to update the cashier screen once the payer approves
                # on their phone. Clubs using their own (non-OAuth) credentials have no webhook
                # wired up, so skip the QR for them -- they can still take Square/cash here.
                if (
                    invoice.show_paypal_button
                    and not invoice.reason_for_payment_not_available
                    and not invoice.paypal_credentials
                ):
                    try:
                        context["paypal_qr_code_link"] = self.create_order(invoice)
                    except Exception:
                        logger.warning("PayPal order creation failed for invoice %s", invoice.pk, exc_info=True)
                # Generate Square QR code if available
                if invoice.show_square_button and not invoice.reason_for_payment_not_available:
                    payment_url, error_message = self.create_payment_link(invoice)
                    if payment_url:
                        context["square_qr_code_link"] = payment_url
                    elif error_message:
                        # Log the error but don't show QR code
                        logger.warning(
                            "Square payment link creation failed for invoice %s: %s", invoice.pk, error_message
                        )
        return context


# Club management views
class ClubDetailView(ClubViewMixin, TemplateView):
    """User self-service page for a club"""

    active_tab = "home"
    template_name = "auctions/club_detail.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not self.club.enable_club_page:
            # Page is disabled — only users with any club permission may view it; everyone else gets 404
            has_access = request.user.is_authenticated and (
                request.user.is_superuser
                or ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False)
                .filter(
                    Q(permission_admin=True)
                    | Q(permission_view=True)
                    | Q(permission_add_edit=True)
                    | Q(permission_edit_club=True)
                    | Q(permission_money=True)
                    | Q(permission_manage_auctions=True)
                    | Q(permission_export=True)
                    | Q(permission_manage_bap=True)
                )
                .exists()
            )
            if not has_access:
                raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        member = None
        if self.request.user.is_authenticated:
            member = ClubMember.objects.filter(club=self.club, user=self.request.user, is_deleted=False).first()
        requested_member_uuid = self.request.GET.get("user", "")
        if requested_member_uuid:
            member = ClubMember.objects.filter(club=self.club, uuid=requested_member_uuid, is_deleted=False).first()
        context["member"] = member
        # Only the actual owner — not a holder of the UUID renewal link — may see the
        # Google Wallet save button, since adding to a wallet should never be done on
        # behalf of someone else.
        context["is_membership_owner"] = bool(
            member and self.request.user.is_authenticated and member.user_id == self.request.user.id
        )
        from auctions.apple_wallet import is_configured as _apple_configured

        context["apple_wallet_enabled"] = _apple_configured()
        if member:
            context["update_form"] = ClubMemberSelfServiceForm(instance=member)
        club = self.club
        requested_tab = (self.kwargs.get("tab") or self.request.GET.get("tab") or "auctions").lower()
        available_tabs = {"auctions"}
        if club.enable_breeder_award_program:
            context["show_bap_tabs"] = True
            context["bap_leaderboard"] = _bap_leaderboard(club, "bap_points", member)
            context["bap_leaderboard_ytd"] = _bap_leaderboard(club, "bap_points_ytd", member)
            available_tabs.add("bap")
            context["hap_leaderboard"] = _bap_leaderboard(club, "hap_points", member) if club.separate_hap else []
            context["hap_leaderboard_ytd"] = (
                _bap_leaderboard(club, "hap_points_ytd", member) if club.separate_hap else []
            )
            if club.separate_hap:
                available_tabs.add("hap")
            context["culture_leaderboard"] = (
                _bap_leaderboard(club, "culture_points", member) if club.separate_cap else []
            )
            context["culture_leaderboard_ytd"] = (
                _bap_leaderboard(club, "culture_points_ytd", member) if club.separate_cap else []
            )
            if club.separate_cap:
                available_tabs.add("culture")
            context["can_manage_bap"] = self.user_has_club_permission("permission_manage_bap")
            context["my_bap_awards"] = None
            context["my_points_chart_data"] = None
            if member:
                context["my_bap_awards"] = BapAward.objects.filter(club_member=member).order_by("-date", "-pk")[:25]
                context["my_points_chart_data"] = _club_points_chart_data(club, member)
                available_tabs.add("my-points")
            alltime_months = _last_n_month_starts(60)
            ytd_months = _ytd_month_starts()
            context["bap_top10_chart"] = _club_top10_chart_data(club, "bap_points", "points", member, alltime_months)
            context["bap_top10_chart_ytd"] = _club_top10_chart_data(
                club, "bap_points_ytd", "points", member, ytd_months, is_ytd=True
            )
            if club.separate_hap:
                context["hap_top10_chart"] = _club_top10_chart_data(
                    club, "hap_points", "hap_points", member, alltime_months
                )
                context["hap_top10_chart_ytd"] = _club_top10_chart_data(
                    club, "hap_points_ytd", "hap_points", member, ytd_months, is_ytd=True
                )
            if club.separate_cap:
                context["cap_top10_chart"] = _club_top10_chart_data(
                    club, "culture_points", "cap_points", member, alltime_months
                )
                context["cap_top10_chart_ytd"] = _club_top10_chart_data(
                    club, "culture_points_ytd", "cap_points", member, ytd_months, is_ytd=True
                )
        else:
            context["show_bap_tabs"] = False
            # Legacy flat leaderboard for clubs without BAP enabled
            has_points = (
                ClubMember.objects.filter(club=club, is_deleted=False)
                .filter(Q(bap_points__gt=0) | Q(hap_points__gt=0))
                .exists()
            )
            context["has_points"] = has_points
            if has_points:
                context["bap_leaderboard"] = ClubMember.objects.filter(
                    club=club, is_deleted=False, bap_points__gt=0
                ).order_by("-bap_points")[:10]
                context["hap_leaderboard"] = ClubMember.objects.filter(
                    club=club, is_deleted=False, hap_points__gt=0
                ).order_by("-hap_points")[:10]
        context["active_club_tab"] = requested_tab if requested_tab in available_tabs else "auctions"
        context["can_access_admin"] = self.user_has_club_permission(
            "permission_admin"
        ) or self.user_has_club_permission("permission_view")
        context["can_edit_settings"] = self.user_has_club_permission("permission_edit_club")
        can_manage_auctions = self.user_has_club_permission("permission_admin") or self.user_has_club_permission(
            "permission_manage_auctions"
        )
        context["can_manage_auctions"] = can_manage_auctions
        context["now"] = timezone.now()
        context["current_auction_id"] = self.club.current_auction_id
        membership_expiration_date = member.membership_expiration_date if member else None
        context["membership_expiration_date"] = membership_expiration_date
        membership_invoice = None
        show_membership_payment_button = False
        is_expired = False
        expiring_soon = False
        is_paid_member = False
        if member:
            _process_pending_membership_renewal_for_member(self.club, member)
            member.refresh_from_db()
            is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
            is_paid_member = member.is_paid_member
            if should_show_payment:
                membership_invoice = _get_or_create_membership_invoice(self.club, member)
                show_membership_payment_button = True
        context["membership_invoice"] = membership_invoice
        context["show_membership_payment_button"] = show_membership_payment_button
        context["membership_is_expired"] = is_expired
        context["membership_expiring_soon"] = expiring_soon
        context["membership_is_paid_member"] = is_paid_member
        show_club_map = self.club.latitude is not None and self.club.longitude is not None
        context["show_club_map"] = show_club_map
        if show_club_map:
            context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
            directions_query = self.club.location or f"{self.club.latitude},{self.club.longitude}"
            context["club_map_directions_url"] = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
                directions_query
            )
        if can_manage_auctions:
            context["club_auctions"] = Auction.objects.filter(club=self.club, is_deleted=False).order_by("-date_start")[
                :CLUB_DETAIL_AUCTION_LIMIT
            ]
        else:
            context["club_auctions"] = Auction.objects.filter(
                club=self.club, promote_this_auction=True, is_deleted=False
            ).order_by("-date_start")[:CLUB_DETAIL_AUCTION_LIMIT]
        # Email button: visible to authenticated users when someone can be reached at this club.
        from .email_routing import email_routing_enabled

        if email_routing_enabled():
            # SES routing active: show button only when a real recipient is configured
            # (permission_add_edit member, admin, or manual override).
            contact_recipient = self.club.contact_email_recipient
            has_club_contact = bool(contact_recipient)
            context["club_contact_email"] = self.club.contact_sender_email if has_club_contact else None
        else:
            # No SES: rely on the plain contact_email address field.
            has_club_contact = bool(self.club.contact_email)
            context["club_contact_email"] = self.club.contact_email or None
        context["has_club_contact"] = has_club_contact
        return context

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        action = request.POST.get("action", "join")
        if action == "make_current":
            can_manage_auctions = self.user_has_club_permission("permission_admin") or self.user_has_club_permission(
                "permission_manage_auctions"
            )
            if can_manage_auctions:
                auction = Auction.objects.filter(
                    pk=request.POST.get("auction"), club=self.club, is_deleted=False
                ).first()
                if auction:
                    self.club.current_auction = auction
                    self.club.save(update_fields=["current_auction"])
                    messages.success(request, f"{auction} is now the current auction.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        if action == "update":
            member = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
            if member:
                form = ClubMemberSelfServiceForm(request.POST, instance=member)
                if form.is_valid():
                    form.save()
                    messages.success(request, "Your info has been updated.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        # join logic
        if not self.club.allow_joining:
            messages.error(request, "This club is not accepting new members right now.")
            return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))
        existing = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
        if existing:
            messages.info(request, "You are already a member of this club.")
        else:
            ClubMember.objects.create(
                club=self.club,
                user=request.user,
                name=f"{request.user.first_name} {request.user.last_name}".strip(),
                email=request.user.email,
                source="joined",
            )
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"{request.user.get_full_name()} joined the club",
                applies_to="MEMBERS",
            )
            messages.success(request, f"You have joined {self.club.name}!")
        return redirect(reverse("club_detail", kwargs={"slug": self.club.slug}))


def _get_or_create_membership_invoice(club, member):
    """Find or create an unpaid renewal invoice for this member's club."""
    # Match on club_member first: that is what we set when creating the invoice below, so this
    # is the only lookup guaranteed to find a previously created renewal invoice. Without it,
    # members with an email but no linked user account never match the lookups below and a new
    # UNPAID invoice is created on every page view.
    invoice = Invoice.objects.filter(
        club=club,
        auction=None,
        club_member=member,
        renewal_processed=False,
        status="UNPAID",
    ).first()
    if invoice is None and member.email:
        invoice = Invoice.objects.filter(
            club=club,
            auction=None,
            auctiontos_user__email__iexact=member.email,
            renewal_processed=False,
            status="UNPAID",
        ).first()
    if invoice is None and member.user:
        invoice = Invoice.objects.filter(
            club=club,
            auction=None,
            buyer=member.user,
            renewal_processed=False,
            status="UNPAID",
        ).first()
    if invoice is None:
        invoice = Invoice.objects.create(
            club=club,
            club_member=member,
            buyer=member.user or None,
            status="UNPAID",
            renewal_needed=True,
        )
    return invoice


def _membership_renewal_state(club, member):
    """Return (is_expired, expiring_soon, should_show_payment, can_pay)."""
    today = timezone.now().date()
    expiration = member.membership_expiration_date
    is_expired = bool(expiration and expiration < today) or (not expiration and not member.is_paid_member)
    expiring_soon = bool(expiration and not is_expired and (expiration - today).days <= 30)
    can_pay = bool(
        club.enable_club_page and club.membership_annual_fee and (club.can_accept_paypal or club.can_accept_square)
    )
    should_show_payment = can_pay and (is_expired or expiring_soon or not member.is_paid_member)
    return is_expired, expiring_soon, should_show_payment, can_pay


def _process_pending_membership_renewal_for_member(club, member):
    """Process any PAID-but-unprocessed renewal invoice for this member.

    Called on member page load so Square payments (webhook may arrive after redirect)
    are picked up synchronously when the member views their page.
    """
    if not member.user:
        return
    paid_invoice = Invoice.objects.filter(
        club=club,
        buyer=member.user,
        renewal_needed=True,
        renewal_processed=False,
        status="PAID",
    ).first()
    if paid_invoice:
        _process_invoice_membership_renewal(paid_invoice)


class ClubMemberByUUIDView(ClubViewMixin, TemplateView):
    """Public, UUID-keyed page that shows a member's name and wallet-add buttons.

    Anyone with the UUID link can view this page and add the membership to their
    Google/Apple wallet — the UUID is the capability token.
    """

    template_name = "auctions/club_member_by_uuid.html"
    allow_non_admins = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        member = get_object_or_404(ClubMember, club=self.club, uuid=kwargs["uuid"], is_deleted=False)
        _process_pending_membership_renewal_for_member(self.club, member)
        member.refresh_from_db()
        member.update_last_club_activity()
        from auctions.apple_wallet import is_configured as _apple_configured

        context["club"] = self.club
        context["member"] = member
        context["apple_wallet_enabled"] = _apple_configured()

        is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
        context["membership_invoice"] = (
            _get_or_create_membership_invoice(self.club, member) if should_show_payment else None
        )
        context["is_expired"] = is_expired
        context["expiring_soon"] = expiring_soon
        context["is_paid_member"] = member.is_paid_member
        return context


class ClubMemberByNumberView(ClubViewMixin, TemplateView):
    """Public, number-keyed page showing membership number, expiration status, and a
    payment button when applicable. Linked from Discord."""

    template_name = "auctions/club_member_by_number.html"
    allow_non_admins = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        member = get_object_or_404(ClubMember, club=self.club, membership_number=kwargs["number"], is_deleted=False)
        _process_pending_membership_renewal_for_member(self.club, member)
        member.refresh_from_db()
        is_expired, expiring_soon, should_show_payment, _ = _membership_renewal_state(self.club, member)
        context["club"] = self.club
        context["member"] = member
        context["expiration"] = member.membership_expiration_date
        context["is_expired"] = is_expired
        context["expiring_soon"] = expiring_soon
        context["is_paid_member"] = member.is_paid_member
        context["membership_invoice"] = (
            _get_or_create_membership_invoice(self.club, member) if should_show_payment else None
        )
        return context


class ClubAdminView(LoginRequiredMixin, ClubViewMixin, HTMxTableView):
    """Admin panel for a club"""

    active_tab = "members"
    model = ClubMember
    table_class = ClubMemberHTMxTable
    filterset_class = ClubMemberFilter
    template_name = "auctions/club_admin.html"
    htmx_table_header_template = "auctions/partials/club_admin_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not request.user.is_superuser:
            member = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
            if not member or not any(
                [
                    member.permission_admin,
                    member.permission_view,
                    member.permission_export,
                    member.permission_add_edit,
                    member.permission_edit_club,
                    member.permission_manage_auctions,
                    member.permission_manage_bap,
                ]
            ):
                raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # is_deleted filtering is handled by ClubMemberFilter.filter_queryset (default: hide deactivated)
        return ClubMember.objects.filter(club=self.club).order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["can_edit"] = self.user_has_club_permission("permission_edit_club")
        context["can_export"] = self.user_has_club_permission("permission_export")
        context["can_add_edit"] = self.user_has_club_permission("permission_add_edit")
        context["can_edit_bap"] = self.user_has_club_permission("permission_manage_bap")
        query = (self.request.GET.get("query") or "").strip()
        filterset = context.get("filter")
        filtered_empty = filterset is not None and query and not filterset.qs.exists()
        if filtered_empty and "deactivated" not in query.lower().split():
            context["no_results"] = self._build_no_results_html(query, context["can_add_edit"])
        return context

    def _build_no_results_html(self, query, can_add_edit):
        """Empty-state with a link to expand the search to deactivated members and (optionally) an
        Add member button pre-populated from the search query."""
        from urllib.parse import urlencode

        from django.utils.html import format_html

        deactivated_query = f"{query} deactivated"
        deactivated_qs = ClubMemberFilter({"query": deactivated_query}, queryset=self.get_queryset()).qs
        deactivated_count = deactivated_qs.count()

        bits = [
            format_html(
                '<p class="text-muted mb-2">No active members match <strong>{}</strong>.</p>',
                query,
            )
        ]
        if deactivated_count:
            link_params = self.request.GET.copy()
            link_params["query"] = deactivated_query
            link_href = f"?{urlencode(link_params)}"
            bits.append(
                format_html(
                    '<a href="{}" class="btn btn-sm btn-danger me-2">'
                    '<i class="bi bi-archive"></i> Show {} deactivated</a>',
                    link_href,
                    deactivated_count,
                )
            )
        if can_add_edit:
            import re as _re

            params = {}
            digits_only = _re.sub(r"\D", "", query)
            if len(digits_only) >= 7:
                params["phone"] = query
            elif "@" in query:
                params["email"] = query
            elif _re.fullmatch(r"[A-Za-z\s\-'.]+", query) and len(query) >= 2:
                params["name"] = query
            create_url = reverse("clubmember_create", kwargs={"slug": self.club.slug})
            if params:
                create_url += f"?{urlencode(params)}"
            bits.append(
                format_html(
                    '<button class="btn btn-info btn-sm" '
                    'hx-get="{}" hx-target="#modals-here" hx-trigger="click" '
                    '_="on htmx:afterOnLoad wait 10ms then add .show to #modal then add .show to #modal-backdrop">'
                    '<i class="bi bi-person-fill-add"></i> Add member</button>',
                    create_url,
                )
            )
        body = "".join(str(b) for b in bits)
        return format_html('<div class="text-center py-3">{}</div>', mark_safe(body))

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["can_add_edit"] = self.user_has_club_permission("permission_add_edit")
        kwargs["can_manage_permissions"] = self.user_has_club_permission("permission_admin")
        kwargs["club_has_fee"] = bool(self.club.membership_annual_fee)
        kwargs["can_manage_discord"] = bool(
            self.club.discord_server_id
            and (
                self.user_has_club_permission("permission_admin")
                or self.user_has_club_permission("permission_edit_club")
            )
        )
        # Column visibility uses direct field checks — permission_admin alone doesn't reveal all columns
        if self.request.user.is_superuser:
            kwargs["can_manage_bap"] = True
            kwargs["can_manage_membership"] = True
            kwargs["can_manage_auctions"] = True
            kwargs["can_manage_discord"] = bool(self.club.discord_server_id)
        else:
            member = ClubMember.objects.filter(club=self.club, user=self.request.user, is_deleted=False).first()
            kwargs["can_manage_bap"] = bool(member and member.permission_manage_bap)
            kwargs["can_manage_membership"] = bool(member and member.permission_add_edit)
            kwargs["can_manage_auctions"] = bool(member and member.permission_manage_auctions)
        return kwargs

    def get_possible_filters(self):
        """Clickable chips that inject ClubMemberFilter search tokens, modeled on the auction
        users page. Each chip's key is normalized (underscores -> spaces) into a search token."""
        filters = []
        # Membership status only exists when the club charges dues (a 0 fee means no membership
        # system, so hide the paid/unpaid chips).
        if self.club.membership_annual_fee:
            filters.extend(
                [
                    ("<small class='text-muted'>Membership:</small>", ""),
                    ("<i class='bi bi-person-badge'></i> Paid club member", "current"),
                    ("<i class='bi bi-person'></i> Unpaid", "expired"),
                    ("<i class='bi bi-hourglass-split'></i> Expiring soon", "expiring"),
                    ("<i class='bi bi-person-x'></i> Never paid", "never"),
                ]
            )
        filters.append(("<small class='text-muted'>Source:</small>", ""))
        filters.extend(
            [
                ("<i class='bi bi-globe'></i> Website signup", "joined"),
                ("<i class='bi bi-pencil'></i> Manually added", "manual"),
            ]
        )
        if self.club.discord_server_id:
            filters.append(("<i class='bi bi-discord'></i> Discord", "discord"))
        filters.append(("<small class='text-muted'>Other:</small>", ""))
        filters.append(("<i class='bi bi-people-fill'></i> Possible duplicate", "duplicate"))
        if self.club.mailchimp_connected:
            filters.append(("<i class='bi bi-envelope-exclamation'></i> Not in Mailchimp", "nonmailchimp"))
        if self.club.brevo_connected:
            filters.append(("<i class='bi bi-envelope-exclamation'></i> Not in Brevo", "nonbrevo"))
        filters.append(("<i class='bi bi-archive'></i> Deactivated", "deactivated"))
        return filters


class ClubMemberValidation(ClubViewMixin, APIPostView):
    """Real-time validation for the club member add/edit form.

    Returns JSON with tooltip messages for duplicate name/email detection and
    auto-fill suggestions from existing club member records.
    """

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not check_club_permission(request.user, self.club, "permission_add_edit"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        try:
            pk = int(request.POST.get("pk") or 0) or None
        except (ValueError, TypeError):
            pk = None
        # In check-in create mode the form has no pk but may carry the pk of an already-matched
        # existing member; exclude that member so its own bidder_number/email don't flag as duplicates.
        try:
            existing_member_pk = int(request.POST.get("existing_member_pk") or 0) or None
        except (ValueError, TypeError):
            existing_member_pk = None
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        result = {
            "id_name": "",
            "id_email": "",
            "id_phone_number": "",
            "id_address": "",
            "name_tooltip": "",
            "email_tooltip": "",
            "bidder_number_tooltip": "",
        }
        bidder_number = request.POST.get("bidder_number", "").strip()
        base_qs = ClubMember.objects.filter(club=self.club, is_deleted=False)
        deactivated_qs = ClubMember.objects.filter(club=self.club, is_deleted=True)
        if pk:
            base_qs = base_qs.exclude(pk=pk)
            deactivated_qs = deactivated_qs.exclude(pk=pk)
        # For email/bidder_number duplicate checks only, also exclude the already-matched existing
        # member so its own values don't flag as duplicates. The name check intentionally still
        # finds that member to keep returning id_existing_member_pk on later blurs.
        contact_base_qs = base_qs
        contact_deactivated_qs = deactivated_qs
        if existing_member_pk and not pk:
            contact_base_qs = contact_base_qs.exclude(pk=existing_member_pk)
            contact_deactivated_qs = contact_deactivated_qs.exclude(pk=existing_member_pk)
        # Auto-fill from manageable club members or auction histories when name typed without email.
        if name and not email and not pk:
            member_match = (
                ClubMember.objects.filter(
                    club_id__in=club_ids_available_for_contact_autofill(request.user), is_deleted=False
                )
                .filter(name__iexact=name)
                .order_by("-createdon")
                .first()
            )
            if member_match:
                result["id_name"] = member_match.name
                result["id_email"] = member_match.email or ""
                result["id_phone_number"] = member_match.phone_number or ""
                result["id_address"] = member_match.address or ""
            else:
                old_auctions = auctions_available_for_contact_autofill(request.user)
                tos_qs = AuctionTOS.objects.filter(auction__in=old_auctions, email__isnull=False).order_by("-createdon")
                old_tos = AuctionTOSFilter.generic(None, tos_qs, name, match_names_only=True).first()
                if old_tos:
                    result["id_name"] = old_tos.name
                    result["id_email"] = old_tos.email
                    result["id_phone_number"] = old_tos.phone_number or ""
                    result["id_address"] = old_tos.address or ""
        # Duplicate name check within this club (active and deactivated). Use the same exact-or-rhyming
        # match as AuctionTOSFilter.generic so e.g. "Dave Banks" surfaces an existing "David Banks".
        if name:
            name_q = Q(name__iexact=name) | rhyming_name_q(name)
            dup = base_qs.filter(name_q).first()
            if dup:
                result["name_tooltip"] = f"{dup} is already in this club"
                # Return full member data so the create form can pre-fill and check in
                result["id_existing_member_pk"] = dup.pk
                result["id_name"] = dup.name
                result["id_email"] = dup.email or ""
                result["id_phone_number"] = dup.phone_number or ""
                result["id_address"] = dup.address or ""
                result["id_bidder_number"] = dup.bidder_number or ""
            elif deactivated_qs.filter(name_q).exists():
                result["name_tooltip"] = "Name matches a deactivated member"
        # Duplicate email check within this club (active and deactivated)
        if email:
            dup = contact_base_qs.filter(email=email).first()
            if dup:
                result["email_tooltip"] = "Email is already in this club"
            elif contact_deactivated_qs.filter(email=email).exists():
                result["email_tooltip"] = "Email matches a deactivated member"
        if bidder_number:
            dup = contact_base_qs.filter(bidder_number=bidder_number).first()
            if dup:
                result["bidder_number_tooltip"] = "Bidder number is already in this club"
            elif contact_deactivated_qs.filter(bidder_number=bidder_number).exists():
                result["bidder_number_tooltip"] = "Bidder number matches a deactivated member"
        return JsonResponse(result)


class ClubMemberAdminView(APIView):
    """DRF-based HTMX view for editing a club member.

    Supports an optional ``tos`` query-string parameter with an AuctionTOS pk.
    When present the form shows auction-scoped fields (pickup_location,
    is_club_member) and hides club-wide fields (contact_status, Discord).
    Saving writes TOS-specific fields to the AuctionTOS and everything else to
    the ClubMember.
    """

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _redirect_to_club_admin(club):
        """Close the modal and reload the page to reflect changes."""
        return close_modal_response("reload-page")

    def _get_member_and_check_permission(self, request, pk):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not (
            check_club_permission(request.user, member.club, "permission_view")
            or check_club_permission(request.user, member.club, "permission_manage_auctions")
        ):
            raise PermissionDenied()
        return member

    def _get_auctiontos(self, request, member):
        """Return the AuctionTOS from the ``tos`` query param, or None."""
        tos_pk = request.query_params.get("tos") or request.POST.get("_tos_pk")
        if not tos_pk:
            return None
        try:
            tos = AuctionTOS.objects.select_related("auction").get(pk=tos_pk, clubmember=member)
        except AuctionTOS.DoesNotExist:
            return None
        return tos

    def _build_context(self, request, member, form, read_only=False, auctiontos=None):
        validation_url = reverse("clubmember_validation", kwargs={"slug": member.club.slug})
        extra_script = self._get_validation_script(request, pk=member.pk, validation_url=validation_url)
        # Header: "{name} - {member_number}" when the club uses membership numbers
        title = str(member)
        if member.club.show_member_barcode and member.membership_number:
            title = f"{member} — #{member.membership_number}"
        ctx = {
            "club": member.club,
            "club_member": member,
            "modal_title": title,
            "form": form,
            "extra_script": mark_safe(extra_script),
            "read_only": read_only,
        }
        # When opened from an auction's user list (via ?tos=), surface the invoice
        # summary and status controls in the modal header exactly like AuctionTOSAdmin does.
        if auctiontos:
            try:
                invoice = auctiontos.invoice
                ctx["modal_title"] = f"{title} {invoice.invoice_summary_short}"
                ctx["top_buttons"] = render_to_string("invoice_buttons.html", {"invoice": invoice})
                ctx["unsold_lot_warning"] = invoice.unsold_lot_warning
                ctx["invoice"] = invoice
                ctx["is_admin"] = True
            except AttributeError:
                pass
        return ctx

    @staticmethod
    def _get_validation_script(request, pk, validation_url, checkin_auction=None):
        pk_js = f"var member_pk={pk};" if pk else "var member_pk=null;"
        csrf = get_token(request)
        # In check-in create mode (no pk, auction present) we support selecting existing members
        is_checkin_create_js = "true" if (not pk and checkin_auction) else "false"
        return f"""<script>
{pk_js}
var clubmember_validation_url = '{validation_url}';
var clubmember_csrf_token = '{csrf}';
var cm_is_checkin_create = {is_checkin_create_js};

function cmSetFieldInvalid(fieldId, message, is_invalid) {{
    var field = document.getElementById(fieldId);
    if (!field) return;
    var feedbackId = fieldId + "_feedback";
    var feedback = document.getElementById(feedbackId);
    if (is_invalid) {{
        field.classList.add("is-invalid");
        var existing_error = document.getElementById("error_1_" + fieldId);
        if (existing_error) existing_error.remove();
        if (feedback) feedback.remove();
        feedback = document.createElement("div");
        feedback.id = feedbackId;
        feedback.className = "invalid-feedback";
        field.parentNode.appendChild(feedback);
        feedback.textContent = message;
    }} else {{
        field.classList.remove("is-invalid");
        if (feedback) feedback.remove();
    }}
}}

function cmClearExistingMemberPk() {{
    var nameField = document.getElementById('id_name');
    var modalForm = nameField ? nameField.closest('form') : document.querySelector('#modal form');
    var hidden = modalForm ? modalForm.querySelector('input[name="_existing_member_pk"]') : null;
    if (hidden) hidden.value = '';
}}

function cmShowAutocomplete(response, remove) {{
    var feedback = document.getElementById('id_name_feedback');
    if (feedback) feedback.remove();
    if (remove) return;
    feedback = document.createElement("div");
    feedback.id = "id_name_feedback";
    feedback.className = "valid-feedback d-block cursor-pointer";
    var btn = document.createElement("button");
    btn.role = "button";
    btn.className = "btn btn-sm btn-info";
    btn.id = "autocompleteMemberForm";
    var isCheckinExisting = cm_is_checkin_create && !!response.id_existing_member_pk;
    if (isCheckinExisting) {{
        btn.textContent = "Click to check in " + (response.id_name || "this member");
        btn.classList.add("btn-success");
        btn.classList.remove("btn-info");
    }} else {{
        btn.textContent = response.id_email ? "Click to fill in " + response.id_email : "Click to fill in details";
    }}
    feedback.appendChild(btn);
    var autocomplete = response;
    document.getElementById('id_name').parentNode.appendChild(feedback);
    var link = document.getElementById('autocompleteMemberForm');
    link.addEventListener('click', function(event) {{
        event.preventDefault();
        for (var key in autocomplete) {{
            if (autocomplete.hasOwnProperty(key) && key.startsWith('id_')) {{
                var element = document.getElementById(key);
                if (element && element.type !== "checkbox") {{
                    element.value = autocomplete[key] || '';
                }}
            }}
        }}
        // In check-in mode with an existing member, store their pk for the POST handler.
        // Anchor to the form that actually contains id_name (the modal form) — the page may
        // have other forms (filter on auction_users, search on club_admin) that would otherwise
        // win document.querySelector('form').
        if (isCheckinExisting) {{
            var nameField = document.getElementById('id_name');
            var modalForm = nameField ? nameField.closest('form') : document.querySelector('#modal form');
            var hidden = modalForm ? modalForm.querySelector('input[name="_existing_member_pk"]') : null;
            if (!hidden && modalForm) {{
                hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = '_existing_member_pk';
                modalForm.appendChild(hidden);
            }}
            if (hidden) hidden.value = autocomplete.id_existing_member_pk || '';
        }}
    }});
    link.focus();
}}

function cmHasAutocompleteData(response) {{
    return !!(response.id_email || response.id_phone_number || response.id_address || response.id_existing_member_pk);
}}

function cmSetFieldNote(fieldId, message) {{
    var field = document.getElementById(fieldId);
    if (!field) return;
    var noteId = fieldId + "_note";
    var note = document.getElementById(noteId);
    if (note) note.remove();
    if (!message) return;
    note = document.createElement("div");
    note.id = noteId;
    note.className = "text-warning small mt-1";
    note.textContent = message;
    field.parentNode.appendChild(note);
}}

function cmValidateField() {{
    var nameField = document.getElementById('id_name');
    var modalForm = nameField ? nameField.closest('form') : document.querySelector('#modal form');
    var existingHidden = modalForm ? modalForm.querySelector('input[name="_existing_member_pk"]') : null;
    var data = {{
        pk: member_pk,
        name: $("#id_name").val(),
        email: $("#id_email").val(),
        bidder_number: $("#id_bidder_number").val(),
        existing_member_pk: existingHidden ? existingHidden.value : "",
    }};
    $.ajax({{
        url: clubmember_validation_url,
        type: "POST",
        data: data,
        headers: {{ "X-CSRFToken": clubmember_csrf_token }},
        success: function(response) {{
            if (response.name_tooltip && !cm_is_checkin_create) {{
                // Non-checkin context: just show warning, no autocomplete
                cmSetFieldNote("id_name", response.name_tooltip);
                cmShowAutocomplete(response, true);
            }} else if (response.name_tooltip && cm_is_checkin_create && response.id_existing_member_pk) {{
                // Check-in context: existing member found — show check-in button, clear warning
                cmSetFieldNote("id_name", "");
                cmShowAutocomplete(response, false);
            }} else if (cmHasAutocompleteData(response)) {{
                cmShowAutocomplete(response);
                cmSetFieldNote("id_name", "");
                cmClearExistingMemberPk();
            }} else {{
                cmSetFieldNote("id_name", "");
                cmShowAutocomplete(response, true);
                cmClearExistingMemberPk();
            }}
            cmSetFieldInvalid("id_email", response.email_tooltip, !!response.email_tooltip);
            cmSetFieldInvalid("id_bidder_number", response.bidder_number_tooltip, !!response.bidder_number_tooltip);
        }}
    }});
}}

$("#id_name, #id_email, #id_bidder_number").on("blur", cmValidateField);
</script>"""

    def _post_url(self, member, auctiontos=None):
        url = reverse("clubmember_admin", kwargs={"pk": member.pk})
        if auctiontos:
            url += f"?tos={auctiontos.pk}"
        return url

    def get(self, request, pk):
        member = self._get_member_and_check_permission(request, pk)
        auctiontos = self._get_auctiontos(request, member)
        read_only = not check_club_permission(request.user, member.club, "permission_add_edit")
        post_url = None if read_only else self._post_url(member, auctiontos)
        form = ClubMemberAdminForm(
            instance=member, post_url=post_url, read_only=read_only, club=member.club, auctiontos=auctiontos
        )
        return render(
            request,
            "auctions/generic_admin_form.html",
            self._build_context(request, member, form, read_only=read_only, auctiontos=auctiontos),
        )

    def post(self, request, pk):
        member = self._get_member_and_check_permission(request, pk)
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        auctiontos = self._get_auctiontos(request, member)
        post_url = self._post_url(member, auctiontos)
        form = ClubMemberAdminForm(
            request.POST, instance=member, post_url=post_url, club=member.club, auctiontos=auctiontos
        )
        if form.is_valid():
            saved = form.save()
            # If in auction context, also save TOS-specific fields to the AuctionTOS
            if auctiontos:
                auction = auctiontos.auction
                tos_update_fields = ["is_club_member"]
                if form.cleaned_data.get("pickup_location") is not None:
                    auctiontos.pickup_location = form.cleaned_data["pickup_location"]
                    tos_update_fields.append("pickup_location_id")
                if auction.alternate_split_mode == "club_member":
                    # Auto-managed: paid club members (or an invoice renewing their
                    # membership) get the alternate split.
                    invoice = auctiontos.invoice
                    auctiontos.is_club_member = invoice.treat_as_club_member if invoice else saved.is_paid_member
                elif auction.alternate_split_mode == "custom":
                    auctiontos.is_club_member = form.cleaned_data.get("is_club_member", auctiontos.is_club_member)
                # Sync bidding/selling permissions to AuctionTOS when the auction uses them
                if auction.only_approved_sellers and "selling_allowed" in form.cleaned_data:
                    auctiontos.selling_allowed = form.cleaned_data["selling_allowed"]
                    tos_update_fields.append("selling_allowed")
                if auction.only_approved_bidders and "bidding_allowed" in form.cleaned_data:
                    auctiontos.bidding_allowed = form.cleaned_data["bidding_allowed"]
                    tos_update_fields.append("bidding_allowed")
                auctiontos.save(update_fields=tos_update_fields)
                ClubHistory.objects.create(
                    club=member.club,
                    user=request.user,
                    action=f"Updated member {saved} via auction {auctiontos.auction}",
                    applies_to="MEMBERS",
                )
            else:
                ClubHistory.objects.create(
                    club=member.club,
                    user=request.user,
                    action=f"Updated member {saved}",
                    applies_to="MEMBERS",
                )
            messages.success(request, f"{saved} updated.")
            return self._redirect_to_club_admin(member.club)
        return render(
            request,
            "auctions/generic_admin_form.html",
            self._build_context(request, member, form, auctiontos=auctiontos),
        )


class ClubMemberPermissionsView(LoginRequiredMixin, View):
    """Admin-only HTMx dialog to set permission bool fields on a ClubMember."""

    def _get_member(self, request, pk):
        member = get_object_or_404(ClubMember, pk=pk, is_deleted=False)
        if not check_club_permission(request.user, member.club, "permission_admin"):
            raise PermissionDenied
        return member

    def get(self, request, pk):
        member = self._get_member(request, pk)
        post_url = reverse("clubmember_permissions", kwargs={"pk": pk})
        form = ClubMemberPermissionsForm(instance=member, post_url=post_url)
        return render(
            request,
            "auctions/generic_admin_form.html",
            {"form": form, "modal_title": f"Permissions — {member.display_name}"},
        )

    def post(self, request, pk):
        member = self._get_member(request, pk)
        post_url = reverse("clubmember_permissions", kwargs={"pk": pk})
        form = ClubMemberPermissionsForm(request.POST, instance=member, post_url=post_url)
        if form.is_valid():
            form.save()
            ClubHistory.objects.create(
                club=member.club,
                user=request.user,
                action=f"Updated roles for {member}",
                applies_to="MEMBERS",
            )
            return ClubMemberAdminView._redirect_to_club_admin(member.club)
        return render(
            request,
            "auctions/generic_admin_form.html",
            {"form": form, "modal_title": f"Permissions — {member.display_name}"},
        )


class ClubMemberDiscordAdminView(LoginRequiredMixin, View):
    """HTMX modal for managing a club member's Discord integration settings.

    Only accessible to users with permission_admin or permission_edit_club.
    """

    def _get_member(self, request, pk):
        member = get_object_or_404(ClubMember, pk=pk, is_deleted=False)
        if not (
            check_club_permission(request.user, member.club, "permission_admin")
            or check_club_permission(request.user, member.club, "permission_edit_club")
        ):
            raise PermissionDenied()
        return member

    _EXTRA_SCRIPT = mark_safe(
        """<script>
(function() {
    var clearBtn = document.getElementById('clear-discord-id-btn');
    if (clearBtn) {
        clearBtn.addEventListener('click', function() {
            var input = document.getElementById('id_discord_id');
            if (input) {
                input.removeAttribute('readonly');
                input.value = '';
                input.focus();
            }
            this.style.display = 'none';
        });
    }
    var autoCheckbox = document.getElementById('id_discord_role_auto_managed');
    var overrideWrapper = document.querySelector('.discord-role-override-field');
    if (autoCheckbox && overrideWrapper) {
        function updateDiscordRoleOverride() {
            overrideWrapper.style.display = autoCheckbox.checked ? 'none' : '';
        }
        updateDiscordRoleOverride();
        autoCheckbox.addEventListener('change', updateDiscordRoleOverride);
    }
})();
</script>"""
    )

    def _build_context(self, member, form):
        subtitle_parts = []
        if member.discord_username:
            subtitle_parts.append(member.discord_username)
        current_role = member.discord_role
        subtitle_parts.append(current_role.role_name if current_role else "No current role")
        subtitle = " · ".join(subtitle_parts)
        title = format_html("{} <small class='text-muted'>{}</small>", member, subtitle)
        return {
            "modal_title": title,
            "form": form,
            "extra_script": self._EXTRA_SCRIPT,
        }

    def get(self, request, pk):
        member = self._get_member(request, pk)
        post_url = reverse("clubmember_discord", kwargs={"pk": pk})
        form = ClubMemberDiscordForm(instance=member, post_url=post_url)
        return render(request, "auctions/generic_admin_form.html", self._build_context(member, form))

    def post(self, request, pk):
        member = self._get_member(request, pk)
        post_url = reverse("clubmember_discord", kwargs={"pk": pk})
        form = ClubMemberDiscordForm(request.POST, instance=member, post_url=post_url)
        if form.is_valid():
            saved = form.save()
            ClubHistory.objects.create(
                club=member.club,
                user=request.user,
                action=f"Updated Discord settings for {saved}",
                applies_to="MEMBERS",
            )
            messages.success(request, f"Discord settings for {saved} updated.")
            return close_modal_response("reload-page")
        return render(request, "auctions/generic_admin_form.html", self._build_context(member, form))


class ClubMemberCreateView(APIView):
    """DRF-based HTMX view for creating a new club member.

    Supports an optional ``auction`` query-string parameter (auction slug).
    When present and the auction is in check-in mode, a linked AuctionTOS is
    created automatically after the ClubMember is saved.
    """

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_club_and_check_permission(self, request, slug):
        club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_add_edit"):
            raise PermissionDenied()
        return club

    def _get_auction_context(self, request, club):
        """Return an Auction if the ?auction= param is set and valid for this club."""
        auction_slug = request.query_params.get("auction") or request.POST.get("_auction_slug")
        if not auction_slug:
            return None
        auction = Auction.objects.filter(
            slug=auction_slug, club=club, is_deleted=False, manage_users_through_club="checkin"
        ).first()
        return auction

    def _post_url(self, slug, auction=None):
        url = reverse("clubmember_create", kwargs={"slug": slug})
        if auction:
            url += f"?auction={auction.slug}"
        return url

    def get(self, request, slug):
        club = self._get_club_and_check_permission(request, slug)
        auction = self._get_auction_context(request, club)
        post_url = self._post_url(slug, auction)
        validation_url = reverse("clubmember_validation", kwargs={"slug": slug})
        # Pre-populate from URL params (name, email, phone) when coming from no-results search
        initial = {}
        for field, param in (("name", "name"), ("email", "email"), ("phone_number", "phone")):
            val = request.query_params.get(param, "").strip()
            if val:
                initial[field] = val
        form = ClubMemberAdminForm(post_url=post_url, club=club, auction=auction, initial=initial or None)
        extra_script = ClubMemberAdminView._get_validation_script(
            request, pk=None, validation_url=validation_url, checkin_auction=auction
        )
        title = f"Add member to {club.name}"
        if auction:
            title += f" — {auction}"
        context = {
            "club": club,
            "modal_title": title,
            "form": form,
            "extra_script": mark_safe(extra_script),
        }
        return render(request, "auctions/generic_admin_form.html", context)

    @staticmethod
    def _create_auction_tos(auction, member, form_cleaned_data):
        """Create an AuctionTOS for *member* in *auction*, applying form overrides."""
        pickup_location = form_cleaned_data.get("pickup_location") or _default_pickup_location_for_auction(auction)
        if not pickup_location:
            return None
        if auction.alternate_split_mode == "club_member":
            # The alternate split is applied automatically to paid club members.
            is_club_member = member.is_paid_member
        else:
            is_club_member = form_cleaned_data.get("is_club_member", False)
        bidding_allowed = member.bidding_allowed
        selling_allowed = member.selling_allowed
        if auction.only_approved_bidders and "bidding_allowed" in form_cleaned_data:
            bidding_allowed = form_cleaned_data["bidding_allowed"]
        if auction.only_approved_sellers and "selling_allowed" in form_cleaned_data:
            selling_allowed = form_cleaned_data["selling_allowed"]
        if auction.use_check_in_mode:
            bidding_allowed = True
        return _upsert_clubmember_shadow_tos(
            auction,
            member,
            pickup_location=pickup_location,
            is_club_member=is_club_member,
            bidding_allowed=bidding_allowed,
            selling_allowed=selling_allowed,
            checked_in_at=timezone.now() if auction.use_check_in_mode else _UNSET,
        )

    def post(self, request, slug):
        club = self._get_club_and_check_permission(request, slug)
        auction = self._get_auction_context(request, club)
        post_url = self._post_url(slug, auction)

        # Check if the user is checking in an existing club member
        existing_pk = request.POST.get("_existing_member_pk")
        existing_member = None
        if existing_pk and auction:
            try:
                existing_member = ClubMember.objects.get(pk=existing_pk, club=club, is_deleted=False)
            except ClubMember.DoesNotExist:
                pass

        if existing_member:
            # Existing member check-in: create AuctionTOS without creating a new ClubMember.
            # We still validate auction-specific fields via a partial form.
            form = ClubMemberAdminForm(
                request.POST, instance=existing_member, post_url=post_url, club=club, auction=auction
            )
            if form.is_valid():
                # Don't save the ClubMember itself (no changes intended from check-in form)
                tos = self._create_auction_tos(auction, existing_member, form.cleaned_data)
                action_detail = f"Checked in existing member {existing_member} to auction {auction}"
                if not tos:
                    messages.warning(request, f"{existing_member} could not be added — no pickup location found.")
                else:
                    messages.success(request, f"{existing_member} checked in to {auction}.")
                AuctionHistory.objects.create(
                    auction=auction, user=request.user, action=action_detail, applies_to="USERS"
                )
                return ClubMemberAdminView._redirect_to_club_admin(club)
            extra_script = ClubMemberAdminView._get_validation_script(
                request,
                pk=None,
                validation_url=reverse("clubmember_validation", kwargs={"slug": slug}),
                checkin_auction=auction,
            )
            title = f"Add member to {club.name}"
            if auction:
                title += f" — {auction}"
            context = {
                "club": club,
                "modal_title": title,
                "form": form,
                "extra_script": mark_safe(extra_script),
            }
            return render(request, "auctions/generic_admin_form.html", context)

        form = ClubMemberAdminForm(request.POST, post_url=post_url, club=club, auction=auction)
        if form.is_valid():
            member = form.save(commit=False)
            member.club = club
            member.added_by = request.user
            member.source = str(auction.title)[:200] if auction else "manually_added"
            member.save()
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Added member {member}",
                applies_to="MEMBERS",
            )
            # In check-in mode, also create a linked AuctionTOS for this auction
            if auction:
                tos = self._create_auction_tos(auction, member, form.cleaned_data)
                if not tos:
                    messages.warning(
                        request, f"{member} added to {club.name}, but no pickup location found for {auction}."
                    )
                else:
                    messages.success(request, f"{member} added to {club.name} and checked in to {auction}.")
                AuctionHistory.objects.create(
                    auction=auction,
                    user=request.user,
                    action=f"Added new member {member} to club '{club.name}' via auction check-in",
                    applies_to="USERS",
                )
            else:
                messages.success(request, f"{member} added to {club.name}.")
            return ClubMemberAdminView._redirect_to_club_admin(club)
        extra_script = ClubMemberAdminView._get_validation_script(
            request,
            pk=None,
            validation_url=reverse("clubmember_validation", kwargs={"slug": slug}),
            checkin_auction=auction,
        )
        title = f"Add member to {club.name}"
        if auction:
            title += f" — {auction}"
        context = {
            "club": club,
            "modal_title": title,
            "form": form,
            "extra_script": mark_safe(extra_script),
        }
        return render(request, "auctions/generic_admin_form.html", context)


class ClubMemberRenewView(APIView):
    """Renew a club member's membership, extending the current expiration by one year."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_member(self, pk, request):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        return member

    def _new_expiration(self, member, today):
        return _compute_member_renewal_expiration(member.club, member, today)

    def get(self, request, pk):
        member = self._get_member(pk, request)
        today = timezone.now().date()
        context = {
            "member": member,
            "new_expiration": self._new_expiration(member, today),
            "renew_url": reverse("club_member_renew", kwargs={"pk": pk}),
            "send_renewal_confirmation": member.club.send_membership_renewal_confirmation,
        }
        return render(request, "auctions/club_member_renew_confirm.html", context)

    def post(self, request, pk):
        member = self._get_member(pk, request)
        today = timezone.now().date()
        member.membership_expiration_date = self._new_expiration(member, today)
        member.membership_last_paid = today
        member.save(
            update_fields=[
                "membership_last_paid",
                "membership_expiration_date",
                "membership_expiration_reminder_30_days_due",
                "membership_expiration_reminder_due",
            ]
        )
        member.update_last_club_activity()
        new_exp_str = (
            member.membership_expiration_date.strftime("%-m/%-d/%Y") if member.membership_expiration_date else "unknown"
        )
        ClubHistory.objects.create(
            club=member.club,
            user=request.user,
            action=f"Renewed membership for {member}; new expiration {new_exp_str}",
            applies_to="MEMBERSHIP",
        )
        if member.club.membership_annual_fee:
            ClubMoney.objects.create(
                club=member.club,
                created_by=request.user,
                date=today,
                amount=member.club.membership_annual_fee,
                description=f"Manual membership renewal for {member}",
                category=ClubMoney.CATEGORY_MEMBERSHIP,
            )
        try:
            maybe_send_membership_renewal_confirmation(member)
        except Exception:
            logger.exception("Failed to send membership renewal confirmation for club member %s", member.pk)
        return close_modal_response(None, extra_triggers={"clubMemberListChanged": None})


class ClubMembershipNumberView(APIView):
    """Show a modal with the member's membership number and allow resetting it."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_member(self, pk, request):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        if not member.club.show_member_barcode:
            # Feature is off for this club — admin endpoint should not be reachable.
            raise Http404
        return member

    def get(self, request, pk):
        member = self._get_member(pk, request)
        return render(request, "auctions/club_membership_number.html", {"member": member})

    def post(self, request, pk):
        from auctions.models import _pick_unique_membership_number

        member = self._get_member(pk, request)
        member.membership_number = _pick_unique_membership_number()
        member.save(update_fields=["membership_number"])
        ClubHistory.objects.create(
            club=member.club,
            user=request.user,
            action=f"Reset membership number for {member}",
            applies_to="MEMBERS",
        )
        return render(request, "auctions/club_membership_number.html", {"member": member})


class ClubMemberAppleWalletPassView(LoginRequiredMixin, View):
    """Serve a signed .pkpass file for a member.

    Only the member's owning account may download — UUID renewal links must NOT
    be able to download someone else's wallet card. We use the same identity
    check as the Google Wallet save URL: request.user.id == member.user_id.
    """

    def get(self, request, pk):
        from auctions.apple_wallet import generate_pkpass_for_member, is_configured

        if not is_configured():
            raise Http404
        member = get_object_or_404(ClubMember, pk=pk, is_deleted=False)
        if not request.user.is_authenticated or member.user_id != request.user.id:
            raise PermissionDenied()
        # Honor the club's number-mode gating — disabled or (paid_only + unpaid) → 404.
        if not member.club.show_member_barcode:
            raise Http404
        pkpass_bytes = generate_pkpass_for_member(member)
        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        response["Content-Disposition"] = f'attachment; filename="{member.club.slug}-membership.pkpass"'
        # Wallet passes are personalized — don't cache them at intermediaries.
        response["Cache-Control"] = "private, no-store"
        return response


class ClubMemberAppleWalletByUUIDView(View):
    """UUID-keyed Apple Wallet download — no login required.

    Anyone with the UUID link can download the .pkpass; the UUID is the capability token.
    """

    def get(self, request, slug, uuid):
        from auctions.apple_wallet import generate_pkpass_for_member, is_configured

        if not is_configured():
            raise Http404
        member = get_object_or_404(ClubMember, club__slug=slug, uuid=uuid, is_deleted=False)
        if not member.club.show_member_barcode:
            raise Http404
        member.update_last_club_activity()
        pkpass_bytes = generate_pkpass_for_member(member)
        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        response["Content-Disposition"] = f'attachment; filename="{member.club.slug}-membership.pkpass"'
        response["Cache-Control"] = "private, no-store"
        return response


class ClubBarcodeView(View):
    """Render an SVG barcode for an arbitrary value.

    Public endpoint so the URL can be embedded in outgoing emails as an <img src>.
    The view only renders the barcode bars — no membership lookup, no caller validation.
    """

    def get(self, request, slug, value):
        import io as _io

        try:
            import barcode as _barcode
            from barcode.writer import SVGWriter as _SVGWriter
        except ImportError:
            raise Http404
        value = str(value or "")
        if not value or not value.isdigit():
            raise Http404
        try:
            cls = _barcode.get_barcode_class("code128")
            buf = _io.BytesIO()
            cls(value, writer=_SVGWriter()).write(buf, options={"write_text": False, "module_height": 12.0})
            svg = buf.getvalue().decode("utf-8")
        except Exception:
            raise Http404
        response = HttpResponse(svg, content_type="image/svg+xml")
        # Barcodes are stable for a given value — let the CDN / browser cache them.
        response["Cache-Control"] = "public, max-age=86400"
        return response


class ClubBarcodePNGView(View):
    """Render a PNG barcode for an arbitrary value.

    Public endpoint so the URL can be embedded in outgoing emails as an <img src>.
    PNG format renders better in email clients like Gmail than SVG.
    The view only renders the barcode bars — no membership lookup, no caller validation.
    """

    def get(self, request, slug, value):
        import io as _io

        try:
            import barcode as _barcode
            from barcode.writer import ImageWriter as _ImageWriter
        except ImportError:
            raise Http404
        value = str(value or "")
        if not value or not value.isdigit():
            raise Http404
        try:
            cls = _barcode.get_barcode_class("code128")
            buf = _io.BytesIO()
            cls(value, writer=_ImageWriter()).write(buf, options={"write_text": False, "module_height": 12.0})
            buf.seek(0)
            png_data = buf.getvalue()
        except Exception:
            raise Http404
        response = HttpResponse(png_data, content_type="image/png")
        # Barcodes are stable for a given value — let the CDN / browser cache them.
        response["Cache-Control"] = "public, max-age=86400"
        return response


BAP_EMBED_PROGRAM_FIELDS = {"bap": "bap_points", "hap": "hap_points", "cap": "culture_points"}
BAP_EMBED_PROGRAM_LABELS = {"bap": "BAP", "hap": "HAP", "cap": "CAP"}


def _bap_embed_leaderboard(club, program):
    """Top-10 leaderboard rows for a program: rank, display name, and points only.

    Deliberately exposes no PII — never emails, member numbers, or database ids. When a
    member has no name we fall back to a generic "Member N" label keyed off their rank.
    """
    field = BAP_EMBED_PROGRAM_FIELDS[program]
    members = ClubMember.objects.filter(club=club, is_deleted=False, **{f"{field}__gt": 0}).order_by(
        f"-{field}", "name"
    )[:10]
    rows = []
    for i, member in enumerate(members):
        name = (member.name or "").strip() or f"Member {i + 1}"
        rows.append({"rank": i + 1, "name": name, "points": getattr(member, field)})
    return rows


@method_decorator(xframe_options_exempt, name="dispatch")
class BapEmbedView(View):
    """Public, embeddable top-10 BAP/HAP/CAP leaderboard for a club.

    A single endpoint serves several representations via ?format= (json, iframelight,
    iframedark, unstyledhtml) and picks the program with ?program= (bap, hap, cap).
    Only the top-10 leaderboard is exposed, and only names + points — never emails,
    member numbers, or other PII. Framing (xframe_options_exempt) and cross-origin
    fetches (Access-Control-Allow-Origin) are allowed so third-party sites can embed it.
    GET-only and public, so CSRF never applies.
    """

    def _json_response(self, club, program, label, rows):
        response = JsonResponse({"club": club.name, "program": program, "program_label": label, "leaderboard": rows})
        response["Access-Control-Allow-Origin"] = "*"
        return response

    def get(self, request, slug):
        club = Club.objects.filter(Q(slug=slug) | Q(abbreviation=slug)).order_by("pk").first()
        if not club or not club.enable_breeder_award_program:
            raise Http404

        program = (request.GET.get("program") or "bap").strip().lower()
        if program not in BAP_EMBED_PROGRAM_FIELDS:
            program = "bap"
        # HAP/CAP only have their own standings when the club tracks them separately;
        # otherwise those points roll into BAP and a dedicated board would be misleading.
        if (program == "hap" and not club.separate_hap) or (program == "cap" and not club.separate_cap):
            raise Http404

        rows = _bap_embed_leaderboard(club, program)
        label = BAP_EMBED_PROGRAM_LABELS[program]
        fmt = (request.GET.get("format") or "json").strip().lower()

        if fmt in ("iframelight", "iframedark", "iframdark"):
            embed_mode = "dark" if fmt in ("iframedark", "iframdark") else "light"
        elif fmt == "unstyledhtml":
            embed_mode = "unstyled"
        else:
            # json or anything unrecognized falls back to JSON — a typo never leaks an unexpected page.
            return self._json_response(club, program, label, rows)

        html = render_to_string(
            "auctions/bap_embed.html",
            {
                "embed_mode": embed_mode,
                "club_name": club.name,
                "program_label": label,
                "leaderboard": rows,
            },
        )
        response = HttpResponse(html)
        response["Access-Control-Allow-Origin"] = "*"
        return response


class ClubBarcodeLabelsView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Print barcode stickers: bidder paddles, invoice adjustments, and member cards."""

    template_name = "auctions/club_barcode_labels.html"
    active_tab = "barcode_labels"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not self.can_access_admin:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _make_barcode_svg(value: str) -> str:
        import base64 as _b64
        import io as _io

        try:
            import barcode as _barcode
            from barcode.writer import ImageWriter as _IW
        except ImportError:
            return ""
        try:
            buf = _io.BytesIO()
            cls = _barcode.get_barcode_class("code128")
            cls(str(value), writer=_IW()).write(
                buf, options={"write_text": False, "module_height": 10.0, "quiet_zone": 2, "dpi": 300}
            )
            buf.seek(0)
            data = _b64.b64encode(buf.getvalue()).decode("ascii")
            return f'<img src="data:image/png;base64,{data}" style="width:100%;height:auto;display:block;">'
        except Exception:
            return ""

    @staticmethod
    def _label_dim_context(prefs):
        if prefs.preset == "sm":
            d = {
                "page_width": 8.5,
                "page_height": 11,
                "label_width": 2.55,
                "label_height": 0.99,
                "label_margin_right": 0.19,
                "label_margin_bottom": 0.01,
                "page_margin_top": 0.57,
                "page_margin_bottom": 0.1,
                "page_margin_left": 0.23,
                "page_margin_right": 0,
                "font_size": 10,
                "unit": "in",
            }
        elif prefs.preset == "lg":
            d = {
                "page_width": 8.5,
                "page_height": 11,
                "label_width": 3.85,
                "label_height": 1.2,
                "label_margin_right": 0.25,
                "label_margin_bottom": 0.13,
                "page_margin_top": 0.88,
                "page_margin_bottom": 0.6,
                "page_margin_left": 0.3,
                "page_margin_right": 0,
                "font_size": 13,
                "unit": "in",
            }
        elif prefs.preset == "thermal_sm":
            d = {
                "page_width": 3,
                "page_height": 2,
                "label_width": 2.78,
                "label_height": 1.9,
                "label_margin_right": 0,
                "label_margin_bottom": 0,
                "page_margin_top": 0.04,
                "page_margin_bottom": 0.04,
                "page_margin_left": 0.16,
                "page_margin_right": 0.04,
                "font_size": 13,
                "unit": "in",
            }
        elif prefs.preset == "thermal_very_sm":
            d = {
                "page_width": 3.5,
                "page_height": 1.125,
                "label_width": 3.3,
                "label_height": 1.025,
                "label_margin_right": 0,
                "label_margin_bottom": 0,
                "page_margin_top": 0.04,
                "page_margin_bottom": 0.04,
                "page_margin_left": 0.16,
                "page_margin_right": 0.04,
                "font_size": 12,
                "unit": "in",
            }
        else:
            d = {
                f.name: getattr(prefs, f.name)
                for f in UserLabelPrefs._meta.get_fields()
                if f.name not in ("id", "user", "preset", "empty_labels", "print_border") and hasattr(prefs, f.name)
            }
        unit_factor = 2.54 if d.get("unit") == "cm" else 1
        for k in (
            "label_width",
            "label_height",
            "label_margin_right",
            "label_margin_bottom",
            "page_margin_top",
            "page_margin_bottom",
            "page_margin_left",
            "page_margin_right",
            "page_width",
            "page_height",
        ):
            if k in d:
                d[k] = d[k] * unit_factor
        return d

    def _build_labels(self, params, members_qs):
        label_types = params.getlist("label_type")
        bidder_numbers = params.getlist("bidder_number")
        amounts = params.getlist("amount")
        label_texts = params.getlist("label_text")
        member_ids = params.getlist("member_id")

        labels = []
        for i, label_type in enumerate(label_types):
            if label_type == "bidder_paddle":
                n = (bidder_numbers[i] if i < len(bidder_numbers) else "").strip()
                if n:
                    svg = ClubBarcodeLabelsView._make_barcode_svg(f"11111{n}")
                    labels.append({"svg": svg, "text": f"Bidder #{n}"})

            elif label_type in ("charge", "discount"):
                amount_raw = (amounts[i] if i < len(amounts) else "").strip()
                label_text = (label_texts[i] if i < len(label_texts) else "").strip()
                try:
                    amount = int(float(amount_raw))
                except (ValueError, TypeError):
                    amount = 0
                if amount > 0 and label_text:
                    prefix = "010" if label_type == "charge" else "000"
                    svg = ClubBarcodeLabelsView._make_barcode_svg(f"{prefix}{amount}{label_text}")
                    sign = "" if label_type == "charge" else "-"
                    labels.append({"svg": svg, "text": f"{sign}${amount} {label_text}"})

            elif label_type == "member_card":
                member_id = (member_ids[i] if i < len(member_ids) else "").strip()
                if member_id:
                    try:
                        member = members_qs.get(pk=int(member_id))
                        if member.membership_number:
                            svg = ClubBarcodeLabelsView._make_barcode_svg(str(member.membership_number))
                            text = (member.name or member.email or str(member.membership_number)).strip()
                            labels.append({"svg": svg, "text": text})
                    except (ValueError, TypeError, ClubMember.DoesNotExist):
                        pass

        return labels

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        context["preset_name"] = dict(UserLabelPrefs.PRESETS).get(prefs.preset, prefs.preset)
        return context


class ClubBarcodeLabelsViewPDF(LoginRequiredMixin, ClubViewMixin, TemplateView, WeasyTemplateResponseMixin):
    """WeasyPrint PDF of barcode labels — exact same physical dimensions as lot labels."""

    template_name = "auctions/club_barcode_labels_print.html"
    pdf_attachment = True

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not self.can_access_admin:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_pdf_filename(self):
        return f"{self.club.slug}-barcodes.pdf"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        dims = ClubBarcodeLabelsView._label_dim_context(prefs)
        context.update(dims)
        context["print_border"] = prefs.print_border

        members = ClubMember.objects.filter(club=self.club, is_deleted=False, membership_number__isnull=False)
        labels = ClubBarcodeLabelsView._build_labels(self, self.request.GET, members)
        if not labels:
            raise Http404
        context["labels"] = labels

        available_width = dims["page_width"] - dims["page_margin_left"] - dims["page_margin_right"]
        available_height = dims["page_height"] - dims["page_margin_top"] - dims["page_margin_bottom"]
        labels_per_row = int(available_width // (dims["label_width"] + dims["label_margin_right"])) or 1
        labels_per_column = int(available_height // (dims["label_height"] + dims["label_margin_bottom"])) or 1
        context["labels_per_page"] = labels_per_row * labels_per_column
        return context


class ClubMemberDeleteView(APIView):
    """Soft-delete a club member."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        member.is_deleted = True
        member.save(update_fields=["is_deleted"])
        ClubHistory.objects.create(
            club=member.club,
            user=request.user,
            action=f"Deactivated member {member}",
            applies_to="MEMBERS",
        )
        return close_modal_response(None, extra_triggers={"clubMemberListChanged": None})


class ClubMemberReactivateView(APIView):
    """Reactivate a deactivated (soft-deleted) club member."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        member.is_deleted = False
        member.save(update_fields=["is_deleted"])
        ClubHistory.objects.create(
            club=member.club,
            user=request.user,
            action=f"Reactivated member {member}",
            applies_to="MEMBERS",
        )
        # Return 200 with HX-Trigger so the event fires on the link element (which stays in the DOM)
        # and bubbles to body where the table container is listening.
        return HttpResponse("", headers={"HX-Trigger": "clubMemberListChanged"})


class ClubMemberPermanentDeleteView(APIView):
    """Hard-delete a club member that has already been deactivated (is_deleted=True)."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        if not member.is_deleted:
            raise PermissionDenied()
        club = member.club
        member_name = str(member)
        member.delete()
        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Permanently deleted member {member_name}",
            applies_to="MEMBERS",
        )
        return close_modal_response(None, extra_triggers={"clubMemberListChanged": None})


class ClubMemberConfirmView(APIView):
    """Show a Bootstrap modal asking the user to confirm a destructive action (e.g. delete)."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, action):
        try:
            member = ClubMember.objects.get(pk=pk)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        if action == "delete":
            body = format_html(
                "<small>Disable this member's membership. They won't appear in searches, or be able to view/renew their membership. You can reactivate or permanently delete them later.</small><br>Deactivate {}?",
                member,
            )
            action_url = reverse("club_member_delete", kwargs={"pk": pk})
            context = {
                "title": f"Deactivate {member}?",
                "body": body,
                "action_url": action_url,
            }
        elif action == "permanent_delete":
            if not member.is_deleted:
                raise Http404
            action_url = reverse("club_member_permanent_delete", kwargs={"pk": pk})
            context = {
                "title": f"Delete {member}?",
                "body": "This cannot be undone.",
                "action_url": action_url,
                "confirm_button_label": "Delete",
            }
        else:
            raise Http404
        return render(request, "auctions/club_member_confirm.html", context)


class ClubMemberRenewPageView(LoginRequiredMixin, ClubViewMixin, View):
    """Set a club member's expiration date directly (manual override)."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_add_edit"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def _get_member(self, pk):
        return get_object_or_404(ClubMember, pk=pk, club=self.club, is_deleted=False)

    def get(self, request, slug, pk):
        member = self._get_member(pk)
        context = {
            "club": self.club,
            "member": member,
            "default_date": member.membership_expiration_date or timezone.now().date(),
            "next_url": request.GET.get("next", ""),
        }
        return render(request, "auctions/club_member_renew_page.html", context)

    def post(self, request, slug, pk):
        member = self._get_member(pk)
        next_url = request.POST.get("next", "")
        date_str = request.POST.get("membership_expiration_date", "")
        try:
            new_expiration = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=date_tz.utc).date()
        except (ValueError, TypeError):
            messages.error(request, "Invalid date.")
            error_redirect = reverse("club_member_renew_page", kwargs={"slug": slug, "pk": pk})
            if next_url:
                error_redirect += "?" + urlencode({"next": next_url})
            return redirect(error_redirect)
        old_expiration = member.membership_expiration_date
        member.membership_expiration_date = new_expiration
        member._preserve_membership_email_schedule = True
        member.save(
            update_fields=[
                "membership_expiration_date",
                "membership_expiration_reminder_30_days_due",
                "membership_expiration_reminder_due",
            ]
        )
        old_str = old_expiration.strftime("%-m/%-d/%Y") if old_expiration else "none"
        new_str = new_expiration.strftime("%-m/%-d/%Y")
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Set membership expiration for {member}: {old_str} → {new_str}",
            applies_to="MEMBERSHIP",
        )
        messages.success(request, f"Expiration date updated for {member}.")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect(reverse("club_admin", kwargs={"slug": self.club.slug}))


class ClubMembershipPaymentView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Self-service membership payment page for club members.

    Creates a pending club membership Invoice and shows PayPal/Square payment buttons.
    """

    template_name = "auctions/club_membership_payment.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if not (
            self.club.enable_club_page
            and self.club.membership_annual_fee
            and (self.club.can_accept_paypal or self.club.can_accept_square)
        ):
            raise Http404
        # Members whose dues are current have nothing to pay — send them back to their
        # membership card rather than showing an empty/confusing payment page.
        if request.user.is_authenticated:
            member = ClubMember.objects.filter(club=self.club, user=request.user, is_deleted=False).first()
            if member:
                _process_pending_membership_renewal_for_member(self.club, member)
                member.refresh_from_db()
                _, _, should_show_payment, _ = _membership_renewal_state(self.club, member)
                if not should_show_payment:
                    return redirect(
                        reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": member.uuid})
                    )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        member = ClubMember.objects.filter(club=self.club, user=self.request.user, is_deleted=False).first()
        invoice = Invoice.objects.filter(
            club=self.club,
            buyer=self.request.user,
            renewal_processed=False,
            status="UNPAID",
        ).first()
        if invoice is None:
            invoice = Invoice.objects.create(
                club=self.club,
                buyer=self.request.user,
                status="UNPAID",
                renewal_needed=True,
            )
        context["club"] = self.club
        context["member"] = member
        context["invoice"] = invoice
        return context


class ClubMemberMergeView(LoginRequiredMixin, ClubViewMixin, View):
    """Merge two club members: keep target, soft-delete (deactivate) source, copy non-empty fields."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_add_edit"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def _get_member(self, pk):
        return get_object_or_404(ClubMember, pk=pk, club=self.club, is_deleted=False)

    @staticmethod
    def _format_merge_value(value):
        if value in (None, ""):
            return "—"
        return value

    def _build_review_initial(self, source, target):
        return {
            field_name: getattr(source, field_name, None)
            for field_name in ClubMemberMergeReviewForm.Meta.fields
            if getattr(target, field_name, None) in (None, "") and getattr(source, field_name, None) not in (None, "")
        }

    def _safe_next_url(self, request):
        url = request.GET.get("next") or request.POST.get("next", "")
        if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
            return url
        return ""

    @staticmethod
    def _format_membership_date(value):
        if not value:
            return "—"
        return value.strftime("%b %-d, %Y")

    def _membership_info_rows(self, source, target):
        rows = []
        for attr, label in [
            ("membership_expiration_date", "Expires"),
            ("membership_last_paid", "Last paid"),
        ]:
            src_val = getattr(source, attr, None)
            tgt_val = getattr(target, attr, None)
            if src_val or tgt_val:
                rows.append(
                    {
                        "label": label,
                        "source_value": self._format_membership_date(src_val),
                        "target_value": self._format_membership_date(tgt_val),
                    }
                )
        return rows

    def _build_review_context(self, request, source, target, review_form, next_url=""):
        cancel_url = next_url or reverse("club_admin", kwargs={"slug": self.club.slug})
        return {
            "step": "review",
            "page_title": f"Merge member — {source}",
            "heading": "Merge member",
            "subheading": f"Club: {self.club.name}",
            "source": source,
            "target": target,
            "source_label": str(source),
            "target_label": str(target),
            "review_form": review_form,
            "next_url": next_url,
            "comparison_rows": (
                self._membership_info_rows(source, target)
                + [
                    {
                        "label": field.label,
                        "source_value": self._format_merge_value(getattr(source, name, None)),
                        "target_value": self._format_merge_value(getattr(target, name, None)),
                    }
                    for name, field in review_form.fields.items()
                ]
            ),
            "summary_lines": [
                f"{source} will be deactivated.",
                f"{target} will be kept.",
                "Permission flags from the removed member will be merged into the surviving member.",
                "Any missing Discord ID, points, or paid-through date on the kept member will be copied over.",
            ],
            "target_field_name": "target",
            "cancel_url": cancel_url,
            "action_url": reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk}),
            "save_button_label": f"Merge and deactivate {source}",
        }

    def get(self, request, slug, pk):
        source = self._get_member(pk)
        selection_form = ClubMemberMergeTargetForm(self.club, source)
        next_url = self._safe_next_url(request)
        cancel_url = next_url or reverse("club_admin", kwargs={"slug": self.club.slug})
        context = {
            "step": "select",
            "page_title": f"Merge member — {source}",
            "heading": "Merge member",
            "subheading": f"Club: {self.club.name}",
            "selection_form": selection_form,
            "source_label": str(source),
            "next_url": next_url,
            "cancel_url": cancel_url,
            "action_url": reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk}),
        }
        return render(request, "auctions/contact_merge.html", context)

    def post(self, request, slug, pk):
        source = self._get_member(pk)
        next_url = self._safe_next_url(request)
        if request.POST.get("step") == "review":
            target = get_object_or_404(ClubMember, pk=request.POST.get("target"), club=self.club)
            review_form = ClubMemberMergeReviewForm(request.POST, instance=target)
            if review_form.is_valid():
                with transaction.atomic():
                    target = review_form.save()
                    update_fields = set(review_form.changed_data)
                    for field in [
                        "discord_id",
                        "bap_points",
                        "hap_points",
                        "membership_last_paid",
                        "membership_expiration_date",
                    ]:
                        source_val = getattr(source, field, None)
                        target_val = getattr(target, field, None)
                        if source_val is not None and not target_val:
                            setattr(target, field, source_val)
                            update_fields.add(field)
                    for perm_field in [
                        "permission_admin",
                        "permission_view",
                        "permission_export",
                        "permission_add_edit",
                        "permission_edit_club",
                        "permission_manage_auctions",
                        "permission_manage_bap",
                    ]:
                        if getattr(source, perm_field, False) and not getattr(target, perm_field, False):
                            setattr(target, perm_field, True)
                            update_fields.add(perm_field)
                    if target.is_deleted:
                        target.is_deleted = False
                        update_fields.add("is_deleted")
                    if update_fields:
                        target.save(update_fields=list(update_fields))
                    source_name = str(source)
                    # Re-point all related records from source to target before deactivating.
                    AuctionTOS.objects.filter(clubmember=source).update(clubmember=target)
                    BapAward.objects.filter(club_member=source).update(club_member=target)
                    InvoicePayment.objects.filter(club_member=source).update(club_member=target)
                    source.is_deleted = True
                    source.save(update_fields=["is_deleted"])
                    ClubHistory.objects.create(
                        club=self.club,
                        user=request.user,
                        action=f"Merged member {source_name} into {target}",
                        applies_to="MEMBERS",
                    )
                messages.success(request, f"Merged {source} into {target}.")
                return redirect(next_url or reverse("club_admin", kwargs={"slug": self.club.slug}))
            return render(
                request,
                "auctions/contact_merge.html",
                self._build_review_context(request, source, target, review_form, next_url),
            )
        selection_form = ClubMemberMergeTargetForm(self.club, source, request.POST or None)
        if request.method == "POST" and selection_form.is_valid():
            target = selection_form.cleaned_data["target"]
            review_form = ClubMemberMergeReviewForm(
                instance=target,
                initial=self._build_review_initial(source, target),
            )
            return render(
                request,
                "auctions/contact_merge.html",
                self._build_review_context(request, source, target, review_form, next_url),
            )
        cancel_url = next_url or reverse("club_admin", kwargs={"slug": self.club.slug})
        context = {
            "step": "select",
            "page_title": f"Merge member — {source}",
            "heading": "Merge member",
            "subheading": f"Club: {self.club.name}",
            "selection_form": selection_form,
            "source_label": str(source),
            "next_url": next_url,
            "cancel_url": cancel_url,
            "action_url": reverse("club_member_merge", kwargs={"slug": self.club.slug, "pk": source.pk}),
        }
        return render(request, "auctions/contact_merge.html", context)


class ClubSetupView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Hub page linking to all of a club's settings pages."""

    active_tab = "setup"
    template_name = "auctions/club_setup.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_edit_club") or self.user_has_club_permission("permission_money")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        return context


class ClubEditView(LoginRequiredMixin, ClubViewMixin, UpdateView):
    """Edit club info"""

    active_tab = "edit"
    template_name = "auctions/club_edit.html"
    form_class = ClubEditForm

    def get_object(self):
        return self.club

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_edit_club") or self.user_has_club_permission("permission_money")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        messages.success(self.request, "Club settings saved.")
        # Honour ?next= if present in POST or GET — validate to prevent open redirects
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("club_detail", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["next_url"] = self.request.GET.get("next", "")
        return context

    def form_valid(self, form):
        result = super().form_valid(form)
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action="Updated club settings",
            applies_to="SETTINGS",
        )
        return result


class ClubMembershipSettingsView(LoginRequiredMixin, ClubViewMixin, UpdateView):
    """Edit membership and payment settings for a club."""

    active_tab = "membership"
    template_name = "auctions/club_membership_settings.html"
    form_class = ClubMembershipSettingsForm

    def get_object(self):
        return self.club

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_edit_club") or self.user_has_club_permission("permission_money")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        messages.success(self.request, "Membership settings saved.")
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("club_detail", kwargs={"slug": self.object.slug})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        club = self.club
        user = self.request.user
        context["club"] = club
        context["paypal_seller"] = club.effective_paypal_seller
        context["square_seller"] = club.effective_square_seller
        context["uses_site_paypal"] = club.uses_site_paypal
        context["user_paypal_seller"] = PayPalSeller.objects.filter(user=user).first()
        context["user_square_seller"] = SquareSeller.objects.filter(user=user).first()
        context["paypal_configured"] = bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET)
        context["square_configured"] = bool(
            getattr(settings, "SQUARE_APPLICATION_ID", None) and getattr(settings, "SQUARE_CLIENT_SECRET", None)
        )
        # Non-OAuth PayPal (admin-only opt-in): the club enters its own REST credentials here
        # instead of connecting via OAuth.
        if club.allow_non_oauth_paypal:
            context["paypal_credentials_form"] = ClubPayPalCredentialsForm(instance=club)
        return context

    def form_valid(self, form):
        result = super().form_valid(form)
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action="Updated membership settings",
            applies_to="SETTINGS",
        )
        return result


class ClubLinkPaymentAccountView(LoginRequiredMixin, ClubViewMixin, View):
    """POST-only endpoint used from the club membership settings page to link or
    unlink the requesting user's PayPal/Square seller to this club.

    Query / form parameters:
      provider: ``paypal`` or ``square``
      action:   ``attach`` (default) — set seller.club to this club
                ``detach``            — clear the club's linked seller
    """

    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_edit_club") or self.user_has_club_permission("permission_money")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        provider = request.POST.get("provider") or request.GET.get("provider")
        action = request.POST.get("action") or "attach"
        if provider not in ("paypal", "square"):
            messages.error(request, "Unknown payment provider.")
            return redirect(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))
        model = PayPalSeller if provider == "paypal" else SquareSeller
        provider_label = "PayPal" if provider == "paypal" else "Square"

        if action == "detach":
            seller = model.objects.filter(club=self.club).first()
            if seller:
                seller.club = None
                seller.save(update_fields=["club"])
                ClubHistory.objects.create(
                    club=self.club,
                    user=request.user,
                    action=f"Disconnected {provider_label} account {seller.payer_email or seller.user}",
                    applies_to="SETTINGS",
                )
                messages.success(request, f"{provider_label} account disconnected.")
            return redirect(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))

        # attach: use the requesting user's existing seller if they have one, otherwise
        # send them through OAuth with the club context set.
        seller = model.objects.filter(user=request.user).first()
        if not seller:
            connect_url = reverse("paypal_connect" if provider == "paypal" else "square_connect")
            return redirect(f"{connect_url}?club={self.club.slug}")

        existing = model.objects.filter(club=self.club).exclude(pk=seller.pk).first()
        if existing:
            existing.club = None
            existing.save(update_fields=["club"])
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Replaced {provider_label} account {existing.payer_email or existing.user}",
                applies_to="SETTINGS",
            )
        seller.club = self.club
        seller.save(update_fields=["club"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Connected {provider_label} account {seller.payer_email or seller.user}",
            applies_to="SETTINGS",
        )
        messages.success(request, f"{provider_label} account linked to {self.club.name}.")
        return redirect(reverse("club_membership_settings", kwargs={"slug": self.club.slug}))


class ClubPayPalCredentialsView(LoginRequiredMixin, ClubViewMixin, View):
    """POST-only endpoint to save a club's own (non-OAuth) PayPal REST credentials.

    Shown on the membership settings page only when the club has ``allow_non_oauth_paypal``
    set (an admin-only flag). Editable by the same people who manage the club's money/settings.
    """

    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_edit_club") or self.user_has_club_permission("permission_money")
        ):
            raise PermissionDenied()
        # Credentials can only be entered when an admin has opted this club into non-OAuth PayPal.
        if not self.club.allow_non_oauth_paypal:
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        settings_url = reverse("club_membership_settings", kwargs={"slug": self.club.slug})
        form = ClubPayPalCredentialsForm(request.POST, instance=self.club)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect(settings_url)
        had_credentials = self.club.uses_own_paypal_credentials
        form.save()
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action="Updated PayPal credentials" if had_credentials else "Added PayPal credentials",
            applies_to="SETTINGS",
        )
        messages.success(request, "PayPal credentials saved.")
        return redirect(settings_url)


class ClubEmailSettingsView(LoginRequiredMixin, ClubViewMixin, UpdateView):
    active_tab = "email_settings"
    template_name = "auctions/club_email_settings.html"
    form_class = ClubEmailSettingsForm

    def get_object(self):
        return self.club

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        messages.success(self.request, "Email settings saved.")
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("club_detail", kwargs={"slug": self.object.slug})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["show_email_routing"] = settings.SES_ROUTE_EMAILS_ENABLED
        return kwargs

    def get_context_data(self, **kwargs):
        from django.utils import timezone

        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["email_domain"] = settings.EMAIL_ROUTING_DOMAIN
        show_email_routing = settings.SES_ROUTE_EMAILS_ENABLED
        context["show_email_routing"] = show_email_routing
        if not show_email_routing:
            context["email_fallback_member"] = self.club._first_email_member_by_priority(Q(permission_add_edit=True))
        # Preview context
        user = self.request.user
        preview_member = self.club.members.filter(user=user, is_deleted=False).first()
        if preview_member:
            preview_name = (preview_member.name or "").strip() or "Member"
            preview_member_link = preview_member.member_page_url
            preview_barcode_url = preview_member.barcode_image_link if preview_member.club.show_member_barcode else ""
        else:
            full_name = user.get_full_name() if user.is_authenticated else ""
            preview_name = full_name.strip() or "Member"
            preview_member_link = ""
            preview_barcode_url = ""
        context["preview_name"] = preview_name
        context["preview_member_link"] = preview_member_link
        context["preview_barcode_url"] = preview_barcode_url
        context["membership_numbers_enabled"] = self.club.show_member_barcode

        today = timezone.localdate()
        next_auction = (
            Auction.objects.filter(
                club=self.club,
                promote_this_auction=True,
                is_deleted=False,
                date_start__date__gte=today,
            )
            .order_by("date_start")
            .first()
        )
        context["next_auction"] = next_auction
        directions_link = ""
        if next_auction:
            physical = list(next_auction.physical_location_qs)
            if len(physical) == 1 and physical[0].directions_link:
                directions_link = physical[0].directions_link
        context["next_auction_directions_link"] = directions_link

        # Build the next-auction HTML fragment exactly once on the server so the
        # JS preview just toggles visibility (no client-side templating).
        next_auction_html = ""
        if next_auction:
            from django.utils.html import escape as _escape

            date_str = next_auction.date_start.strftime("%B %-d, %Y") if next_auction.date_start else ""
            parts = [f"Our next auction will be {_escape(next_auction.title)}"]
            if date_str:
                parts.append(f"on {_escape(date_str)}")
            next_auction_html = " ".join(parts).rstrip() + "."
            if directions_link:
                next_auction_html += " <span class='text-info'>Get directions</span>."
            next_auction_html += " <span class='text-info'>Read the auction's rules</span>."

        club_icon_url = ""
        if self.club.icon:
            try:
                club_icon_url = self.club.icon_display_url or ""
            except (ValueError, AttributeError):
                club_icon_url = ""

        context["preview_payload"] = {
            "club_name": self.club.name,
            "club_icon_url": club_icon_url,
            "preview_name": preview_name,
            "preview_member_link": preview_member_link,
            "preview_barcode_url": preview_barcode_url,
            "membership_numbers_enabled": context["membership_numbers_enabled"],
            "payments_enabled": self.club.membership_payment_emails_enabled,
            "next_auction_html": next_auction_html,
        }
        return context

    def form_valid(self, form):
        result = super().form_valid(form)
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action="Updated email settings",
            applies_to="SETTINGS",
        )
        return result


class ClubBapSettingsView(LoginRequiredMixin, ClubViewMixin, UpdateView):
    """Edit BAP (Breeder Award Program) settings for a club. Requires permission_manage_bap."""

    active_tab = "bap_settings"
    template_name = "auctions/club_bap_settings.html"
    form_class = ClubBapSettingsForm

    def get_object(self):
        return self.club

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        messages.success(self.request, "BAP settings saved.")
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()}):
            return next_url
        return reverse("club_detail", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["next_url"] = self.request.GET.get("next", "")
        context["bap_category_overrides"] = ClubBapCategoryOverride.objects.filter(club=self.club).select_related(
            "category"
        )
        context["override_form"] = ClubBapCategoryOverrideForm()
        return context

    def form_valid(self, form):
        result = super().form_valid(form)
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action="Updated BAP settings",
            applies_to="BAP",
        )
        return result


class ClubBapCategoryOverrideSaveView(LoginRequiredMixin, ClubViewMixin, View):
    """Create or update a per-category BAP point override for a club."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        form = ClubBapCategoryOverrideForm(request.POST)
        if form.is_valid():
            category = form.cleaned_data["category"]
            points = form.cleaned_data["points"]
            ClubBapCategoryOverride.objects.update_or_create(
                club=self.club, category=category, defaults={"points": points}
            )
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Set BAP point override for {category.name}: {points} pts",
                applies_to="BAP",
            )
        return redirect(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))


class ClubBapCategoryOverrideDeleteView(LoginRequiredMixin, ClubViewMixin, View):
    """Delete a per-category BAP point override."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk):
        override = ClubBapCategoryOverride.objects.filter(pk=pk, club=self.club).first()
        if override:
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Removed BAP point override for {override.category.name}",
                applies_to="BAP",
            )
            override.delete()
        return redirect(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))


class ClubBapView(LoginRequiredMixin, ClubViewMixin, HTMxTableView):
    """Main BAP admin page — awarded points tab."""

    active_tab = "bap_awards"
    model = BapAward
    table_class = BapAwardHTMxTable
    filterset_class = BapAwardFilter
    template_name = "auctions/club_bap.html"
    htmx_table_header_template = "auctions/partials/club_bap_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        if not self.club.enable_breeder_award_program:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            BapAward.objects.filter(club_member__club=self.club, club_member__is_deleted=False)
            .select_related("club_member", "lot")
            .order_by("-date")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["can_manage_bap"] = self.user_has_club_permission("permission_manage_bap")
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["club"] = self.club
        return kwargs


class ClubBapLotsView(LoginRequiredMixin, ClubViewMixin, HTMxTableView):
    """Pending BAP page — lots from this club's auctions awaiting point assignment."""

    active_tab = "bap_lots"
    model = Lot
    table_class = ClubBapLotHTMxTable
    filterset_class = ClubBapLotFilter
    template_name = "auctions/club_bap_lots.html"
    htmx_table_header_template = "auctions/partials/club_bap_lots_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        if not self.club.enable_breeder_award_program:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        matching_member = ClubMember.objects.filter(
            club=self.club,
            is_deleted=False,
        ).filter(
            Q(user=OuterRef("auctiontos_seller__user"))
            | Q(user=OuterRef("user"))
            | Q(email__iexact=OuterRef("auctiontos_seller__email"))
        )
        qs = Lot.objects.filter(
            auction__club=self.club,
            is_deleted=False,
            active=False,
        )
        if self.club.only_sold_lots:
            qs = qs.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
        return (
            qs.filter(Exists(matching_member))
            .select_related("auctiontos_seller__user", "auction__club", "species_category")
            .prefetch_related("bap_award")
            .order_by("-date_end")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["bap_embed_path"] = reverse("bap_embed", kwargs={"slug": self.club.slug})
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["club"] = self.club
        return kwargs


class ClubBapLotCategoryView(APIView):
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_lot(self, request, pk):
        lot = get_object_or_404(Lot, pk=pk, is_deleted=False, banned=False)
        club = lot.auction.club if lot.auction else None
        if not club or not check_club_permission(request.user, club, "permission_manage_bap"):
            raise PermissionDenied()
        return lot, club

    def get(self, request, pk):
        lot, _club = self._get_lot(request, pk)
        form = LotCategoryForm(instance=lot, post_url=reverse("club_bap_lot_category", kwargs={"pk": lot.pk}))
        return render(
            request,
            "auctions/generic_admin_form.html",
            {"form": form, "modal_title": f"Set category — {lot.lot_name}"},
        )

    def post(self, request, pk):
        lot, club = self._get_lot(request, pk)
        form = LotCategoryForm(
            request.POST, instance=lot, post_url=reverse("club_bap_lot_category", kwargs={"pk": lot.pk})
        )
        if form.is_valid():
            updated_lot = form.save(commit=False)
            update_fields = ["species_category"]
            if not BapAward.objects.filter(lot=updated_lot).exists() and not updated_lot.manually_approved:
                updated_lot.bap_points_awarded = 0
                updated_lot.bap_auto_reason = updated_lot.sold_lot_no_bap_reason or ""
                update_fields.extend(["bap_points_awarded", "bap_auto_reason"])
            updated_lot.save(update_fields=update_fields)
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Updated lot category for {updated_lot.lot_name}",
                applies_to="BAP",
            )
            return HttpResponse("<script>closeModal(); htmx.trigger(document.body, 'bapLotListChanged');</script>")
        return render(
            request,
            "auctions/generic_admin_form.html",
            {"form": form, "modal_title": f"Set category — {lot.lot_name}"},
        )


class BapAwardAdminView(APIView):
    """HTMX modal for creating or editing a BapAward."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_club_and_award(self, request, slug=None, pk=None):
        if pk:
            award = get_object_or_404(BapAward, pk=pk)
            club = award.club_member.club
        else:
            award = None
            club = get_object_or_404(Club, slug=slug)
        if not check_club_permission(request.user, club, "permission_manage_bap"):
            raise PermissionDenied()
        return club, award

    @staticmethod
    def _lot_initial(lot, club):
        initial = {}
        seller_user = lot.user or (lot.auctiontos_seller.user if lot.auctiontos_seller else None)
        seller_email = (lot.auctiontos_seller.email if lot.auctiontos_seller else None) or ""
        member = None
        if seller_user:
            member = ClubMember.objects.filter(club=club, user=seller_user, is_deleted=False).first()
        if not member and seller_email:
            member = ClubMember.objects.filter(club=club, email__iexact=seller_email, is_deleted=False).first()
        if member:
            initial["club_member"] = member
        if lot.date_end:
            initial["date"] = lot.date_end.date()
        override = (
            ClubBapCategoryOverride.objects.filter(club=club, category=lot.species_category).first()
            if lot.species_category
            else None
        )
        points = (
            override.points
            if override is not None
            else (club.points_per_lot or (lot.species_category.bap_points if lot.species_category else 5))
        )
        placeholder = lot.bap_placeholder
        if placeholder == "HAP":
            initial["hap_points"] = points
        elif placeholder == "Culture":
            initial["cap_points"] = points
        else:
            initial["points"] = points
        return initial

    def _build_form(self, request_data=None, *, club, award, lot, post_url, delete_url=None):
        kwargs = {
            "post_url": post_url,
            "delete_url": delete_url,
            "club": club,
            "show_hap": club.separate_hap,
            "show_cap": club.separate_cap,
            "lot": lot if not award else None,
        }
        if request_data is not None:
            return BapAwardForm(request_data, instance=award, **kwargs)
        if award:
            return BapAwardForm(instance=award, **kwargs)
        return BapAwardForm(initial=self._lot_initial(lot, club) if lot else {}, **kwargs)

    def _build_context(self, club, award, form):
        title = f"Edit award for {award.club_member}" if award else f"Add points — {club.name}"
        return {"modal_title": title, "form": form}

    def get(self, request, slug=None, pk=None):
        club, award = self._get_club_and_award(request, slug=slug, pk=pk)
        lot = None
        if not award:
            lot_pk = request.GET.get("lot_pk")
            if lot_pk:
                lot = Lot.objects.filter(pk=lot_pk, is_deleted=False, banned=False).first()
        post_url = (
            reverse("bapaward_admin", kwargs={"pk": award.pk})
            if award
            else reverse("bapaward_create", kwargs={"slug": club.slug}) + (f"?lot_pk={lot.pk}" if lot else "")
        )
        delete_url = reverse("bapaward_delete", kwargs={"pk": award.pk}) if award else None
        form = self._build_form(club=club, award=award, lot=lot, post_url=post_url, delete_url=delete_url)
        return render(request, "auctions/generic_admin_form.html", self._build_context(club, award, form))

    def post(self, request, slug=None, pk=None):
        club, award = self._get_club_and_award(request, slug=slug, pk=pk)
        lot = None
        if not award:
            lot_pk = request.GET.get("lot_pk")
            if lot_pk:
                lot = Lot.objects.filter(pk=lot_pk, is_deleted=False, banned=False).first()
        post_url = (
            reverse("bapaward_admin", kwargs={"pk": award.pk})
            if award
            else reverse("bapaward_create", kwargs={"slug": club.slug}) + (f"?lot_pk={lot.pk}" if lot else "")
        )
        delete_url = reverse("bapaward_delete", kwargs={"pk": award.pk}) if award else None
        form = self._build_form(request.POST, club=club, award=award, lot=lot, post_url=post_url, delete_url=delete_url)
        if form.is_valid():
            award_obj = form.save(commit=False)
            award_obj.awarded_by = request.user
            if lot and not award:
                award_obj.lot = lot
            award_obj.save()
            if lot:
                placeholder = lot.bap_placeholder
                lot.bap_points_awarded = (
                    award_obj.hap_points
                    if placeholder == "HAP"
                    else (award_obj.cap_points if placeholder == "Culture" else award_obj.points)
                )
                lot.manually_approved = True
                lot.bap_auto_reason = ""
                lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"{'Updated' if award else 'Added'} BAP award: {award_obj}",
                applies_to="BAP",
            )
            return close_modal_response(
                "trigger-event",
                event_name="bapAwardListChanged",
                extra_triggers={"bapLotListChanged": True},
            )
        return render(request, "auctions/generic_admin_form.html", self._build_context(club, award, form))


class BapAwardDeleteView(APIView):
    """HTMX endpoint to delete a BapAward and trigger table refresh."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        award = get_object_or_404(BapAward, pk=pk)
        club = award.club_member.club
        if not check_club_permission(request.user, club, "permission_manage_bap"):
            raise PermissionDenied()
        member_name = str(award.club_member)
        lot = award.lot
        award.delete()
        if lot:
            lot.bap_points_awarded = 0
            lot.manually_approved = False
            lot.bap_auto_reason = lot.sold_lot_no_bap_reason or ""
            lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
        ClubHistory.objects.create(
            club=club,
            user=request.user,
            action=f"Deleted BAP award for {member_name}",
            applies_to="BAP",
        )
        return close_modal_response(
            "trigger-event",
            event_name="bapAwardListChanged",
            extra_triggers={"bapLotListChanged": True},
        )


class BapAwardCSVImportView(LoginRequiredMixin, CSVContactImportMixin, ClubViewMixin, View):
    """Create-only CSV import for BapAward records (never updates or deletes).

    Routes through the shared preview: each row is matched to an existing club member by email and, on
    confirm, a BapAward is created. There is no duplicate-resolution choice (awards are always new), so the
    review page just shows the awards to create and the skipped rows with reasons."""

    import_record_kind = "award"
    import_supports_duplicates = False
    import_preview_columns = (
        ("Member", "member_name"),
        ("Email", "email"),
        ("BAP", "bap"),
        ("HAP", "hap"),
        ("CAP", "cap"),
    )

    def import_target_id(self):
        return f"club:{self.club.pk}"

    def import_done_url(self):
        return reverse("club_bap", kwargs={"slug": self.club.slug})

    def import_cancel_url(self):
        return reverse("club_bap", kwargs={"slug": self.club.slug})

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        if not self.club.enable_breeder_award_program:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        preview_token = request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        return redirect(self.import_cancel_url())

    def post(self, request, *args, **kwargs):
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        csv_file = request.FILES.get("csv_file")
        if not csv_file:
            messages.error(request, "No file uploaded.")
            return redirect(self.import_cancel_url())
        result = self.handle_csv_upload(csv_file)
        if result is None:
            return redirect(self.import_cancel_url())
        return result

    def plan_row(self, row):
        row_lower = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
        email = normalize_email(row_lower.get("email", ""))
        bap = self._parse_int(row_lower.get("bap", ""))
        hap = self._parse_int(row_lower.get("hap", ""))
        cap = self._parse_int(row_lower.get("cap", ""))
        award_date = self.parse_flexible_date(row_lower.get("date", ""))
        fields = {
            "email": email,
            "member_name": "",
            "member_pk": None,
            "bap": bap,
            "hap": hap,
            "cap": cap,
            "notes": (row_lower.get("notes", "") or "")[:500],
            "date": award_date.isoformat() if award_date else None,
        }
        base = {"fields": fields, "target_pk": None, "target_display": "", "match_type": None}
        if not email:
            return {**base, "action": "skip", "reason": "Row has no email"}
        member = self.club.find_member(email=email)
        if not member:
            return {**base, "action": "skip", "reason": "No club member matches this email"}
        fields["member_name"] = member.name
        fields["member_pk"] = member.pk
        if bap == 0 and hap == 0 and cap == 0:
            return {**base, "action": "skip", "reason": f"No BAP/HAP/CAP points for {member.name}"}
        return {**base, "action": "create", "reason": ""}

    def apply_action(self, action, decision):
        if action["action"] == "skip":
            return "skipped"
        fields = action["fields"]
        member = ClubMember.objects.filter(pk=fields.get("member_pk"), club=self.club, is_deleted=False).first()
        if not member:
            return "skipped"
        award_date = date_type.fromisoformat(fields["date"]) if fields.get("date") else timezone.now().date()
        BapAward.objects.create(
            club_member=member,
            date=award_date,
            points=fields.get("bap", 0),
            hap_points=fields.get("hap", 0),
            cap_points=fields.get("cap", 0),
            notes=fields.get("notes", ""),
            awarded_by=self.request.user,
        )
        return "created"

    def message_import_results(self, results):
        parts = []
        if results.get("created"):
            parts.append(f"{results['created']} award(s) added")
        if results.get("skipped"):
            parts.append(f"{results['skipped']} rows skipped")
        messages.success(self.request, ", ".join(parts) or "No awards imported.")

    def record_import_history(self, results, filename=None):
        if not results.get("created"):
            return
        action = f"BAP CSV import: {results['created']} award(s) added"
        if filename:
            action += f" from {filename}"
        ClubHistory.objects.create(club=self.club, user=self.request.user, action=action, applies_to="BAP")

    def process_csv_data(self, csv_reader, filename=None):
        """Parse the upload into planned actions and show the review page; nothing is written yet."""
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)

    @staticmethod
    def _parse_int(value):
        try:
            return max(0, int(value)) if value else 0
        except (ValueError, TypeError):
            return 0


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
        )
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Created API key [{prefix}] '{name}'",
            applies_to="SETTINGS",
        )
        request.session[f"new_api_key_{api_key.pk}"] = raw_key
        return redirect(reverse("club_api_key_detail", kwargs={"slug": self.club.slug, "pk": api_key.pk}))


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
        ctx["site_domain"] = Site.objects.get_current().domain
        ctx["example_member_id"] = (
            self.club.members.filter(is_deleted=False).order_by("-pk").values_list("pk", flat=True).first()
        )
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
        return render(
            request,
            "auctions/self_serve_contact.html",
            {"club": self.club, "member": member, "level": level, "label": label},
        )


class ClubHistoryView(LoginRequiredMixin, ClubViewMixin, HTMxTableView):
    """History log for a club"""

    active_tab = "history"
    model = ClubHistory
    table_class = ClubHistoryHTMxTable
    filterset_class = ClubHistoryFilter
    template_name = "auctions/club_history.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_view"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return ClubHistory.objects.filter(club=self.club).order_by("-timestamp")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        return context

    def get_table_kwargs(self, **kwargs):
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["club"] = self.club
        return kwargs


class ClubStatsView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    """Club-level charts for auctions and membership growth."""

    active_tab = "stats"
    template_name = "auctions/club_stats.html"
    membership_window_days = 365 * 10

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_view"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def _format_chart_date(self, value):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%b %-d, %Y")

    def _format_auction_start_date(self, auction):
        dt = auction.date_start
        if timezone.is_aware(dt):
            dt = timezone.localtime(dt)
        return dt.date().strftime("%b %-d, %Y")

    def _paid_member_filter(self):
        today = timezone.now().date()
        if self.club.membership_system == "january_first":
            paid_cutoff = date_type(today.year, 1, 1)
        else:
            paid_cutoff = today - timedelta(days=365)
        return Q(membership_expiration_date__gte=today) | (
            Q(membership_expiration_date__isnull=True) & Q(membership_last_paid__gte=paid_cutoff)
        )

    def _get_cached_club_stats(self, auction):
        cached_stats = auction.cached_stats or {}
        misc_stats = cached_stats.get("misc") or {}
        return misc_stats.get("club_stats") or {}

    def get_auction_stats_chart_data(self):
        auctions = Auction.objects.filter(club=self.club, is_deleted=False).order_by("date_start", "pk")
        labels = []
        gross_values = []
        lot_values = []
        participant_values = []
        for auction in auctions:
            auction_misc = self._get_cached_club_stats(auction)
            gross = auction_misc.get("gross")
            total_lots = auction_misc.get("total_lots")
            participants = auction_misc.get("checked_in") or auction_misc.get("participants")
            labels.append(self._format_auction_start_date(auction))
            gross_values.append(round(float(gross), 2) if gross is not None else 0)
            lot_values.append(total_lots if total_lots is not None else 0)
            participant_values.append(participants if participants is not None else 0)
        return {
            "labels": labels,
            "datasets": [
                {
                    "label": "Gross",
                    "data": gross_values,
                    "borderColor": "#4bc0c0",
                    "backgroundColor": "rgba(75, 192, 192, 0.2)",
                    "fill": False,
                    "yAxisID": "y-right",
                },
                {
                    "label": "Lots",
                    "data": lot_values,
                    "borderColor": "#36a2eb",
                    "backgroundColor": "rgba(54, 162, 235, 0.2)",
                    "fill": False,
                    "yAxisID": "y-left",
                },
                {
                    "label": "Checked in",
                    "data": participant_values,
                    "borderColor": "#ff9f40",
                    "backgroundColor": "rgba(255, 159, 64, 0.2)",
                    "fill": False,
                    "yAxisID": "y-left",
                },
            ],
        }

    def get_membership_growth_chart_data(self):
        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=self.membership_window_days)
        total_days = (end_date - start_date).days
        start_dt = timezone.make_aware(
            datetime.combine(start_date, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        end_dt = timezone.make_aware(
            datetime.combine(end_date + timedelta(days=1), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        paid_filter = self._paid_member_filter()
        members_qs = ClubMember.objects.filter(club=self.club, is_deleted=False)
        window_qs = members_qs.filter(createdon__gte=start_dt, createdon__lt=end_dt)
        initial_total = members_qs.filter(createdon__lt=start_dt).count()
        initial_paid = members_qs.filter(createdon__lt=start_dt).filter(paid_filter).count()

        def daily_count(qs):
            return (
                qs.annotate(join_date=TruncDay("createdon"))
                .values("join_date")
                .annotate(count=Count("pk"))
                .order_by("join_date")
            )

        def make_cumulative(daily_qs, initial):
            date_counts = {item["join_date"].date(): item["count"] for item in daily_qs}
            cumulative = []
            running = initial
            for i in range(total_days + 1):
                running += date_counts.get(start_date + timedelta(days=i), 0)
                cumulative.append(running)
            return cumulative

        return {
            "labels": [(start_date + timedelta(days=i)).strftime("%b %-d, %Y") for i in range(total_days + 1)],
            "datasets": [
                {
                    "label": "Members",
                    "data": make_cumulative(daily_count(window_qs), initial_total),
                    "borderColor": "#9966ff",
                    "backgroundColor": "rgba(153, 102, 255, 0.2)",
                    "fill": False,
                },
                {
                    "label": "Paid members",
                    "data": make_cumulative(daily_count(window_qs.filter(paid_filter)), initial_paid),
                    "borderColor": "#4caf50",
                    "backgroundColor": "rgba(76, 175, 80, 0.2)",
                    "fill": False,
                },
            ],
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["club_auction_stats"] = self.get_auction_stats_chart_data()
        context["club_membership_growth"] = self.get_membership_growth_chart_data()
        return context


class ClubTreasurerReportView(LoginRequiredMixin, ClubViewMixin, TemplateView):
    active_tab = "treasurer_report"
    template_name = "auctions/club_treasurer_report.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.has_treasurer_permission():
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def has_treasurer_permission(self):
        return self.user_has_club_permission("permission_money") or self.user_has_club_permission(
            "permission_edit_club"
        )

    def _default_date_values(self):
        today = timezone.localdate()
        return {"start_date": today.replace(day=1), "end_date": today}

    def _get_filter_form(self):
        initial = self._default_date_values()
        form = ClubTreasurerReportForm(self.request.GET or None, initial=initial)
        if form.is_valid():
            data = form.cleaned_data
        else:
            data = initial
        return form, data["start_date"] or initial["start_date"], data["end_date"] or initial["end_date"]

    def _manual_category_choices(self):
        # Auto categories are reconciled from invoices and the balance adjustment is created
        # by the "balance books" tool, so neither may be entered by hand here.
        excluded = set(ClubMoney.AUTO_CATEGORIES) | {ClubMoney.CATEGORY_ADJUSTMENT}
        return [choice for choice in ClubMoney.CATEGORY_CHOICES if choice[0] not in excluded]

    def _filtered_entries(self, start_date, end_date):
        return ClubMoney.objects.filter(club=self.club, date__range=(start_date, end_date)).order_by("-date", "-pk")

    def _money_sum(self, queryset, **filters):
        total = queryset.filter(**filters).aggregate(total=Sum("amount"))["total"]
        return total or Decimal("0.00")

    def _outstanding_invoices(self, start_date, end_date):
        """Auction invoices from the period that still owe the club money.

        An invoice is only outstanding when, after applying every recorded payment, the
        member still owes the club (a negative balance). The previous implementation
        looked at ``calculated_total`` alone — the invoice total *before* payments — so an
        invoice that had been paid (in full or in part) but not yet flipped to ``PAID``
        was reported as outstanding even though nothing was owed. Comparing the stored
        total against recorded payments fixes that over-count.

        Returns a dict with the number of such invoices and the total still owed.
        """
        from django.db.models import DecimalField
        from django.db.models.functions import Coalesce

        rows = (
            Invoice.objects.filter(
                auction__club=self.club,
                auction__date_start__date__range=(start_date, end_date),
                status__in=("DRAFT", "UNPAID"),
                calculated_total__isnull=False,
            )
            .annotate(
                paid=Coalesce(
                    Sum("payments__amount"),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
            .values("pk", "calculated_total", "paid")
        )
        count = 0
        amount_owed = Decimal("0.00")
        for row in rows:
            balance = Decimal(row["calculated_total"]) + (row["paid"] or Decimal("0.00"))
            if balance < 0:
                count += 1
                amount_owed += -balance
        return {"count": count, "amount": amount_owed}

    def _report_summary(self, entries, start_date, end_date):
        # money_in / money_out are the raw cash flow for the period; everything else is a
        # breakdown of where it came from. Each figure below is a sum over ledger entries,
        # so they always reconcile to the running balance.
        money_in = sum((entry.amount for entry in entries if entry.amount > 0), Decimal("0.00"))
        money_out = abs(sum((entry.amount for entry in entries if entry.amount < 0), Decimal("0.00")))
        auction_sales = self._money_sum(entries, category=ClubMoney.CATEGORY_AUCTION_SALE)
        seller_payouts = abs(self._money_sum(entries, category=ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT))
        outstanding = self._outstanding_invoices(start_date, end_date)
        return {
            "money_in": money_in,
            "money_out": money_out,
            "net": money_in - money_out,
            "membership_renewals": ClubMember.objects.filter(
                club=self.club,
                is_deleted=False,
                membership_last_paid__range=(start_date, end_date),
            ).count(),
            # Auction commission is sales minus payouts — the club's cut — never stored as
            # its own ledger row, so the gross numbers keep matching the bank.
            "auction_sales": auction_sales,
            "seller_payouts": seller_payouts,
            "auction_commission": auction_sales - seller_payouts,
            "tax_collected": self._money_sum(entries, category=ClubMoney.CATEGORY_TAX),
            "membership_dues": self._money_sum(entries, category=ClubMoney.CATEGORY_MEMBERSHIP),
            "donations": self._money_sum(entries, category=ClubMoney.CATEGORY_DONATION),
            "outstanding_invoices": outstanding["count"],
            "outstanding_invoices_amount": outstanding["amount"],
        }

    def _club_currency_symbol(self):
        seller = self.club.effective_paypal_seller or self.club.effective_square_seller
        currency = seller.user.userdata.currency if (seller and seller.user) else "USD"
        return get_currency_symbol(currency)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filter_form, start_date, end_date = self._get_filter_form()
        entries_qs = self._filtered_entries(start_date, end_date)
        entries = list(entries_qs)
        current_balance = ClubMoney.objects.filter(club=self.club).aggregate(total=Sum("amount"))["total"] or Decimal(
            "0.00"
        )
        currency_symbol = self._club_currency_symbol()
        context.update(
            {
                "club": self.club,
                "filter_form": filter_form,
                "start_date": start_date,
                "end_date": end_date,
                "report_entries": entries,
                "report_summary": self._report_summary(entries_qs, start_date, end_date),
                "club_money_form": ClubMoneyForm(
                    initial={"date": timezone.localdate()},
                    category_choices=self._manual_category_choices(),
                ),
                "club_money_balance_form": ClubMoneyBalanceForm(initial={"account_balance": current_balance}),
                "current_balance": current_balance,
                "currency_symbol": currency_symbol,
                "treasurer_export_url": reverse("club_treasurer_report_export", kwargs={"slug": self.club.slug}),
                "can_manage_money": self.has_treasurer_permission(),
            }
        )
        return context


class ClubTreasurerReportExportView(LoginRequiredMixin, ClubViewMixin, View):
    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_money") or self.user_has_club_permission("permission_edit_club")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ClubTreasurerReportForm(request.GET or None)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid date range.")
        start_date = form.cleaned_data["start_date"] or timezone.localdate().replace(day=1)
        end_date = form.cleaned_data["end_date"] or timezone.localdate()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.club.slug}-treasurer-report.csv"'
        writer = csv.writer(response)
        writer.writerow(["date", "amount", "description", "category"])
        for entry in ClubMoney.objects.filter(club=self.club, date__range=(start_date, end_date)).order_by(
            "date", "pk"
        ):
            writer.writerow([entry.date.isoformat(), entry.amount, entry.description, entry.category])
        return response


class ClubMoneyCreateView(LoginRequiredMixin, ClubViewMixin, View):
    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_money") or self.user_has_club_permission("permission_edit_club")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Reject the invoice-reconciled categories and the balance adjustment: a hand-entered
        # auto category would be undone the next time the owning invoice is reconciled.
        blocked = set(ClubMoney.AUTO_CATEGORIES) | {ClubMoney.CATEGORY_ADJUSTMENT}
        category_choices = [choice for choice in ClubMoney.CATEGORY_CHOICES if choice[0] not in blocked]
        form = ClubMoneyForm(request.POST, category_choices=category_choices)
        if not form.is_valid():
            return JsonResponse({"ok": False, "errors": form.errors}, status=400)
        entry = form.save(commit=False)
        entry.club = self.club
        entry.created_by = request.user
        entry.save()
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Added {entry.get_category_display()} record: {entry.description} ({entry.amount})",
            applies_to="SETTINGS",
        )
        seller = self.club.effective_paypal_seller or self.club.effective_square_seller
        currency = seller.user.userdata.currency if (seller and seller.user) else "USD"
        return JsonResponse(
            {
                "ok": True,
                "message": f"Saved {entry.get_category_display()} record.",
                "current_balance": str(
                    ClubMoney.objects.filter(club=self.club).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
                ),
                "entry": {
                    "date": str(entry.date),
                    "amount": str(entry.amount),
                    "description": entry.description,
                    "category_display": entry.get_category_display(),
                    "currency_symbol": get_currency_symbol(currency),
                },
            }
        )


class ClubMoneyBalanceView(LoginRequiredMixin, ClubViewMixin, View):
    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not (
            self.user_has_club_permission("permission_money") or self.user_has_club_permission("permission_edit_club")
        ):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = ClubMoneyBalanceForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"ok": False, "errors": form.errors}, status=400)
        current_balance = ClubMoney.objects.filter(club=self.club).aggregate(total=Sum("amount"))["total"] or Decimal(
            "0.00"
        )
        account_balance = form.cleaned_data["account_balance"]
        adjustment = account_balance - current_balance
        if adjustment:
            ClubMoney.objects.create(
                club=self.club,
                created_by=request.user,
                date=timezone.localdate(),
                amount=adjustment,
                description=f"Balance books adjustment to match account balance {account_balance}",
                category=ClubMoney.CATEGORY_ADJUSTMENT,
            )
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Balance adjustment of {adjustment} to match account balance {account_balance}",
                applies_to="SETTINGS",
            )
        return JsonResponse(
            {
                "ok": True,
                "message": (
                    "Balance books adjustment saved."
                    if adjustment
                    else "Books already matched the supplied account balance. No adjustment was created."
                ),
                "current_balance": str(
                    ClubMoney.objects.filter(club=self.club).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
                ),
            }
        )


class ClubMemberCSVImportView(LoginRequiredMixin, CSVContactImportMixin, ClubViewMixin, View):
    """Import club members from a CSV file"""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not check_club_permission(request.user, self.club, "permission_export"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    # CSV import preview framework (see CSVContactImportMixin)
    import_record_kind = "member"
    import_supports_duplicates = True
    import_dedupe_field = "email"  # two rows with the same email are the same member; combine them
    import_preview_columns = (
        ("Name", "name"),
        ("Email", "email"),
        ("Phone", "phone"),
    )

    def import_target_id(self):
        return f"club:{self.club.pk}"

    def import_done_url(self):
        return reverse("club_admin", kwargs={"slug": self.club.slug})

    def import_cancel_url(self):
        return reverse("club_admin", kwargs={"slug": self.club.slug})

    def get(self, request, *args, **kwargs):
        preview_token = request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        return redirect(self.import_cancel_url())

    def post(self, request, *args, **kwargs):
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        csv_file = request.FILES.get("csv_file")
        if not csv_file:
            messages.error(request, "No file uploaded.")
            return redirect(self.import_cancel_url())
        result = self.handle_csv_upload(csv_file)
        if result is None:
            return redirect(self.import_cancel_url())
        return result

    @staticmethod
    def _member_label(member):
        label = member.name or "(no name)"
        if member.email:
            return f"{label} ({member.email})"
        return label

    @staticmethod
    def _to_date(value):
        return date_type.fromisoformat(value) if value else None

    def _parse_member_row(self, row):
        """Extract + normalize one CSV row into the member fields dict (dates as ISO strings for caching)."""
        first_name = self.extract_csv_field(row, self.FIRST_NAME_FIELD_NAMES)
        last_name = self.extract_csv_field(row, self.LAST_NAME_FIELD_NAMES)
        if first_name or last_name:
            member_name = f"{first_name} {last_name}".strip()
        else:
            member_name = self.extract_csv_field(row, self.NAME_FIELD_NAMES) or ""
        raw_contact_status = self.extract_csv_field(row, self.CONTACT_STATUS_FIELD_NAMES)
        contact_status = self.parse_contact_status(raw_contact_status)
        mark_deleted = (raw_contact_status or "").strip().lower() in {
            "inactive past member",
            "disabled",
            "deleted",
            "deactivated",
        }
        if mark_deleted:
            contact_status = "do_not_contact"

        def iso(value):
            parsed = self.parse_flexible_date(value)
            return parsed.isoformat() if parsed else None

        return {
            "email": normalize_email(self.extract_csv_field(row, self.EMAIL_FIELD_NAMES))[:254],
            "name": (member_name or "")[:200],
            "phone": (self.extract_csv_field(row, self.PHONE_FIELD_NAMES) or "")[:20],
            "address": (self.extract_csv_field(row, self.ADDRESS_FIELD_NAMES) or "")[:500],
            "memo": (self.extract_csv_field(row, self.MEMO_FIELD_NAMES) or "")[:500],
            "discord_id": (self.extract_csv_field(row, self.DISCORD_ID_FIELD_NAMES) or "")[:100],
            "contact_status": contact_status,
            "mark_deleted": mark_deleted,
            "membership_last_paid": iso(self.extract_csv_field(row, self.MEMBERSHIP_LAST_PAID_FIELD_NAMES)),
            "membership_expiration_date": iso(self.extract_csv_field(row, self.MEMBERSHIP_EXPIRATION_FIELD_NAMES)),
            "date_joined": iso(self.extract_csv_field(row, self.DATE_JOINED_FIELD_NAMES)),
        }

    def plan_row(self, row):
        fields = self._parse_member_row(row)
        base = {"fields": fields, "target_pk": None, "target_display": "", "match_type": None}
        email, name = fields["email"], fields["name"]
        if not email and not name:
            return {**base, "action": "skip", "reason": "Row has no name or email"}
        if email:
            existing = self.club.find_member(email=email)
            if existing:
                return {
                    **base,
                    "action": "update",
                    "target_pk": existing.pk,
                    "target_display": self._member_label(existing),
                    "match_type": "email",
                    "reason": "Matched an existing member by email",
                }
        if name:
            existing = self.club.find_member(name=name)
            if existing:
                return {
                    **base,
                    "action": "duplicate",
                    "target_pk": existing.pk,
                    "target_display": self._member_label(existing),
                    "match_type": "name",
                    "reason": "Same or similar name as an existing member",
                }
        return {**base, "action": "create", "reason": ""}

    def _create_member(self, fields):
        member = ClubMember.objects.create(
            club=self.club,
            email=fields.get("email", ""),
            name=fields.get("name", ""),
            phone_number=fields.get("phone", ""),
            address=fields.get("address", ""),
            memo=fields.get("memo", ""),
            discord_id=fields.get("discord_id") or None,
            contact_status=fields.get("contact_status") or "contact",
            membership_last_paid=self._to_date(fields.get("membership_last_paid")),
            membership_expiration_date=self._to_date(fields.get("membership_expiration_date")),
            send_welcome_email=False,
            welcome_email_sent=True,
            source="csv",
            added_by=self.request.user,
            is_deleted=fields.get("mark_deleted", False),
        )
        date_joined = self._to_date(fields.get("date_joined"))
        if date_joined is not None:
            ClubMember.objects.filter(pk=member.pk).update(createdon=date_joined)
        return member

    def _update_member(self, member, fields):
        """Apply CSV fields onto an existing member. Only overwrites with non-empty values so a merge of a
        sparse walk-in row never blanks existing contact details."""
        if fields.get("name"):
            member.name = fields["name"]
        if fields.get("phone"):
            member.phone_number = fields["phone"]
        if fields.get("address"):
            member.address = fields["address"]
        if fields.get("memo"):
            member.memo = fields["memo"]
        if fields.get("discord_id"):
            member.discord_id = fields["discord_id"]
        if fields.get("contact_status") is not None:
            member.contact_status = fields["contact_status"]
        if fields.get("mark_deleted"):
            member.is_deleted = True
        if fields.get("email") and not member.email:
            member.email = fields["email"]
        last_paid = self._to_date(fields.get("membership_last_paid"))
        if last_paid is not None:
            member.membership_last_paid = last_paid
        expiration = self._to_date(fields.get("membership_expiration_date"))
        if expiration is not None:
            member.membership_expiration_date = expiration
        member.save()
        date_joined = self._to_date(fields.get("date_joined"))
        if date_joined is not None:
            ClubMember.objects.filter(pk=member.pk).update(createdon=date_joined)

    def apply_action(self, action, decision):
        kind = action["action"]
        if kind == "skip":
            return "skipped"
        fields = action.get("fields", {})
        target_pk = action.get("target_pk")
        if kind == "create" or (kind == "duplicate" and decision == "create"):
            member = self._create_member(fields)
            if kind == "duplicate" and target_pk:
                ClubMember.objects.filter(pk=member.pk).update(possible_duplicate=target_pk)
                ClubMember.objects.filter(pk=target_pk, possible_duplicate__isnull=True).update(
                    possible_duplicate=member.pk
                )
            return "created"
        member = (
            ClubMember.objects.filter(pk=target_pk, club=self.club, is_deleted=False).first() if target_pk else None
        )
        if not member:
            self._create_member(fields)
            return "created"
        self._update_member(member, fields)
        return "updated" if kind == "update" else "merged"

    def record_import_history(self, results, filename=None):
        parts = []
        if results.get("created"):
            parts.append(f"{results['created']} members added")
        updated = results.get("updated", 0) + results.get("merged", 0)
        if updated:
            parts.append(f"{updated} members updated")
        if not parts:
            return
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action=f"CSV import: {', '.join(parts)}" + (f" from {filename}" if filename else ""),
            applies_to="MEMBERS",
        )

    def process_csv_data(self, csv_reader, filename=None):
        """Parse the upload into planned actions and show the review page; nothing is written yet."""
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)


class ClubMemberCSVExportView(LoginRequiredMixin, ClubViewMixin, View):
    """Export club members as CSV — applies the same filter query as the admin list view."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not check_club_permission(request.user, self.club, "permission_export"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        from .filters import ClubMemberFilter

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.club.slug}-members.csv"'
        writer = csv.writer(response)
        # Omit the Membership Number column entirely when the club has the
        # feature disabled — user asked for "no UI" referencing those numbers.
        include_membership_number = self.club.show_member_barcode
        header = [
            "Name",
            "Email",
            "Phone",
            "Address",
            "BAP Points",
            "HAP Points",
            "Membership Last Paid",
            "Date Joined",
            "Source",
            "Contact Status",
            "Discord ID",
            "Memo",
            "Membership Expires",
            "Renewal Link",
            "Barcode Link",
            "Distance",
            "Total Sold",
            "Total Bought",
            "Mailchimp Status",
            "Tags",
        ]
        if include_membership_number:
            header.append("Membership Number")
        writer.writerow(header)
        base_qs = ClubMember.objects.filter(club=self.club, is_deleted=False)
        filterset = ClubMemberFilter(request.GET, queryset=base_qs)
        qs = filterset.qs
        for member in qs:
            if member.less_than_10_miles:
                distance = "nearby"
            elif member.less_than_30_miles:
                distance = "medium"
            elif member.more_than_30_miles:
                distance = "long"
            else:
                distance = ""
            active_tags = ", ".join(name for name, active in member.compute_mailchimp_tags().items() if active)
            row = [
                member.name,
                member.email or "",
                member.phone_as_string,
                member.address,
                member.bap_points,
                member.hap_points,
                member.membership_last_paid or "",
                member.createdon.date(),
                member.source,
                member.contact_status,
                member.discord_id or "",
                member.memo,
                member.membership_expiration_date or "",
                member.wallet_link,
                member.barcode_image_link_png,
                distance,
                member.cached_total_sold if member.cached_total_sold is not None else "",
                member.cached_total_bought if member.cached_total_bought is not None else "",
                member.get_mailchimp_status_display(),
                active_tags,
            ]
            if include_membership_number:
                row.append(member.membership_number)
            writer.writerow(row)
        query_filter = request.GET.get("query", "all")
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Exported member CSV (filter: {query_filter})",
            applies_to="MEMBERS",
        )
        return response


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
            from .filters import ClubMemberFilter

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


# ---------------------------------------------------------------------------
# Inbound email routing API
# ---------------------------------------------------------------------------


class InboundEmailRoutingView(APIView):
    """Resolve an inbound email address to its forwarding recipient.

    Called by the SES inbound Lambda to determine where to forward a message.
    Requires a shared secret supplied via the ``X-Routing-Secret`` header (must
    match the ``INBOUND_ROUTING_SECRET`` Django setting).

    GET /api/v1/email-routing/resolve/?address=<local_part_or_full_email>

    Returns:
        200 {"recipient": "user@example.com", "display_name": "Spring Auction 2024"}
        400 {"error": "address parameter is required"}
        401 {"error": "invalid or missing routing secret"}
        503 {"error": "email routing is not enabled"}
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from .email_routing import email_routing_enabled, resolve_routing_info

        secret = (getattr(settings, "INBOUND_ROUTING_SECRET", "") or "").strip()
        provided = (request.META.get("HTTP_X_ROUTING_SECRET", "") or "").strip()
        if not secret or not provided or not secrets.compare_digest(provided, secret):
            return Response({"error": "invalid or missing routing secret"}, status=401)

        if not email_routing_enabled():
            return Response({"error": "email routing is not enabled"}, status=503)

        address = (request.query_params.get("address") or "").strip().lower()
        if not address:
            return Response({"error": "address parameter is required"}, status=400)

        # Accept either a bare local-part or a full email; extract local-part only.
        local_part = address.split("@")[0]
        info = resolve_routing_info(local_part)
        if info is None:
            return Response({"error": "no recipient found for this address"}, status=404)
        return Response({"recipient": info["recipient"], "display_name": info["display_name"]})


# ---------------------------------------------------------------------------
# Discord integration helpers and views
# ---------------------------------------------------------------------------

# Discord interaction type constants
_DISCORD_TYPE_PING = 1
_DISCORD_TYPE_APPLICATION_COMMAND = 2
_DISCORD_TYPE_COMPONENT = 3
_DISCORD_TYPE_CHANNEL_MESSAGE = 4
_DISCORD_TYPE_MODAL_SUBMIT = 5
_DISCORD_TYPE_MODAL = 9

# Discord component type constants
_DISCORD_COMPONENT_ACTION_ROW = 1
_DISCORD_COMPONENT_TEXT_INPUT = 4
_DISCORD_COMPONENT_BUTTON = 2

# Discord button styles
_DISCORD_BUTTON_STYLE_PRIMARY = 1

# Discord message flag: ephemeral (only visible to the user who triggered it)
_DISCORD_FLAG_EPHEMERAL = 64


def verify_discord_signature(public_key_hex, signature_hex, timestamp, body):
    """Verify a Discord interaction request signature using Ed25519.

    Returns True if the signature is valid, False otherwise.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        message = timestamp.encode() + (body if isinstance(body, bytes) else body.encode())
        key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def assign_discord_role(guild_id, user_id, role_id):
    """Assign a Discord role to a guild member via the Discord REST API.

    PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}
    Returns True on success (204 No Content), False otherwise.
    """
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token:
        logger.warning("DISCORD_BOT_TOKEN not configured – cannot assign Discord role")
        return False
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {bot_token}"}
    try:
        resp = requests.put(url, headers=headers, timeout=10)
        if resp.status_code == 204:
            return True
        logger.warning(
            "Discord role assignment failed: guild=%s user=%s role=%s status=%s response=%s",
            guild_id,
            user_id,
            role_id,
            resp.status_code,
            resp.text,
        )
        return False
    except requests.RequestException as exc:
        logger.exception("Error assigning Discord role: %s", exc)
        return False


def _discord_ephemeral(content):
    return JsonResponse(
        {"type": _DISCORD_TYPE_CHANNEL_MESSAGE, "data": {"content": content, "flags": _DISCORD_FLAG_EPHEMERAL}}
    )


_DISCORD_PERMISSION_MANAGE_GUILD = 1 << 5


def _has_discord_manage_guild(data):
    member_data = data.get("member") or {}
    try:
        return bool(int(member_data.get("permissions", "0")) & _DISCORD_PERMISSION_MANAGE_GUILD)
    except (ValueError, TypeError):
        return False


def _sync_discord_roles(club, bot_token):
    """Fetch roles from Discord and upsert ClubDiscordRole objects.

    Also fetches the bot's own member record to determine its highest role position.
    Roles at or above that position have bot_can_manage=False.

    Returns the number of roles synced, or None if the API call failed.
    """
    headers = {"Authorization": f"Bot {bot_token}"}
    guild_id = club.discord_server_id

    # Fetch all guild roles
    url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        logger.exception("Error fetching Discord roles: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("Discord roles fetch failed: status=%s response=%s", resp.status_code, resp.text)
        return None

    all_roles = resp.json()
    # Build a position lookup by role ID
    position_by_id = {r["id"]: r.get("position", 0) for r in all_roles}

    # Fetch the bot's own guild member to find its highest role position.
    # /members/@me only works with OAuth2 bearer tokens, not bot tokens.
    # Instead: resolve the bot's user ID first, then fetch its guild member by ID.
    bot_max_position = 0
    try:
        user_resp = requests.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=10)
        if user_resp.status_code == 200:
            bot_user_id = user_resp.json().get("id")
            if bot_user_id:
                member_resp = requests.get(
                    f"https://discord.com/api/v10/guilds/{guild_id}/members/{bot_user_id}",
                    headers=headers,
                    timeout=10,
                )
                if member_resp.status_code == 200:
                    bot_role_ids = member_resp.json().get("roles", [])
                    if bot_role_ids:
                        bot_max_position = max(position_by_id.get(rid, 0) for rid in bot_role_ids)
                    logger.info(
                        "Discord bot position resolved: user_id=%s bot_max_position=%s", bot_user_id, bot_max_position
                    )
                else:
                    logger.warning("Discord guild member fetch failed: status=%s", member_resp.status_code)
        else:
            logger.warning("Discord users/@me fetch failed: status=%s", user_resp.status_code)
    except requests.RequestException as exc:
        logger.exception("Error fetching bot's own member record: %s", exc)

    updated = 0
    fetched_role_ids = set()
    for role in all_roles:
        role_id = role.get("id", "")
        role_name = role.get("name", "")
        if role_id == guild_id or role.get("managed"):
            continue
        role_position = role.get("position", 0)
        can_manage = bot_max_position > role_position
        fetched_role_ids.add(role_id)
        obj = ClubDiscordRole.objects.filter(club=club, role_id=role_id).first()
        if obj:
            update_fields = []
            if obj.role_name != role_name:
                obj.role_name = role_name
                update_fields.append("role_name")
            if obj.bot_can_manage != can_manage:
                obj.bot_can_manage = can_manage
                update_fields.append("bot_can_manage")
            if update_fields:
                obj.save(update_fields=update_fields)
        else:
            ClubDiscordRole.objects.create(club=club, role_id=role_id, role_name=role_name, bot_can_manage=can_manage)
        updated += 1
    # Remove roles that no longer exist in Discord (only those with a non-empty role_id;
    # preserve placeholder rows without a Discord ID)
    ClubDiscordRole.objects.filter(club=club).exclude(role_id__in=fetched_role_ids).exclude(role_id="").delete()
    return updated


class DiscordInteractionsView(View):
    """Handle Discord interaction requests at /discord/interactions/.

    Supports:
      - Type 1 (PING)
      - Type 3 (component / button click) with custom_id=join_button
        (behaves like the /membership command: join modal if not joined,
        membership info + link if joined)
      - Type 5 (modal submit) with custom_id=join_modal
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        public_key = getattr(settings, "DISCORD_PUBLIC_KEY", "")
        if not public_key:
            logger.warning("DISCORD_PUBLIC_KEY not configured")
            return HttpResponseForbidden("Discord integration not configured")

        # Signature verification
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not signature or not timestamp:
            return HttpResponseBadRequest("Missing signature headers")
        try:
            if abs(time() - int(timestamp)) > 300:
                return HttpResponseForbidden("Stale request")
        except (ValueError, TypeError):
            return HttpResponseBadRequest("Invalid timestamp")
        body = request.body

        if not verify_discord_signature(public_key, signature, timestamp, body):
            return HttpResponseForbidden("Invalid request signature")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Invalid JSON")

        interaction_type = data.get("type")

        # Type 1 – PING (required for Discord endpoint verification)
        if interaction_type == _DISCORD_TYPE_PING:
            return JsonResponse({"type": _DISCORD_TYPE_PING})

        # Type 3 – Component interaction (button click)
        if interaction_type == _DISCORD_TYPE_COMPONENT:
            custom_id = data.get("data", {}).get("custom_id", "")
            if custom_id == "join_button":
                # The join button mirrors the /membership command: if the user
                # hasn't joined, show the join modal; if they have, show their
                # membership info and link.
                return self._handle_membership_command(data)
            return _discord_ephemeral("Unsupported interaction")

        # Type 2 – Application command (slash command)
        if interaction_type == _DISCORD_TYPE_APPLICATION_COMMAND:
            command_name = data.get("data", {}).get("name", "")
            if command_name == "connect":
                return self._handle_connect_command(data)
            if command_name == "auctions_here":
                return self._handle_auctions_here_command(data)
            if command_name == "membership":
                return self._handle_membership_command(data)
            if command_name == "bap":
                return self._handle_bap_command(data)
            if command_name == "join":
                return self._handle_join_command(data)
            return _discord_ephemeral("❌ Unknown command.")

        # Type 5 – Modal submit
        if interaction_type == _DISCORD_TYPE_MODAL_SUBMIT:
            custom_id = data.get("data", {}).get("custom_id", "")
            if custom_id == "join_modal":
                return self._handle_join_modal(data)
            return _discord_ephemeral("Unsupported interaction")

        return _discord_ephemeral("Unsupported interaction")

    def _join_modal_response(self):
        """Return a Discord modal response for joining the club."""
        return JsonResponse(
            {
                "type": _DISCORD_TYPE_MODAL,
                "data": {
                    "custom_id": "join_modal",
                    "title": "Enter your contact information",
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_TEXT_INPUT,
                                    "custom_id": "name",
                                    "label": "Full name",
                                    "style": 1,
                                    "placeholder": "John Smith",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_TEXT_INPUT,
                                    "custom_id": "email",
                                    "label": "Email address",
                                    "style": 1,
                                    "placeholder": "john@example.com",
                                    "required": True,
                                }
                            ],
                        },
                    ],
                },
            }
        )

    def _handle_join_command(self, data):
        guild_id = data.get("guild_id", "")
        if not guild_id or not Club.objects.filter(discord_server_id=guild_id).exists():
            return _discord_ephemeral("❌ No club is configured for this Discord server.")
        if self._already_joined(data):
            return _discord_ephemeral("✅ You've already joined!")
        return self._join_modal_response()

    def _already_joined(self, data):
        """Return True if the Discord user is already a member of the club for this server."""
        guild_id = data.get("guild_id", "")
        if not guild_id:
            return False
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")
        if not discord_id:
            return False
        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return False
        return ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).exists()

    def _handle_join_modal(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")
        discord_username = user_data.get("username", "") or user_data.get("global_name", "") or ""

        # Extract text inputs from modal components
        fields = {}
        for row in data.get("data", {}).get("components", []):
            for comp in row.get("components", []):
                fields[comp.get("custom_id", "")] = comp.get("value", "")

        # Accept either a single ``name`` field or ``first_name`` / ``last_name``.
        name = fields.get("name", "").strip()
        if not name:
            first_name = fields.get("first_name", "").strip()
            last_name = fields.get("last_name", "").strip()
            name = f"{first_name} {last_name}".strip()
        email = fields.get("email", "").strip()

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process your request. Please try again.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ No club is configured for this Discord server.")

        # Already registered with this Discord ID?
        existing = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if existing:
            return _discord_ephemeral("✅ You're already registered!")

        # Email match – link Discord ID and assign role
        if email:
            # note that we do not verify email anywhere
            # this means that anyone can claim any email address by entering it in the modal
            # under no circumstances should the club member expose any information,
            # not even name, to anyone who hasn't been specifically granted a role in the club
            # and anything on discord needs to reflect this, too
            # the user model has an email that can be assumed valid
            if len(email) < 5 or "@" not in email:
                return _discord_ephemeral("❌ Please enter a valid email address.")
            existing_by_email = ClubMember.objects.filter(club=club, email=email, is_deleted=False).first()
            if existing_by_email:
                if existing_by_email.discord_id and existing_by_email.discord_id != discord_id:
                    return _discord_ephemeral("❌ This email is already linked to another Discord account.")
                update_fields = ["discord_id"]
                existing_by_email.discord_id = discord_id
                if discord_username:
                    existing_by_email.discord_username = discord_username
                    update_fields.append("discord_username")
                existing_by_email.save(update_fields=update_fields)
                existing_by_email.maybe_assign_discord_role()
                ClubHistory.objects.create(
                    club=club,
                    user=None,
                    action=f"Discord account linked for {existing_by_email} (@{discord_username or discord_id})",
                    applies_to="MEMBERS",
                )
                return _discord_ephemeral("✅ You're in! Access unlocked.")

        # Create a new club member
        new_member = ClubMember(
            club=club,
            name=name,
            email=email or None,
            discord_id=discord_id,
            discord_username=discord_username or None,
            source="discord",
        )
        new_member.save()
        new_member.maybe_assign_discord_role()
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"New member added via Discord: {new_member} (@{discord_username or discord_id})",
            applies_to="MEMBERS",
        )
        return _discord_ephemeral("✅ You're in! Access unlocked.")

    def _handle_connect_command(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        caller_discord_id = user_data.get("id", "")
        options = {o["name"]: o["value"] for o in data.get("data", {}).get("options", [])}
        club_uuid = options.get("club_uuid", "").strip()

        if not _has_discord_manage_guild(data):
            return _discord_ephemeral("❌ You need the Manage Server permission to run this command.")

        if not guild_id or not club_uuid:
            return _discord_ephemeral("❌ Missing guild ID or club UUID.")

        try:
            club = Club.objects.get(uuid=club_uuid)
        except (Club.DoesNotExist, ValueError, ValidationError):
            return _discord_ephemeral("❌ This isn't a valid club connection code.")

        # Reject if another club already owns this guild
        if Club.objects.filter(discord_server_id=guild_id).exclude(pk=club.pk).exists():
            return _discord_ephemeral("❌ This Discord server is already connected to another club.")

        club.discord_server_id = guild_id
        club.save(update_fields=["discord_server_id"])

        caller_username = user_data.get("username") or caller_discord_id
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Discord server connected by @{caller_username} (Discord ID {caller_discord_id})",
            applies_to="SETTINGS",
        )

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        _sync_discord_roles(club, bot_token) if bot_token else None

        return JsonResponse(
            {
                "type": _DISCORD_TYPE_CHANNEL_MESSAGE,
                "data": {
                    "content": (f"Welcome to the **{club.name}**! Click the button below to register."),
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_BUTTON,
                                    "custom_id": "join_button",
                                    "label": "Join our club",
                                    "style": _DISCORD_BUTTON_STYLE_PRIMARY,
                                }
                            ],
                        }
                    ],
                },
            }
        )

    def _handle_auctions_here_command(self, data):
        guild_id = data.get("guild_id", "")
        channel_id = data.get("channel_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        caller_discord_id = user_data.get("id", "")

        if not _has_discord_manage_guild(data):
            return _discord_ephemeral("❌ You need the Manage Server permission to run this command.")

        if not guild_id or not channel_id:
            return _discord_ephemeral("❌ Missing guild or channel ID.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club. Run /connect first.")

        club.auction_channel_id = channel_id
        club.save(update_fields=["auction_channel_id"])
        caller_username = user_data.get("username") or caller_discord_id
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Auction announcement channel set by @{caller_username} (Discord ID {caller_discord_id})",
            applies_to="SETTINGS",
        )
        return _discord_ephemeral("✅ Auction announcements will be posted in this channel.")

    def _handle_membership_command(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process this request.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club.")

        member = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if not member:
            return self._join_modal_response()

        lines = [f"**{club.name}** — Your membership"]
        lines.append(f"Member since: {member.createdon.strftime('%B %d, %Y')}")

        expiry = member.membership_expiration_date
        if not expiry:
            if club.membership_annual_fee:
                lines.append("Status: ❌ Expired — please renew your membership")
        else:
            today = timezone.now().date()
            expiry_ts = int(datetime.combine(expiry, datetime.min.time(), date_tz.utc).timestamp())
            if expiry >= today:
                lines.append(f"Status: ✅ Active — expires <t:{expiry_ts}:D>")
            else:
                lines.append(f"Status: ❌ Expired <t:{expiry_ts}:D> — please renew your membership")

        if club.enable_club_page:
            lines.append(f"\n[View your membership]({member.simple_membership_link})")

        return _discord_ephemeral("\n".join(lines))

    def _handle_bap_command(self, data):  # noqa: C901 (kept intentional)
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process this request.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club.")

        if not club.enable_breeder_award_program:
            return _discord_ephemeral("❌ This club does not use the Breeder Award Program.")

        member = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if not member:
            return self._join_modal_response()

        lines = [f"**{club.name}** — Your points"]

        bap_rank = ClubMember.objects.filter(club=club, bap_points__gt=member.bap_points, is_deleted=False).count() + 1
        lines.append(f"BAP: {member.bap_points} pts (#{bap_rank}) — {member.bap_points_ytd} pts this year")

        if club.separate_hap:
            hap_rank = (
                ClubMember.objects.filter(club=club, hap_points__gt=member.hap_points, is_deleted=False).count() + 1
            )
            lines.append(f"HAP: {member.hap_points} pts (#{hap_rank}) — {member.hap_points_ytd} pts this year")

        if club.separate_cap:
            cap_rank = (
                ClubMember.objects.filter(club=club, culture_points__gt=member.culture_points, is_deleted=False).count()
                + 1
            )
            lines.append(
                f"Culture: {member.culture_points} pts (#{cap_rank}) — {member.culture_points_ytd} pts this year"
            )

        recent_awards = BapAward.objects.filter(club_member=member).order_by("-date", "-pk")[:5]
        if recent_awards:
            lines.append("\n**Recent awards:**")
            for award in recent_awards:
                lines.append(f"• {award.date} — {award}")

        return _discord_ephemeral("\n".join(lines))


class LotBapPointsView(LoginRequiredMixin, View):
    """Inline BAP approve/reject/undo endpoint for the Pending BAP table."""

    def _seller_name(self, lot):
        if lot.auctiontos_seller:
            return lot.auctiontos_seller.name
        if lot.user:
            return f"{lot.user.first_name} {lot.user.last_name}".strip() or lot.user.username or f"user #{lot.user.pk}"
        return f"lot #{lot.pk}"

    def _resolve_member(self, lot, club):
        seller_user = lot.user or (lot.auctiontos_seller.user if lot.auctiontos_seller else None)
        seller_email = (lot.auctiontos_seller.email if lot.auctiontos_seller else None) or ""
        member = None
        if seller_user:
            member = ClubMember.objects.filter(club=club, user=seller_user, is_deleted=False).first()
        if not member and seller_email:
            member = ClubMember.objects.filter(club=club, email__iexact=seller_email, is_deleted=False).first()
        return member

    def _render_buttons(self, request, lot, club):
        lot.refresh_from_db()
        try:
            award = lot.bap_award
        except Exception:
            award = None
        lot.bap_award_cached = award
        override = (
            ClubBapCategoryOverride.objects.filter(club=club, category=lot.species_category).first()
            if lot.species_category
            else None
        )
        default_points = (
            override.points
            if override is not None
            else (club.points_per_lot or (lot.species_category.bap_points if lot.species_category else 5))
        )
        if club.points_for_custom_checkbox > 0 and lot.custom_checkbox:
            default_points += club.points_for_custom_checkbox
        return render(
            request,
            "auctions/bap_lot_buttons.html",
            {"lot": lot, "club": club, "default_points": default_points},
        )

    def post(self, request, pk):
        lot = get_object_or_404(Lot, pk=pk, is_deleted=False, banned=False)
        club = lot.auction.club if lot.auction else None
        if not club or not check_club_permission(request.user, club, "permission_manage_bap"):
            return HttpResponse(status=403)

        action = request.POST.get("action", "approve")

        if action == "undo":
            existing = BapAward.objects.filter(lot=lot).first()
            if existing:
                existing.delete()
            lot.bap_points_awarded = 0
            lot.manually_approved = False
            lot.bap_auto_reason = lot.sold_lot_no_bap_reason or ""
            lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
            return self._render_buttons(request, lot, club)

        if action == "reject":
            existing = BapAward.objects.filter(lot=lot).first()
            if existing:
                existing.delete()
            lot.bap_points_awarded = 0
            lot.manually_approved = True
            # Leave bap_auto_reason as-is: it reflects the system's eligibility verdict,
            # which is still useful to show even when an admin explicitly rejects.
            lot.save(update_fields=["bap_points_awarded", "manually_approved"])
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Rejected BAP points for {self._seller_name(lot)}: {lot.lot_name}",
                applies_to="BAP",
            )
            return self._render_buttons(request, lot, club)

        # action == "approve"
        def _parse_pts(key):
            try:
                return max(0, int(str(request.POST.get(key, 0)).strip() or 0))
            except (ValueError, TypeError):
                return 0

        bap_pts = _parse_pts("bap_points")
        hap_pts = _parse_pts("hap_points")
        cap_pts = _parse_pts("cap_points")

        member = self._resolve_member(lot, club)
        award_date = lot.date_end.date() if lot.date_end else timezone.now().date()

        if (bap_pts or hap_pts or cap_pts) and member:
            BapAward.objects.update_or_create(
                lot=lot,
                defaults={
                    "club_member": member,
                    "date": award_date,
                    "points": bap_pts,
                    "hap_points": hap_pts,
                    "cap_points": cap_pts,
                    "awarded_by": request.user,
                },
            )
            lot.bap_points_awarded = bap_pts + hap_pts + cap_pts
            lot.manually_approved = True
            lot.bap_auto_reason = ""
            lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Awarded {lot.bap_points_awarded} BAP point(s) to {self._seller_name(lot)} for {lot.lot_name}",
                applies_to="BAP",
            )
        return self._render_buttons(request, lot, club)


class ClubDiscordConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Discord settings for a club."""

    active_tab = "discord"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return render(request, "auctions/club_discord_settings.html", self._context(request))

    def post(self, request, *args, **kwargs):
        club = self.club
        club.create_events_for_auctions = "create_events_for_auctions" in request.POST
        club.save(update_fields=["create_events_for_auctions"])
        if request.headers.get("HX-Request"):
            return HttpResponse(status=204)
        messages.success(request, "Discord event settings saved.")
        return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

    def _context(self, request):
        roles = ClubDiscordRole.objects.filter(club=self.club).order_by("role_name")
        client_id = getattr(settings, "DISCORD_BOT_CLIENT_ID", "")
        oauth_url = (
            f"https://discord.com/oauth2/authorize?client_id={client_id}"
            "&scope=bot%20applications.commands&permissions=2415921152"
            if client_id
            else ""
        )
        club_uuid = str(self.club.uuid)
        return {
            "club": self.club,
            "roles": roles,
            "oauth_url": oauth_url,
            "club_uuid": club_uuid,
            "view": self,
        }


class ClubDiscordFetchRolesView(LoginRequiredMixin, ClubViewMixin, View):
    """Fetch roles from the Discord API and save them to the database."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        club = self.club
        if not club.discord_server_id:
            messages.error(request, "Save a Discord server ID first.")
            return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        if not bot_token:
            messages.error(request, "DISCORD_BOT_TOKEN is not configured.")
            return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

        updated = _sync_discord_roles(club, bot_token)
        if updated is None:
            messages.error(request, "Could not fetch roles from Discord. Check your bot token and server ID.")
        else:
            messages.success(request, f"Fetched {updated} role(s) from Discord.")
            from .tasks import sync_discord_member_roles_for_club

            sync_discord_member_roles_for_club.delay(club.pk)
        return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))


class ClubDiscordEditRoleView(LoginRequiredMixin, ClubViewMixin, View):
    """Edit a single ClubDiscordRole's BAP/HAP thresholds and paid/unpaid flags."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        if not role.bot_can_manage:
            messages.error(
                request,
                f'"{role.role_name}" is at or above the bot\'s role in the Discord hierarchy. '
                "Move the bot's role above it in Discord before configuring it here.",
            )
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))
        return render(request, "auctions/club_discord_role_edit.html", {"club": self.club, "role": role})

    def post(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        if not role.bot_can_manage:
            messages.error(request, f'"{role.role_name}" cannot be edited — the bot\'s role is not above it.')
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))
        is_paid = "is_paid_role" in request.POST
        is_unpaid = "is_unpaid_role" in request.POST
        try:
            bap = max(0, int(request.POST.get("bap_points_for_role", 0)))
        except (TypeError, ValueError):
            bap = 0
        try:
            hap = max(0, int(request.POST.get("hap_points_for_role", 0)))
        except (TypeError, ValueError):
            hap = 0

        with transaction.atomic():
            # Enforce exclusivity: each club can have at most one paid and one unpaid role
            if is_paid:
                ClubDiscordRole.objects.filter(club=self.club, is_paid_role=True).exclude(pk=pk).update(
                    is_paid_role=False
                )
            if is_unpaid:
                ClubDiscordRole.objects.filter(club=self.club, is_unpaid_role=True).exclude(pk=pk).update(
                    is_unpaid_role=False
                )
            role.is_paid_role = is_paid
            role.is_unpaid_role = is_unpaid
            role.bap_points_for_role = bap
            role.hap_points_for_role = hap
            role.save(update_fields=["is_paid_role", "is_unpaid_role", "bap_points_for_role", "hap_points_for_role"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Updated Discord role '{role.role_name}' settings",
            applies_to="SETTINGS",
        )
        messages.success(request, f'Role "{role.role_name}" updated.')
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))


class ClubDiscordSetDefaultRoleView(LoginRequiredMixin, ClubViewMixin, View):
    """Set a ClubDiscordRole as the default for new Discord registrations."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        # Clear any existing default for this club
        ClubDiscordRole.objects.filter(club=self.club, is_default=True).update(is_default=False)
        role.is_default = True
        role.save(update_fields=["is_default"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Set '{role.role_name}' as the default Discord role",
            applies_to="SETTINGS",
        )
        messages.success(request, f'"{role.role_name}" set as the default role.')
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))


class ClubDiscordSendJoinMessageView(LoginRequiredMixin, ClubViewMixin, View):
    """Send a welcome message with a join button to a Discord channel."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        channel_id = request.POST.get("channel_id", "").strip()
        if not channel_id:
            messages.error(request, "Please enter a channel ID.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        if not bot_token:
            messages.error(request, "DISCORD_BOT_TOKEN is not configured.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
        payload = {
            "content": f"Welcome to **{self.club.name}**! Click the button below to register and get access to the server.",
            "components": [
                {
                    "type": _DISCORD_COMPONENT_ACTION_ROW,
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_BUTTON,
                            "custom_id": "join_button",
                            "label": "Join our club",
                            "style": _DISCORD_BUTTON_STYLE_PRIMARY,
                        }
                    ],
                }
            ],
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.exception("Error sending Discord join message: %s", exc)
            messages.error(request, "Network error while sending join message.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        if resp.status_code == 200 or resp.status_code == 201:  # Discord returns 200 or 201 depending on version
            messages.success(request, "Join message sent to the channel!")
        else:
            messages.error(request, f"Discord API error {resp.status_code}: could not send message.")
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))


class CommandPaletteView(View):
    """JSON results for the command palette.

    GET ?q= returns search groups; an empty/absent query returns the default items.
    Login is required (applied in urls.py); the response is never cached.
    """

    def get(self, request, *args, **kwargs):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""))
        response = JsonResponse({"groups": groups})
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response


class CommandPaletteLogView(View):
    """Upsert the user's current command-palette search row.

    Accepts form-encoded POST data (works with both fetch and navigator.sendBeacon):
    id, search, result, result_type, result_url, result_object_id. Returns {"id": <pk>}.
    """

    def post(self, request, *args, **kwargs):
        from auctions import command_palette

        def _int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        search_id = command_palette.log_search(
            request.user,
            search_id=_int(request.POST.get("id")),
            search=request.POST.get("search", ""),
            result=request.POST.get("result"),
            result_type=request.POST.get("result_type", ""),
            result_url=request.POST.get("result_url", ""),
            result_object_id=_int(request.POST.get("result_object_id")),
        )
        return JsonResponse({"id": search_id})


class CommandPaletteAnalyticsView(AdminOnlyViewMixin, TemplateView):
    """Admin overview of what people search for in the command palette.

    Surfaces the most common searches and, especially, the top 'bounce' searches
    (queries that returned nothing) so we can add them as synonyms or new shortcuts.
    """

    template_name = "command_palette_analytics.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base = CommandPaletteSearch.objects.exclude(search="")

        def top(qs):
            return list(
                qs.values("search")
                .annotate(count=Count("id"), clicks=Count("id", filter=Q(result="clicked")))
                .order_by("-count")[:20]
            )

        context["top_searches"] = top(base)
        context["top_bounces"] = top(base.filter(result="bounce"))
        context["total_searches"] = base.count()
        context["total_bounces"] = base.filter(result="bounce").count()
        return context
