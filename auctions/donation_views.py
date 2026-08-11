"""Views for donation tracking: the vendor table, the vendor panel, and the contact dialog.

Kept out of ``views.py`` (which is already ~25k lines) but wired into the same URL conf and using
the same building blocks: :class:`~auctions.views.ClubViewMixin` for permissions,
:class:`~auctions.views.HTMxTableView` for the table, and the ``#modals-here`` HTMX modal machinery
for everything that opens over the top of it.

The public unsubscribe view and the inbound mail webhook also live here -- they belong to this
feature even though neither is a club admin page.
"""

from __future__ import annotations

import logging
import secrets as secrets_module

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView, UpdateView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView as DRFAPIView

from . import donations
from .donations import DonationSendError
from .email_routing import email_routing_enabled, resolve_donation_alias
from .filters import DonationVendorFilter
from .forms import (
    ClubDonationSettingsForm,
    DonationContactForm,
    DonationEmailEditForm,
    DonationVendorForm,
)
from .llm import LLMError, assist_enabled
from .models import Club, ClubHistory, DonationEmail, DonationVendor
from .tables import DonationVendorHTMxTable
from .views import (
    ClubViewMixin,
    HTMxTableView,
    close_modal_response,
)

logger = logging.getLogger(__name__)


class DonationPermissionMixin(ClubViewMixin):
    """Gate every donation page behind the donation permission and the feature flag.

    Donation tracking holds third-party contact details and can send mail in the club's name, so it
    has a permission of its own rather than riding on member management: the people a club trusts
    with its member list are not necessarily the ones it wants writing to businesses in its name.
    ``check_club_permission`` grants everything to ``permission_admin``, so club admins are covered
    without naming them here.
    """

    def check_donation_permission(self):
        if not self.club.enable_donation_tracking:
            raise Http404
        if self.request.user.is_authenticated and self.user_has_club_permission("permission_manage_donations"):
            return True
        raise PermissionDenied


class ClubDonationVendorsView(LoginRequiredMixin, DonationPermissionMixin, HTMxTableView):
    """The donation tracking table: every vendor, filtered by status."""

    active_tab = "donations"
    model = DonationVendor
    table_class = DonationVendorHTMxTable
    filterset_class = DonationVendorFilter
    template_name = "auctions/club_donation_vendors.html"
    htmx_table_header_template = "auctions/partials/donation_table_header.html"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated:
            self.check_donation_permission()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return DonationVendor.objects.filter(club=self.club).order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["can_send"] = self.club.sends_donation_email
        context["assist_enabled"] = assist_enabled()
        vendors = self.get_queryset().filter(is_deleted=False)
        context["followup_due_count"] = vendors.filter(followup_due__lte=timezone.now()).count()
        # Sending from this site is blocked without a postal address (see donations.send_request);
        # say so here rather than letting an admin find out at the end of the contact dialog.
        context["needs_mailing_address"] = self.club.sends_donation_email and not (
            self.club.donation_mailing_address.strip()
        )
        # The status menu is written by the header template rather than by crispy, so it needs the
        # choices and the current selection handed to it.
        selected_status = (self.request.GET.get("status") or "").strip()
        context["status_choices"] = DonationVendor.STATUS_CHOICES
        context["selected_status"] = selected_status
        context["selected_status_label"] = dict(DonationVendor.STATUS_CHOICES).get(selected_status, "Any status")
        return context


class ClubDonationSettingsView(LoginRequiredMixin, ClubViewMixin, UpdateView):
    """Turn donation tracking on, choose how mail goes out, and set the club's standing context."""

    active_tab = "donation_settings"
    template_name = "auctions/club_donation_settings.html"
    form_class = ClubDonationSettingsForm

    def get_object(self):
        return self.club

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["routing_enabled"] = email_routing_enabled()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["routing_enabled"] = email_routing_enabled()
        context["assist_enabled"] = assist_enabled()
        context["email_domain"] = settings.EMAIL_ROUTING_DOMAIN
        context["donation_contact"] = self.club.donation_email_recipient
        return context

    def get_success_url(self):
        messages.success(self.request, "Donation tracking settings saved.")
        return reverse("club_donation_settings", kwargs={"slug": self.club.slug})

    def form_valid(self, form):
        was_enabled = Club.objects.filter(pk=self.club.pk).values_list("enable_donation_tracking", flat=True).first()
        result = super().form_valid(form)
        if was_enabled != self.object.enable_donation_tracking:
            state = "on" if self.object.enable_donation_tracking else "off"
            action = f"Turned donation tracking {state}"
        else:
            action = "Updated donation tracking settings"
        ClubHistory.objects.create(
            club=self.club,
            user=self.request.user,
            action=action,
            applies_to="DONATIONS",
        )
        return result


class DonationVendorPanelView(LoginRequiredMixin, DonationPermissionMixin, View):
    """The panel behind a vendor's name: their email history, and a form to edit their details.

    One view serves both the create and edit cases, the way ``AuctionTOSAdmin`` does -- a URL with
    a club slug creates, a URL with a vendor pk edits.
    """

    def _load(self, request, slug=None, pk=None):
        if pk is not None:
            self.vendor = get_object_or_404(DonationVendor.objects.select_related("club"), pk=pk, is_deleted=False)
            self.club = self.vendor.club
        else:
            self.vendor = None
            self.get_club(slug)
        self.check_donation_permission()

    def _context(self, request, form, editing=False):
        vendor = self.vendor
        return {
            "club": self.club,
            "vendor": vendor,
            "form": form,
            "editing": editing or vendor is None,
            "emails": vendor.emails.all() if vendor else [],
            "modal_title": vendor.name if vendor else "Add vendor",
            "can_send": self.club.sends_donation_email,
            "contact_url": reverse("club_donation_contact", kwargs={"pk": vendor.pk}) if vendor else "",
            "cannot_contact_reason": vendor.cannot_contact_reason if vendor else "",
            "reply_to_address": vendor.reply_to_address if vendor else "",
        }

    def get(self, request, slug=None, pk=None):
        self._load(request, slug=slug, pk=pk)
        # ?edit=1 opens straight into the form; without it an existing vendor shows their history.
        editing = bool(request.GET.get("edit")) or self.vendor is None
        form = DonationVendorForm(
            instance=self.vendor,
            club=self.club,
            post_url=self._post_url(),
        )
        return render(request, "auctions/donation_vendor_panel.html", self._context(request, form, editing=editing))

    def _post_url(self):
        if self.vendor:
            return reverse("club_donation_vendor", kwargs={"pk": self.vendor.pk})
        return reverse("club_donation_vendor_create", kwargs={"slug": self.club.slug})

    def post(self, request, slug=None, pk=None):
        self._load(request, slug=slug, pk=pk)
        form = DonationVendorForm(
            request.POST,
            instance=self.vendor,
            club=self.club,
            post_url=self._post_url(),
        )
        if form.is_valid():
            creating = self.vendor is None
            vendor = form.save()
            verb = "Added" if creating else "Updated"
            ClubHistory.objects.create(
                club=self.club,
                user=request.user,
                action=f"{verb} donation vendor {vendor.name}",
                applies_to="DONATIONS",
            )
            # A Django message rather than close_modal_response's toast: "reload-page" reloads
            # immediately, which would wipe a toast off the screen before it could be read.
            messages.success(request, f"{vendor.name} {'added' if creating else 'saved'}.")
            return close_modal_response("reload-page")
        return render(request, "auctions/donation_vendor_panel.html", self._context(request, form, editing=True))


class DonationVendorDeleteView(LoginRequiredMixin, DonationPermissionMixin, View):
    """Soft-delete a vendor. Their email history goes with them from the club's view."""

    def post(self, request, pk):
        self.vendor = get_object_or_404(DonationVendor.objects.select_related("club"), pk=pk, is_deleted=False)
        self.club = self.vendor.club
        self.check_donation_permission()
        self.vendor.is_deleted = True
        self.vendor.save(update_fields=["is_deleted"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Removed donation vendor {self.vendor.name}",
            applies_to="DONATIONS",
        )
        messages.success(request, f"{self.vendor.name} removed.")
        return close_modal_response("reload-page")


class DonationContactView(LoginRequiredMixin, DonationPermissionMixin, View):
    """The write-an-email dialog, in three steps within one modal.

    ``GET``                    -> step 1: context and last email.
    ``POST step=generate``     -> step 2: the drafted email, editable.
    ``POST step=send``         -> commit: send it or mark it copied, then close.

    Step 2 is reachable again from step 2 (the Regenerate button) so an admin who doesn't like the
    draft isn't stuck retyping their context.
    """

    def _load(self, request, pk):
        self.vendor = get_object_or_404(DonationVendor.objects.select_related("club"), pk=pk, is_deleted=False)
        self.club = self.vendor.club
        self.check_donation_permission()
        if not self.vendor.can_be_contacted:
            raise PermissionDenied(self.vendor.cannot_contact_reason)

    def _last_email_text(self):
        """Their most recent incoming message, to prefill the 'last email' box."""
        last = self.vendor.emails.filter(direction=DonationEmail.DIRECTION_INCOMING).first()
        return last.body if last else ""

    def _step_one_context(self, form):
        return {
            "club": self.club,
            "vendor": self.vendor,
            "form": form,
            "step": "context",
            "modal_title": f"Contact {self.vendor.name}",
            "post_url": reverse("club_donation_contact", kwargs={"pk": self.vendor.pk}),
            "assist_enabled": assist_enabled(),
        }

    def _step_two_context(self, form, error=""):
        return {
            "club": self.club,
            "vendor": self.vendor,
            "form": form,
            "step": "review",
            "modal_title": f"Contact {self.vendor.name}",
            "post_url": reverse("club_donation_contact", kwargs={"pk": self.vendor.pk}),
            "can_send": self.club.sends_donation_email,
            "footer_preview": donations.unsubscribe_footer(self.vendor),
            "error": error,
        }

    def get(self, request, pk):
        self._load(request, pk)
        form = DonationContactForm(
            initial={
                "context": self.vendor.context,
                "last_email": self._last_email_text(),
            }
        )
        return render(request, "auctions/donation_contact_modal.html", self._step_one_context(form))

    def post(self, request, pk):
        self._load(request, pk)
        step = request.POST.get("step")
        if step == "generate":
            return self._generate(request)
        if step == "send":
            return self._send(request)
        # No recognised step. Whatever went wrong, the safe landing is the review screen with the
        # draft intact -- never a send the admin didn't ask for, and never a 404 that eats it.
        form = DonationEmailEditForm(request.POST)
        form.is_valid()
        if form.is_bound and form.data.get("body"):
            return render(request, "auctions/donation_contact_modal.html", self._step_two_context(form))
        return render(request, "auctions/donation_contact_modal.html", self._step_one_context(DonationContactForm()))

    def _generate(self, request):
        # Reached from step 1 (the Next button) and from step 2 (Rewrite). Step 2 carries the same
        # two values in hidden inputs, so one form reads both cases.
        form = DonationContactForm(request.POST)
        if not form.is_valid():
            return render(request, "auctions/donation_contact_modal.html", self._step_one_context(form))
        context = form.cleaned_data["context"]
        last_email = form.cleaned_data["last_email"]
        # Remember what the admin told us about this vendor so the next email doesn't start blank.
        if context.strip() and context.strip() != self.vendor.context.strip():
            self.vendor.context = context.strip()
            self.vendor.save(update_fields=["context"])
        try:
            subject, body = donations.draft_request(
                self.vendor,
                context=context,
                last_email=last_email,
                user=request.user,
            )
        except LLMError as error:
            # Keep them on step 1 with their typing intact and say what went wrong.
            step_one = self._step_one_context(form)
            step_one["error"] = str(error)
            return render(request, "auctions/donation_contact_modal.html", step_one)
        edit_form = DonationEmailEditForm(
            initial={"subject": subject, "body": body, "context": context, "last_email": last_email}
        )
        return render(request, "auctions/donation_contact_modal.html", self._step_two_context(edit_form))

    def _send(self, request):
        form = DonationEmailEditForm(request.POST)
        if not form.is_valid():
            return render(request, "auctions/donation_contact_modal.html", self._step_two_context(form))
        subject = form.cleaned_data["subject"]
        body = form.cleaned_data["body"]
        try:
            if self.club.sends_donation_email:
                donations.send_request(self.vendor, subject=subject, body=body, user=request.user)
                toast = f"Donation request sent to {self.vendor.name}."
            else:
                donations.record_copied_request(self.vendor, subject=subject, body=body, user=request.user)
                toast = f"Recorded a donation request for {self.vendor.name}."
        except DonationSendError as error:
            return render(request, "auctions/donation_contact_modal.html", self._step_two_context(form, str(error)))
        messages.success(request, toast)
        return close_modal_response("reload-page")


class DonationEmailPreviewView(LoginRequiredMixin, DonationPermissionMixin, View):
    """Show one stored message in full. Opened from the history list in the vendor panel."""

    def get(self, request, pk):
        email_row = get_object_or_404(DonationEmail.objects.select_related("vendor__club"), pk=pk)
        self.vendor = email_row.vendor
        self.club = self.vendor.club
        self.check_donation_permission()
        return render(
            request,
            "auctions/donation_email_modal.html",
            {
                "club": self.club,
                "vendor": self.vendor,
                "email": email_row,
                "modal_title": email_row.subject or "(no subject)",
            },
        )


class DonationUnsubscribeView(TemplateView):
    """The vendor-facing opt-out page. No login, and no way back.

    A GET only *offers* the unsubscribe; the POST performs it. Mail clients and security scanners
    routinely fetch every link in a message, and a GET that acted would unsubscribe vendors who
    never clicked anything.
    """

    template_name = "auctions/donation_unsubscribe.html"

    def get_vendor(self):
        return get_object_or_404(DonationVendor, uuid=self.kwargs["uuid"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vendor = self.get_vendor()
        context["vendor"] = vendor
        context["club"] = vendor.club
        context["already_unsubscribed"] = vendor.unsubscribed
        return context

    def post(self, request, *args, **kwargs):
        vendor = self.get_vendor()
        if not vendor.unsubscribed:
            donations.unsubscribe_vendor(vendor)
        context = self.get_context_data(**kwargs)
        context["just_unsubscribed"] = True
        context["already_unsubscribed"] = True
        return self.render_to_response(context)


class InboundDonationEmailView(DRFAPIView):
    """Webhook: record an inbound donation reply, then summarize it.

    Called by the SES inbound Lambda for any address that
    :func:`~auctions.email_routing.resolve_routing_info` reported as ``kind == "donation"``. The
    Lambda still forwards the message to the club's donation contact (when there is one); this
    endpoint is what makes the reply show up against the vendor.

    Authenticated with the same ``X-Routing-Secret`` shared secret as the resolve endpoint.

    POST /api/v1/email-routing/donation/
        {"address": "<to address or local part>", "from": "...", "subject": "...",
         "body": "...", "message_id": "...", "recipients": "..."}

    Anything that doesn't resolve to a live vendor is dropped with a 200 -- a 4xx would make SES
    retry a message that will never match.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = (getattr(settings, "INBOUND_ROUTING_SECRET", "") or "").strip()
        provided = (request.META.get("HTTP_X_ROUTING_SECRET", "") or "").strip()
        if not secret or not provided or not secrets_module.compare_digest(provided, secret):
            return Response({"error": "invalid or missing routing secret"}, status=401)
        if not email_routing_enabled():
            return Response({"error": "email routing is not enabled"}, status=503)

        payload = request.data if isinstance(request.data, dict) else {}
        address = str(payload.get("address") or "").strip().lower()
        local_part = address.split("@")[0]
        match = resolve_donation_alias(local_part)
        if not match:
            # Not a donation address, or the vendor/club is gone. Silently accepted and dropped.
            return Response({"status": "dropped"}, status=200)

        vendor = match["vendor"]
        sender = donations.sender_address(payload.get("from"))
        email_row, created = donations.record_incoming(
            vendor,
            sender=sender,
            recipients=str(payload.get("recipients") or address),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            message_id=str(payload.get("message_id") or ""),
        )
        if not created:
            return Response({"status": "duplicate", "email_id": email_row.pk}, status=200)

        # Summarizing is best-effort: the message is already safely stored either way.
        summary = donations.summarize_incoming(email_row)
        return Response(
            {
                "status": "recorded",
                "email_id": email_row.pk,
                "vendor": vendor.name,
                "summarized": bool(summary),
            },
            status=200,
        )
