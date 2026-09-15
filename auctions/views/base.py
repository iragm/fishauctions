"""Shared view machinery: the mixins that decide who may see a page.

:class:`AuctionViewMixin` and :class:`ClubViewMixin` resolve the URL's auction or club and the
viewer's role. Every views module imports from here, never from each other.
:func:`check_club_permission` is the one club permission gate. The membership-renewal helpers live
here because payments, webhooks, invoices and member pages all use them.
"""

import collections
import json
import logging
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.db.models import (
    Q,
    Sum,
)
from django.db.models.base import Model as Model
from django.db.models.functions import TruncMonth
from django.http import (
    Http404,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import escape
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin
from rest_framework.permissions import BasePermission

from auctions.models import (
    Auction,
    AuctionTOS,
    BapAward,
    Club,
    ClubHistory,
    ClubMember,
    Invoice,
    InvoicePayment,
    PickupLocation,
    UserData,
)
from auctions.services import SHARED_MEMBER_FIELDS, clear_bidder_number_in
from auctions.tasks import (
    maybe_send_membership_renewal_confirmation,
)

# Distance conversion constant
MILES_TO_KM = 1.60934

# Delay before an invoice email, allowing undo.
INVOICE_NOTIFICATION_DELAY_SECONDS = 15

# Maximum length for feedback text fields
FEEDBACK_TEXT_MAX_LENGTH = 500

UNASSIGNED_BIDDER_NUMBER_LABEL = "not assigned"

logger = logging.getLogger(__name__)


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
    """Sets self.auction from the URL slug and checks the user's permission."""

    # True for views shared by admins and others, which then branch on is_auction_admin.
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

    @cached_property
    def _auction_permission(self):
        """Whether this user may change this auction, once per request. The result is cached, not
        ``is_auction_admin``, which raises depending on ``allow_non_admins``.
        """
        return self.auction.permission_check(self.request.user)

    @property
    def is_auction_admin(self):
        """Whether request.user created or administers the auction."""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise requests.HTTPError(msg) from None
        result = self._auction_permission
        if not result:
            if self.allow_non_admins:
                # logger.debug("non-admins allowed")
                pass
            else:
                raise PermissionDenied()
        else:
            pass
        return result

    @property
    def can_add_edit_people(self):
        """Club-managed auctions gate people management on the club's permission_add_edit; otherwise
        is_auction_admin. Raises PermissionDenied when neither grants it.
        """
        prev_allow_non_admins = self.allow_non_admins
        self.allow_non_admins = True
        try:
            is_admin = self.is_auction_admin
        finally:
            self.allow_non_admins = prev_allow_non_admins
        if is_admin:
            return True
        if user_can_add_edit_people(self.request.user, self.auction):
            return True
        raise PermissionDenied()

    @property
    def club_sidebar_club(self):
        """The club whose nav sidebar should render on this auction page (or None)."""
        return self.auction.club if self.auction else None

    @property
    def club_sidebar_can_view(self):
        """Whether the user may see the club sidebar on an auction page. Non-raising, for templates."""
        if not self.auction or not self.auction.club_id:
            return False
        return bool(self.auction.permission_check(self.request.user))


def check_club_permission(user, club, permission_name):
    """Whether the user is a superuser or has the named club permission (permission_admin is a wildcard)."""
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


#: Every ClubMember permission flag; "any permission" gates the speaker directory.
CLUB_PERMISSION_FIELDS = (
    "permission_admin",
    "permission_view",
    "permission_export",
    "permission_add_edit",
    "permission_edit_club",
    "permission_money",
    "permission_manage_auctions",
    "permission_manage_bap",
    "permission_manage_donations",
    "permission_send_announcements",
)


def clubs_with_any_permission(user, nec_only=True):
    """Clubs where this user holds at least one permission. One ``filter()`` call on purpose, so a single
    ClubMember row must satisfy all conditions.
    """
    if not user.is_authenticated:
        return Club.objects.none()
    base = Club.objects.filter(is_nec_club=True) if nec_only else Club.objects.all()
    if user.is_superuser:
        return base.order_by("name")
    any_permission = Q()
    for field in CLUB_PERMISSION_FIELDS:
        any_permission |= Q(**{f"members__{field}": True})
    return base.filter(any_permission, members__user=user, members__is_deleted=False).distinct().order_by("name")


def user_can_add_edit_people(user, auction):
    """Whether this user may manage participants in a club-managed auction (non-raising). Shared with the
    palette's ``check_in``; callers also allowing auction admins check ``permission_check`` first.
    """
    if not auction or not auction.is_club_managed:
        return False
    return check_club_permission(user, auction.club, "permission_add_edit")


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
    # One person, one number: a shared number made winner lookups pick either.
    if member.bidder_number:
        clear_bidder_number_in(auction, member.bidder_number, keep_tos=tos)
        tos.bidder_number = member.bidder_number
    for field in SHARED_MEMBER_FIELDS:
        setattr(tos, field, getattr(member, field, None) or "")
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


_SCRIPT_JSON_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


def script_json(value):
    """``json.dumps`` safe inside an inline ``<script>``: escapes the characters Django's
    ``|json_script`` does, so ``</script>`` can't close the tag.
    """
    return json.dumps(value).translate(_SCRIPT_JSON_ESCAPES)


def close_modal_response(
    action=None,
    *,
    event_name=None,
    redirect_url=None,
    table_selector=None,
    extra_triggers=None,
    toast=None,
    toast_type="success",
):
    """Close the active HtmxModal after a successful POST, with an inline ``window.closeModal`` script.

    ``extra_triggers`` fires more HTMX events via ``HX-Trigger``; ``toast`` shows a message as it closes.
    """
    detail = {"action": action} if action else {}
    if event_name is not None:
        detail["eventName"] = event_name
    if redirect_url is not None:
        detail["redirectUrl"] = redirect_url
    if table_selector is not None:
        detail["tableSelector"] = table_selector
    body = ""
    if toast:
        # The toast plugin concatenates HTML, so escape names and emails.
        toast_options = script_json({"title": escape(toast), "type": toast_type, "delay": 8000})
        body += f"<script>window.jQuery && window.jQuery.toast({toast_options});</script>"
    body += f"<script>window.closeModal({script_json(detail)});</script>"
    headers = {}
    if extra_triggers:
        # A header, not markup: plain json.dumps.
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
        # Else the ClubMember linked on the TOS.
        return getattr(invoice.auctiontos_user, "clubmember", None)
    return _find_club_member(invoice.auction.club, user, email)


def _compute_member_renewal_expiration(club, member, today):
    """The new expiration when renewing: one year from a future expiration, else from today. January-1st
    clubs always land on January 1.
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
    # No email or user: don't auto-add the fee, unless the TOS already links a ClubMember.
    if not _invoice_membership_lookup_email(invoice) and not (invoice.auctiontos_user and invoice.auctiontos_user.user):
        if not (invoice.auctiontos_user and invoice.auctiontos_user.clubmember_id):
            return False
    member = _invoice_membership_candidate(invoice)
    if not member:
        return True
    # PayPal subscriptions auto-renew.
    if member.paypal_subscription_id:
        return False
    expiration_date = member.membership_expiration_date
    if not expiration_date:
        return True
    return expiration_date <= timezone.now().date() + timedelta(days=30)


def _sync_tos_alternate_split(tos, invoice=None):
    """None-guarded ``AuctionTOS.update_alternate_split_from_membership``."""
    if tos:
        tos.update_alternate_split_from_membership(invoice)


def _ensure_invoice_renewal_state(invoice):
    if not invoice:
        return
    if not invoice.auction:
        # Club-only renewal invoices set renewal_needed at creation.
        return
    # Skip processed renewals and admin-set checkboxes.
    if not invoice.renewal_processed and not invoice.renewal_manually_set:
        should_need = _should_mark_invoice_renewal_needed(invoice)
        if invoice.renewal_needed != should_need:
            invoice.renewal_needed = should_need
            invoice.save(update_fields=["renewal_needed"])
    _sync_tos_alternate_split(invoice.auctiontos_user, invoice)


def _process_invoice_membership_renewal(invoice, acting_user=None, payment_method="Invoice", external_id=None):
    """Process a membership renewal from an invoice payment. Never raises into the caller that marked it paid."""
    if not invoice or not invoice.renewal_needed:
        return
    try:
        with transaction.atomic():
            # Locked, so concurrent webhooks can't double-process.
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
                member = tos_member
            elif not user and not email and not locked.auctiontos_user:
                # Nothing to identify the buyer; don't create a junk member.
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
                # Link the email-only member to the user.
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
            # Dues aren't booked here: Invoice.sync_club_money books and reverses them on status change.
            locked.renewal_processed = True
            locked.save(update_fields=["renewal_processed"])
            # Keep the in-memory invoice in sync for the caller.
            invoice.renewal_processed = True
            member.update_last_club_activity()
    except Exception:
        logger.exception("Failed to process membership renewal for invoice %s", invoice.pk)
        return
    # Best-effort; must not roll back the renewal.
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
    """With ``?club=<slug>`` and payment permission, store the club in the session so the OAuth callback
    can link the seller. Returns the Club or None.
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
    """Auctions whose participant history may autofill contact details. ``extra_created_by`` adds another
    user's auctions.
    """
    if not user.is_authenticated:
        return Auction.objects.none()

    club_ids = club_ids_available_for_contact_autofill(user)
    filters = Q(created_by=user) | Q(auctiontos__is_admin=True, auctiontos__user=user) | Q(club_id__in=club_ids)
    if extra_created_by:
        filters |= Q(created_by=extra_created_by)
    return Auction.objects.filter(filters).distinct()


def _bap_leaderboard(club, field, current_member):
    """Leaderboard ``(rank, member, is_current_user)`` tuples: top 10 with points, plus the current member
    at their rank if lower.
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
    """Cumulative points chart for the top 10 (green = you, red = first, blue = others), or None."""
    top10 = list(
        ClubMember.objects.filter(club=club, is_deleted=False, **{f"{rank_field}__gt": 0}).order_by(f"-{rank_field}")[
            :10
        ]
    )
    if not top10:
        return None

    start_month = months[0]
    member_ids = [m.pk for m in top10]

    # Skip awards on deleted or banned lots, as the leaderboard does.
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
CLUB_DETAIL_EVENT_LIMIT = 20
CLUB_DETAIL_PAST_EVENT_LIMIT = 5


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
        """Record the club a member last viewed, for palette club shortcuts. Writes only on change."""
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
    def can_manage_donations(self):
        return self.user_has_club_permission("permission_manage_donations")

    @property
    def can_manage_money(self):
        return self.user_has_club_permission("permission_money") or self.user_has_club_permission(
            "permission_edit_club"
        )

    @property
    def can_access_admin(self):
        return self.user_has_club_permission("permission_admin") or self.user_has_club_permission("permission_view")

    @property
    def can_manage_auctions(self):
        """Runs the club's public-facing calendar: events and website snippets."""
        return self.user_has_club_permission("permission_admin") or self.user_has_club_permission(
            "permission_manage_auctions"
        )

    @property
    def can_send_announcements(self):
        """Writes announcements (its own permission)."""
        return self.user_has_club_permission("permission_send_announcements")

    @property
    def can_add_edit(self):
        return self.user_has_club_permission("permission_add_edit")

    @property
    def club_sidebar_club(self):
        """The club whose nav sidebar should render on this page (or None)."""
        return self.club

    @property
    def club_sidebar_can_view(self):
        """Whether the user may see the club sidebar. The union of every sidebar page's permission, since the
        sidebar is the only way to those pages.
        """
        if not self.club:
            return False
        return bool(
            self.can_access_admin
            or self.can_edit_settings
            or self.can_manage_bap
            or self.can_manage_money
            or self.can_manage_donations
            or self.can_manage_auctions
            or self.can_send_announcements
        )


class AdminOnlyViewMixin:
    """Superusers only. Not auction admins, despite the name."""

    permission_denied_message = "Only admins can view this page"
    redirect_url = "/"

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser):
            messages.error(request, self.permission_denied_message)
            return redirect(self.redirect_url)
        return super().dispatch(request, *args, **kwargs)


class AuctionAdminAnywhereViewMixin:
    """Anyone who runs an auction (``UserData.runs_an_auction``), plus superusers. For adding species at a
    check-in table; what they add is private until approved.
    """

    permission_denied_message = "Only auction admins can view this page"
    redirect_url = "/"

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.userdata.runs_an_auction):
            messages.error(request, self.permission_denied_message)
            return redirect(self.redirect_url)
        return super().dispatch(request, *args, **kwargs)


class AuctionStatsPermissionsMixin:
    """For graph classes"""

    @property
    def is_auction_admin(self):
        """Whether request.user created or administers the auction."""
        if not self.auction:
            msg = "you must set self.auction (typically in dispatch) for self.is_auction_admin to be available"
            raise Exception(msg)
        result = self.auction.permission_check(self.request.user)
        if not result:
            if not self.auction.make_stats_public:
                logger.debug("non-admins allowed")

            else:
                raise PermissionDenied()
        else:
            logger.debug("allowing user %s to view %s", self.request.user, self.auction)
        return result


class LocationMixin:
    """Location-aware views: ``get_coordinates()`` returns (latitude, longitude) from cookies or userdata.
    Call it before get_context_data; set ``no_location_message``.
    """

    # Shown to users without a location.
    no_location_message = "Click here to set your location"

    # Set automatically by get_coordinates().
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
