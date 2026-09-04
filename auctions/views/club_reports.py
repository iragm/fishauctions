"""What a club's officers read: history, stats, the treasurer's report, money in and out.

``ClubMemberCSVImportView`` and its export partner are here rather than with the member pages
because they are a reporting job, and they share the column matching in
:mod:`auctions.views.bulk_add`.
"""

import csv
import logging
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Count,
    Sum,
    Value,
)
from django.db.models.base import Model as Model
from django.db.models.functions import TruncDay
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from auctions.filters import (
    ClubHistoryFilter,
)
from auctions.forms import (
    ClubMoneyBalanceForm,
    ClubMoneyForm,
    ClubTreasurerReportForm,
)
from auctions.helper_functions import get_currency_symbol
from auctions.models import (
    Auction,
    ClubHistory,
    ClubMember,
    ClubMoney,
    Invoice,
    normalize_email,
)
from auctions.tables import (
    ClubHistoryHTMxTable,
)

from .base import ClubViewMixin, HTMxTableView, check_club_permission
from .bulk_add import CSVContactImportMixin

logger = logging.getLogger(__name__)


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
        # the table prints who did each thing
        return ClubHistory.objects.filter(club=self.club).select_related("user").order_by("-timestamp")

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
        """One definition of "paid member" for the whole site — see filters.membership_paid_q."""
        from auctions.filters import membership_paid_q

        return membership_paid_q(timezone.now().date())

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
        # An admin importing their roster owns these rows now; the account-deletion rules follow.
        member.admin_edited = True
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
        from auctions.filters import ClubMemberFilter

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
