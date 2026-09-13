"""The copyright policy page, the notice form, and the report button on a lot.

Three pages that exist because this site hosts photographs uploaded by its users:

* ``/dmca/`` publishes the designated agent. 17 U.S.C. 512(c)(2) requires the agent's name,
  address, phone number and email to be both filed with the Copyright Office and made available to
  the public on the site, and the safe harbour is conditioned on doing both. The page 404s on a
  deployment that has not configured one -- see :mod:`auctions.dmca` for why that is the right
  answer rather than a placeholder.
* ``/dmca/notice/`` collects a notice with the six parts 512(c)(3)(A) asks for. It is a
  convenience, not the channel: the agent's address is what the statute designates.
* ``/lots/<pk>/report/`` is everything else -- a scam, an animal that should not be sold, abuse.
  App Store Review Guideline 1.2 requires an app carrying user-generated content to offer "a
  mechanism to report offensive content and timely responses to concerns"; this is that mechanism,
  and the blocking half of the same guideline is ``CreateUserBan``, which already exists.

All three are open to people who are not signed in, and all three are rate limited per IP on top of
the invisible reCAPTCHA. A rightsholder is not going to make an account to file a notice, and
somebody who has just been scammed should not have to either.
"""

import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from auctions import dmca
from auctions.models import Lot
from auctions.moderation_forms import CopyrightNoticeForm, ReportContentForm

logger = logging.getLogger(__name__)

#: Submissions one address may send in an hour, matching SupportView.MESSAGES_PER_HOUR. Generous:
#: a rightsholder with a real complaint sends one or two, and somebody working through a catalogue
#: of stolen photographs should be writing to the agent rather than filling the form out eleven
#: times.
SUBMISSIONS_PER_HOUR = 5


def _over_the_limit(request, bucket):
    from auctions.mobile.services.ar import _client_ip

    key = f"{bucket}:{_client_ip(request) or 'unknown'}"
    count = cache.get_or_set(key, 0, timeout=3600)
    if count >= SUBMISSIONS_PER_HOUR:
        return True
    try:
        cache.incr(key)
    except ValueError:  # the window expired between the read and the increment
        cache.set(key, 1, timeout=3600)
    return False


class DmcaPolicyView(TemplateView):
    """The copyright policy, and the designated agent's details.

    404s when no agent is configured. That reads oddly for a legal page until you remember this is
    open source and forks exist: a fork operator is their own service provider with their own
    agent, and a page that fell back to this site's details would be publishing a Vermont fish
    club's address as the place to send notices about somebody else's website. No page is the
    honest answer, and the setup checklist is where an operator finds out they need one.
    """

    template_name = "dmca.html"

    def get_context_data(self, **kwargs):
        agent = dmca.agent()
        if not agent:
            raise Http404
        context = super().get_context_data(**kwargs)
        context["agent"] = agent
        context["strikes_before_termination"] = dmca.STRIKES_BEFORE_TERMINATION
        context["site_domain"] = Site.objects.get_current().domain
        return context


class CopyrightNoticeCreate(FormView):
    """Send a DMCA notice through the site instead of writing to the agent directly."""

    template_name = "dmca_notice.html"
    form_class = CopyrightNoticeForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["agent"] = dmca.agent()
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse("dmca_notice")

    def form_valid(self, form):
        from post_office import mail

        if _over_the_limit(self.request, "dmca-notice"):
            messages.error(
                self.request,
                "That's a lot of notices in a short time - please email them to our copyright "
                "agent instead, at the address on this page.",
            )
            return super().form_valid(form)
        notice = form.save(commit=False)
        if self.request.user.is_authenticated:
            notice.submitted_by = self.request.user
        notice.lot = _lot_from_urls(notice.material)
        notice.save()
        domain = Site.objects.get_current().domain
        recipient = dmca.agent_email()
        if recipient:
            mail.send(
                recipient,
                subject=f"DMCA notice from {notice.name}",
                message=(
                    f"A copyright notice was submitted at https://{domain}/dmca/notice/\n\n"
                    f"From: {notice.name} <{notice.email}> {notice.phone}\n"
                    f"On behalf of: {notice.on_behalf_of or 'themselves'}\n"
                    f"Address:\n{notice.address}\n\n"
                    f"Work said to be infringed:\n{notice.work}\n\n"
                    f"Material complained of:\n{notice.material}\n\n"
                    f"Good faith statement: {'yes' if notice.good_faith else 'NO'}\n"
                    f"Accuracy statement under penalty of perjury: {'yes' if notice.accurate else 'NO'}\n"
                    f"Signed: {notice.signature}\n"
                    f"Complete notice under 512(c)(3)(A): {'yes' if notice.is_complete else 'NO'}\n\n"
                    f"Act on it here: https://{domain}/admin/auctions/copyrightnotice/{notice.pk}/change/"
                ),
                # Reply-To rather than From: the From address is the site's own routed sender, and
                # a stranger's address there fails SPF and lands the one email that matters in spam.
                headers={"Reply-To": notice.email},
            )
        messages.success(
            self.request,
            "Your notice has been sent to our designated agent. We'll be in touch at the address you gave us.",
        )
        return super().form_valid(form)


class ReportContentCreate(FormView):
    """The report button on a lot page."""

    template_name = "report_content.html"
    form_class = ReportContentForm

    @property
    def lot(self):
        if not hasattr(self, "_lot"):
            self._lot = get_object_or_404(Lot, pk=self.kwargs["pk"], is_deleted=False)
        return self._lot

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["lot"] = self.lot
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return self.lot.lot_link or reverse("allLots")

    def form_valid(self, form):
        from post_office import mail

        if _over_the_limit(self.request, "content-report"):
            messages.error(
                self.request,
                "That's a lot of reports in a short time - give us a chance to read the ones you've already sent.",
            )
            return super().form_valid(form)
        report = form.save(commit=False)
        report.lot = self.lot
        domain = Site.objects.get_current().domain
        report.material = f"https://{domain}{self.lot.lot_link}" if self.lot.lot_link else str(self.lot)
        if self.request.user.is_authenticated:
            report.reported_by = self.request.user
            report.reporter_email = self.request.user.email or ""
        else:
            report.reporter_email = form.cleaned_data.get("reporter_email") or ""
        report.save()
        admin_email = settings.ADMINS[0][1] if settings.ADMINS else ""
        if admin_email:
            if report.reported_by:
                reporter = report.reported_by.username
            else:
                # Signed out: whatever they told us, and "anonymous" when they told us nothing.
                # Both fields are optional -- a report worth reading is worth reading unsigned.
                named = form.cleaned_data.get("reporter_name") or ""
                reporter = " ".join(filter(None, [named, report.reporter_email])) or "anonymous"
            mail.send(
                admin_email,
                subject=f"Lot reported: {self.lot.lot_name}",
                message=(
                    f"{reporter} reported {report.material}\n\n"
                    f"Reason: {report.get_reason_display()}\n\n"
                    f"{report.details}\n\n"
                    f"https://{domain}/admin/auctions/contentreport/{report.pk}/change/"
                ),
                headers={"Reply-To": report.reporter_email} if report.reporter_email else None,
            )
        messages.success(self.request, "Thanks - we've got your report and we'll take a look.")
        return super().form_valid(form)


def _lot_from_urls(text):
    """Best-effort: pick the lot out of the URLs a notice quotes, so the admin has one click.

    Wrong or absent is fine and expected -- a notice can name a club icon, a speaker photo, or a
    page that no longer exists. It only saves the operator a search when it works, and the
    operator confirms what actually comes down either way.
    """
    match = re.search(r"/lots/(\d+)", text or "")
    if not match:
        return None
    return Lot.objects.filter(pk=int(match.group(1)), is_deleted=False).first()
