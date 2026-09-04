"""The breeder award program: settings, overrides, awards and the lots behind them.

Points are awarded against a species, so the two override models let a club say "this genus counts
as that category" without touching the species list everyone shares.
"""

import logging
from datetime import date as date_type

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import View
from django.views.generic.edit import (
    UpdateView,
)
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.filters import (
    BapAwardFilter,
    ClubBapLotFilter,
)
from auctions.forms import (
    BapAwardForm,
    ClubBapCategoryOverrideForm,
    ClubBapGenusOverrideForm,
    ClubBapSettingsForm,
    LotCategoryForm,
)
from auctions.models import (
    BapAward,
    Club,
    ClubBapCategoryOverride,
    ClubBapGenusOverride,
    ClubHistory,
    ClubMember,
    Lot,
    normalize_email,
)
from auctions.services import (
    bap_review_lots,
)
from auctions.tables import (
    BapAwardHTMxTable,
    ClubBapLotHTMxTable,
)

from .base import ClubViewMixin, HTMxTableView, check_club_permission, close_modal_response
from .bulk_add import CSVContactImportMixin

logger = logging.getLogger(__name__)


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
        context["bap_genus_overrides"] = ClubBapGenusOverride.objects.filter(club=self.club)
        context["genus_override_form"] = ClubBapGenusOverrideForm()
        # What the free-text genus box offers as suggestions.  Deliberately the genera this club's
        # own lots use rather than all 4,000 in the database: a rule is only worth writing for a
        # genus the club actually sells, and clean_genus still accepts anything real that is typed.
        context["bap_genus_choices"] = list(
            Lot.objects.filter(auction__club=self.club, species__isnull=False, is_deleted=False)
            .exclude(species__genus="")
            .order_by("species__genus")
            .values_list("species__genus", flat=True)
            .distinct()
        )
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


class ClubBapGenusOverrideSaveView(LoginRequiredMixin, ClubViewMixin, View):
    """Create or update a per-genus BAP point override for a club.

    The genus twin of :class:`ClubBapCategoryOverrideSaveView`; a genus rule outranks a category
    rule when a lot's species falls under both (see ``Lot.bap_points_for_club``).
    """

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug):
        form = ClubBapGenusOverrideForm(request.POST)
        if form.is_valid():
            genus = form.cleaned_data["genus"]
            points = form.cleaned_data["points"]
            ClubBapGenusOverride.objects.update_or_create(club=self.club, genus=genus, defaults={"points": points})
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Set BAP point override for the genus {genus}: {points} pts",
                applies_to="BAP",
            )
        else:
            for error in form.errors.get("genus", []):
                messages.error(request, error)
        return redirect(reverse("club_bap_settings", kwargs={"slug": self.club.slug}))


class ClubBapGenusOverrideDeleteView(LoginRequiredMixin, ClubViewMixin, View):
    """Delete a per-genus BAP point override."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_manage_bap"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk):
        override = ClubBapGenusOverride.objects.filter(pk=pk, club=self.club).first()
        if override:
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"Removed BAP point override for the genus {override.genus}",
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
        # In services because the ``points_queue`` skill lists the same rows, and "which lots is
        # this club's points desk looking at" must have one answer.
        return bap_review_lots(self.club)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
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
            # category_automatically_added comes along because the form clears it: it is what stops
            # Lot._do_save from re-deriving the category from the species and undoing this edit.
            update_fields = ["species_category", "category_automatically_added"]
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
        points = lot.default_bap_points(club)
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
