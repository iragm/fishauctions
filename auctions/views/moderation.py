"""The copyright policy page, the notice form, and the report button on a lot.

Three pages that exist because this site hosts photographs uploaded by its users:

* ``/dmca/`` publishes the designated agent, which 17 U.S.C. 512(c)(2) requires to be both filed
  with the Copyright Office and published here. It 404s on a deployment with no agent configured --
  see :mod:`auctions.dmca` for why that beats a placeholder.
* ``/dmca/notice/`` collects a notice with the six parts 512(c)(3)(A) asks for. A convenience, not
  the channel: the agent's address is what the statute designates.
* ``/lots/<pk>/report/`` is everything else. App Store Review Guideline 1.2 requires a mechanism to
  report offensive content; the blocking half is ``CreateUserBan``.

All three are open to people who are not signed in, and rate limited per IP on top of the invisible
reCAPTCHA: a rightsholder will not make an account to file a notice.
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

#: Submissions one address may send in an hour, matching SupportView.MESSAGES_PER_HOUR. Generous: a
#: rightsholder sends one or two, and somebody working through a catalogue should write to the agent.
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
    """The copyright policy and the designated agent's details.

    404s with no agent configured. That reads oddly for a legal page until you remember forks exist: a
    fork operator is their own service provider, and falling back to this site's details would publish a
    Vermont fish club's address as where to send notices about somebody else's website.
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
                # Reply-To rather than From: the From is the site's routed sender, and a stranger's
                # address there fails SPF.
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
                # Signed out: whatever they told us, or "anonymous". Both fields are optional -- a
                # report worth reading is worth reading unsigned.
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

    Wrong or absent is expected -- a notice can name a club icon, a speaker photo, or a page that no
    longer exists -- and the operator confirms what comes down either way.
    """
    match = re.search(r"/lots/(\d+)", text or "")
    if not match:
        return None
    return Lot.objects.filter(pk=int(match.group(1)), is_deleted=False).first()
