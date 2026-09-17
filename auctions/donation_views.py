"""Donation tracking views: the vendor table, the vendor panel, and the contact dialog.

Uses the same building blocks as ``views.py``: :class:`~auctions.views.ClubViewMixin`,
:class:`~auctions.views.HTMxTableView` and the ``#modals-here`` modal machinery. The public
unsubscribe view and the inbound mail webhook are here too.
"""

from __future__ import annotations

import logging
import secrets as secrets_module

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import F, OuterRef, Subquery
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

    Donation tracking holds third-party contacts and sends mail in the club's name, so it has its own
    permission rather than riding on member management. ``check_club_permission`` covers club admins.
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
        # Most overdue first: a work queue. Vendors with no date go last (they unsubscribed or an
        # admin cleared it); name breaks ties.
        return (
            DonationVendor.objects.filter(club=self.club)
            .annotate(
                # What they last said, in one line. A subquery, not a prefetch, since the table
                # wants one string per vendor. Blank when the newest reply has no summary, rather
                # than an older one, which would read as their latest word.
                latest_reply_summary=Subquery(
                    DonationEmail.objects.filter(vendor=OuterRef("pk"), direction=DonationEmail.DIRECTION_INCOMING)
                    .order_by("-date")
                    .values("summary")[:1]
                )
            )
            .order_by(F("followup_due").asc(nulls_last=True), "name")
        )

    def get_table_kwargs(self, **kwargs):
        # Counted once for the page: every Contact button asks the same question.
        kwargs = super().get_table_kwargs(**kwargs)
        kwargs["quota"] = donations.donation_email_quota(self.club)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        context["can_send"] = self.club.sends_donation_email
        context["assist_enabled"] = assist_enabled()
        context["quota"] = donations.donation_email_quota(self.club)
        # Not self.get_queryset(): counting through the annotation would run the latest-reply
        # subquery per vendor.
        context["followup_due_count"] = DonationVendor.objects.filter(
            club=self.club, is_deleted=False, followup_due__lte=timezone.now()
        ).count()
        # Sending is blocked without a postal address (donations.send_request); say so here rather
        # than at the end of the dialog.
        context["needs_mailing_address"] = self.club.sends_donation_email and not (
            self.club.donation_mailing_address.strip()
        )
        # The status menu is written by the header template, not crispy.
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
        if self.object.enable_donation_tracking:
            # Settings are a means to an end: what the admin came here to do is track vendors.
            return reverse("club_donation_vendors", kwargs={"slug": self.club.slug})
        # Tracking is off, and the vendor page 404s in that state -- stay put.
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
    """The panel behind a vendor's name: their email history and a form to edit them.

    One view serves create and edit, like ``AuctionTOSAdmin``: a club slug creates, a vendor pk edits.
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
            "cannot_contact_reason": donations.contact_blocked_reason(vendor) if vendor else "",
            "reply_to_address": vendor.reply_to_address if vendor else "",
        }

    def get(self, request, slug=None, pk=None):
        self._load(request, slug=slug, pk=pk)
        # ?edit=1 opens the form; otherwise an existing vendor shows their history.
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
            # A Django message, not a toast: "reload-page" would wipe a toast off the screen.
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

    ``GET`` is step 1 (context and last email), ``POST step=generate`` step 2 (the editable draft), and
    ``POST step=send`` commits. Step 2 is reachable from itself (Regenerate).
    """

    def _load(self, request, pk):
        self.vendor = get_object_or_404(DonationVendor.objects.select_related("club"), pk=pk, is_deleted=False)
        self.club = self.vendor.club
        self.check_donation_permission()
        if not self.vendor.can_be_contacted:
            raise PermissionDenied(self.vendor.cannot_contact_reason)
        self.quota = donations.donation_email_quota(self.club)

    def _previous_email(self):
        """The last message in this conversation, in either direction: their reply is something to answer, and
        our own unanswered request is something to nudge about.
        """
        return self.vendor.emails.first()

    def _last_email_initial(self, previous):
        """Prefill for the 'last email' box: the previous message, without our own footer."""
        if not previous:
            return {"last_email": "", "last_email_direction": ""}
        return {
            "last_email": donations.strip_donation_footer(previous.body),
            "last_email_direction": previous.direction,
        }

    def _blocked_context(self):
        """The dialog replaced by "not today", when the daily allowance is gone."""
        return {
            "club": self.club,
            "vendor": self.vendor,
            "step": "blocked",
            "modal_title": f"Contact {self.vendor.name}",
            "error": self.quota.exhausted_message,
            "quota": self.quota,
        }

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
        if self.quota.exhausted:
            return render(request, "auctions/donation_contact_modal.html", self._blocked_context())
        form = DonationContactForm(
            initial={
                "context": self.vendor.context,
                **self._last_email_initial(self._previous_email()),
            }
        )
        return render(request, "auctions/donation_contact_modal.html", self._step_one_context(form))

    def post(self, request, pk):
        self._load(request, pk)
        step = request.POST.get("step")
        if self.quota.exhausted:
            # Nothing may be written past the limit, so don't offer a screen that ends in a refusal.
            return render(request, "auctions/donation_contact_modal.html", self._blocked_context())
        if step == "generate":
            return self._generate(request)
        if step == "send":
            return self._send(request)
        # No recognised step: land on the review screen with the draft intact, never a send nobody
        # asked for.
        form = DonationEmailEditForm(request.POST)
        form.is_valid()
        if form.is_bound and form.data.get("body"):
            return render(request, "auctions/donation_contact_modal.html", self._step_two_context(form))
        # Step 1 again, bound, so their typing is still on screen.
        return render(
            request, "auctions/donation_contact_modal.html", self._step_one_context(DonationContactForm(request.POST))
        )

    def _remember_context(self, context):
        """Keep what the admin said about this vendor, so the next email doesn't start blank."""
        if context.strip() and context.strip() != self.vendor.context.strip():
            self.vendor.context = context.strip()
            self.vendor.save(update_fields=["context"])

    def _generate(self, request):
        # Only reached from step 1, so a failure lands them there with their typing.
        form = DonationContactForm(request.POST)
        if not form.is_valid():
            return render(request, "auctions/donation_contact_modal.html", self._step_one_context(form))
        context = form.cleaned_data["context"]
        last_email = form.cleaned_data["last_email"]
        self._remember_context(context)
        try:
            subject, body = donations.draft_request(
                self.vendor,
                context=context,
                last_email=last_email,
                last_email_is_outgoing=form.cleaned_data["last_email_direction"] == DonationEmail.DIRECTION_OUTGOING,
                user=request.user,
            )
        except LLMError as error:
            step_one = self._step_one_context(form)
            step_one["error"] = str(error)
            return render(request, "auctions/donation_contact_modal.html", step_one)
        previous = self._previous_email()
        if previous:
            # Anything after the first email belongs to the running thread, so it goes out as a reply.
            subject = donations.followup_subject(previous.subject, subject) or subject
        return render(
            request,
            "auctions/donation_contact_modal.html",
            self._step_two_context(DonationEmailEditForm(initial={"subject": subject, "body": body})),
        )

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
    """Show one stored message in full, from the history list in the vendor panel."""

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

    GET only offers the unsubscribe; POST performs it. Mail clients fetch every link in a message.
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

    Called by the SES Lambda for addresses :func:`~auctions.email_routing.resolve_routing_info` reports
    as ``kind == "donation"``. Authenticated with the same ``X-Routing-Secret`` as the resolve endpoint.

    POST /api/v1/email-routing/donation/ with address, from, subject, body, message_id and recipients.
    Anything that doesn't resolve to a live vendor is dropped with a 200, since a 4xx makes SES retry.
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
            # Not a donation address, or the vendor is gone.
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

        # Summarizing is best-effort; the message is stored either way.
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
