"""The club's list of people: joining, renewing, permissions, cards.

The member admin pages, the self-service renewal, and the wallet passes and barcodes a member
carries. Renewal state is worked out in :mod:`auctions.views.base` so that this module, the
invoices and the webhooks all reach the same answer.
"""

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.generic import View
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.filters import (
    AuctionTOSFilter,
    rhyming_name_q,
)
from auctions.forms import (
    ClubMemberAdminForm,
    ClubMemberDiscordForm,
    ClubMemberPermissionsForm,
)
from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubHistory,
    ClubMember,
    ClubMoney,
)
from auctions.tasks import (
    maybe_send_membership_renewal_confirmation,
)

from .ajax import APIPostView
from .base import (
    _UNSET,
    ClubViewMixin,
    _compute_member_renewal_expiration,
    _default_pickup_location_for_auction,
    _upsert_clubmember_shadow_tos,
    auctions_available_for_contact_autofill,
    check_club_permission,
    close_modal_response,
    club_ids_available_for_contact_autofill,
)

logger = logging.getLogger(__name__)


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


def renew_club_member(member, *, acting_user=None, actor="", money_description=""):
    """Extend a membership by one period and record it, returning the member.

    Shared by the Renew button on the member list and the API-key renew endpoint so the two
    can't drift: same expiration math, same club history, same ledger entry, same confirmation
    email.  ``actor`` names a non-user actor (an API key) for the history line.
    """
    today = timezone.now().date()
    member.membership_expiration_date = _compute_member_renewal_expiration(member.club, member, today)
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
    via = f" via {actor}" if actor else ""
    ClubHistory.objects.create(
        club=member.club,
        user=acting_user,
        action=f"Renewed membership for {member}{via}; new expiration {new_exp_str}",
        applies_to="MEMBERSHIP",
    )
    if member.club.membership_annual_fee:
        ClubMoney.objects.create(
            club=member.club,
            created_by=acting_user,
            date=today,
            amount=member.club.membership_annual_fee,
            description=money_description or f"Membership renewal for {member}{via}",
            category=ClubMoney.CATEGORY_MEMBERSHIP,
        )
    try:
        maybe_send_membership_renewal_confirmation(member)
    except Exception:
        logger.exception("Failed to send membership renewal confirmation for club member %s", member.pk)
    return member


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
        renew_club_member(member, acting_user=request.user, money_description=f"Manual membership renewal for {member}")
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


class ClubMemberResendCardView(APIView):
    """Email a member a fresh link to their membership card."""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_member(self, pk, request):
        try:
            member = ClubMember.objects.get(pk=pk, is_deleted=False)
        except ClubMember.DoesNotExist:
            raise Http404
        if not check_club_permission(request.user, member.club, "permission_add_edit"):
            raise PermissionDenied()
        if not member.club.show_member_barcode:
            # No membership cards for this club — admin endpoint should not be reachable.
            raise Http404
        return member

    def post(self, request, pk):
        from auctions.tasks import send_membership_card_email

        member = self._get_member(pk, request)
        # Rather than hiding the action for members we can't email, say why on click.
        if not member.email:
            return close_modal_response(
                toast=f"{member.display_name} has no email address on file.", toast_type="danger"
            )
        if member.contact_status == "do_not_contact":
            return close_modal_response(
                toast=f"{member.display_name} is marked do-not-contact, so no email was sent.",
                toast_type="danger",
            )
        try:
            sent = send_membership_card_email(member)
        except Exception:
            logger.exception("Failed to send membership card email to club member %s", member.pk)
            sent = False
        if not sent:
            return close_modal_response(
                toast=f"Couldn't email {member.display_name} — check the site's email settings.",
                toast_type="danger",
            )
        ClubHistory.objects.create(
            club=member.club,
            user=request.user,
            action=f"Emailed membership card to {member} ({member.email})",
            applies_to="MEMBERS",
        )
        return close_modal_response(toast=f"Membership card emailed to {member.email}.")


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
