"""Setting a club up: its details, membership settings, payment accounts, email.

The club admin's own edit pages, plus the member merge and the barcode label sheets. Nothing here
is public.
"""

import logging
from datetime import datetime
from datetime import timezone as date_tz
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView, View
from django.views.generic.edit import (
    UpdateView,
)
from django_weasyprint import WeasyTemplateResponseMixin
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions import club_events
from auctions.forms import (
    ClubEditForm,
    ClubEmailSettingsForm,
    ClubMemberMergeReviewForm,
    ClubMemberMergeTargetForm,
    ClubMembershipSettingsForm,
    ClubPayPalCredentialsForm,
)
from auctions.models import (
    AuctionTOS,
    BapAward,
    ClubHistory,
    ClubMember,
    Invoice,
    InvoicePayment,
    PayPalSeller,
    SquareSeller,
    UserLabelPrefs,
)

from .base import ClubViewMixin, check_club_permission, close_modal_response
from .club_pages import _membership_renewal_state, _process_pending_membership_renewal_for_member

logger = logging.getLogger(__name__)


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

    def get(self, request, *args, **kwargs):
        """A form with nothing complete in it used to 404, which is what "Download PDF" did on a
        row where the label type was still on "- select -" or the amount was blank. A 404 reads as
        "this feature is broken"; the truth is that there is nothing to print yet, so say that on
        the page the person is already looking at. The form guards this in the browser too -- this
        is the backstop for a submit that gets past it."""
        members = ClubMember.objects.filter(club=self.club, is_deleted=False, membership_number__isnull=False)
        self.labels = ClubBarcodeLabelsView._build_labels(self, request.GET, members)
        if not self.labels:
            messages.error(
                request,
                "There was nothing to print. Every row needs a label type and the fields that go "
                "with it: a bidder number, an amount and some label text, or a member.",
            )
            return redirect("club_barcode_labels", slug=self.club.slug)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        dims = ClubBarcodeLabelsView._label_dim_context(prefs)
        context.update(dims)
        context["print_border"] = prefs.print_border

        context["labels"] = self.labels

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
        elif action == "resend_card":
            if member.is_deleted or not member.club.show_member_barcode:
                raise Http404
            context = {
                "title": "Resend membership card",
                "body": format_html(
                    "Email {} ({}) a link to their membership card",
                    member.display_name,
                    member.email or "no email address on file",
                ),
                "action_url": reverse("club_member_resend_card", kwargs={"pk": pk}),
                "confirm_button_label": "Send email",
                "confirm_button_class": "btn-primary",
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
        if not (self.club.membership_annual_fee and (self.club.can_accept_paypal or self.club.can_accept_square)):
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
                        "permission_manage_donations",
                        "permission_send_announcements",
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
        kwargs["webhook_url"] = self.request.build_absolute_uri(reverse("club_paypal_subscription_webhook"))
        kwargs["show_paypal_subscriptions"] = self.club.supports_paypal_subscriptions
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
        # Wallet buttons ride along under the barcode in the real emails, but only for the
        # wallets this site is actually set up for.
        from auctions import apple_wallet, google_wallet

        context["google_wallet_enabled"] = google_wallet.is_configured()
        context["apple_wallet_enabled"] = apple_wallet.is_configured()

        # Build the next-event HTML fragment exactly once on the server so the JS preview just
        # toggles visibility (no client-side templating). Uses the same builder as the real
        # emails, with as_links=False so a preview never contains working links.
        from auctions.tasks import next_event_fragment

        context["next_event"] = club_events.next_member_facing_event(self.club)
        _, next_event_html = next_event_fragment(self.club, Site.objects.get_current(), as_links=False)

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
            "next_event_html": next_event_html,
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
