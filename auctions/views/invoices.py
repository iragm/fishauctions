"""Invoices as a person reads them: the list, one invoice, and the no-login link.

``InvoiceView`` is the page a buyer or seller is sent to after an auction. ``InvoiceNoLoginView`` is
the same page reached by a signed link, which is how someone who never made an account pays.
``InvoiceAdjustmentSave`` saves one adjustment row from that page as the admin edits it.
"""

import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.generic import DetailView, View
from django.views.generic.edit import (
    FormMixin,
)

from auctions import auction_survey, logistics
from auctions.filters import (
    InvoiceFilter,
)
from auctions.forms import (
    InvoiceAdjustmentForm,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Invoice,
    InvoiceAdjustment,
)
from auctions.tables import (
    InvoiceHTMxTable,
)

from .base import AuctionViewMixin, HTMxTableView, _ensure_invoice_renewal_state, check_club_permission

logger = logging.getLogger(__name__)

#: Whole dollars. InvoiceAdjustmentForm has no ceiling, and past the column's range the save 500s.
MAX_ADJUSTMENT_AMOUNT = 99999

#: The row key the invoice page gives each adjustment form: "r<pk>" for saved rows, "n<time>" for new ones.
ADJUSTMENT_ROW_KEY = re.compile(r"[a-z0-9]{1,24}")


class Invoices(LoginRequiredMixin, HTMxTableView):
    """Get all invoices for the current user"""

    model = Invoice
    table_class = InvoiceHTMxTable
    filterset_class = InvoiceFilter
    template_name = "all_invoices.html"
    filter_placeholder_text = "Search your invoices"

    def get_queryset(self):
        """Newest first.

        The table's own ``Meta.order_by`` decides what the page opens on; this keeps the queryset sensible
        for anything reading it without the table.
        """
        return (
            Invoice.objects.filter(auctiontos_user__user=self.request.user)
            .select_related("auction", "auction__club", "auctiontos_user")
            .order_by("-date")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filterset = context.get("filter")
        if filterset is not None and not filterset.qs.exists():
            context["no_results"] = self._build_no_results_html()
        return context

    def _build_no_results_html(self):
        """Empty state: nothing matched a search, or there is nothing here at all yet."""
        query = (self.request.GET.get("query") or "").strip()
        if query:
            return format_html(
                "<div class='text-center text-muted p-4'>No invoices match <strong>{}</strong>.</div>", query
            )
        return (
            "<div class='text-center text-muted p-4'>"
            "<i class='bi bi-bag fs-1 d-block mb-2'></i>"
            "<p class='mb-0'>You don't have any invoices yet. An invoice is created automatically "
            "once you buy or sell a lot in an auction.</p>"
            "</div>"
        )


class InvoiceCreateView(LoginRequiredMixin, View, AuctionViewMixin):
    """Create a new invoice for a user in an auction. POST only: it also checks the person in."""

    def post(self, request, *args, **kwargs):
        """Create invoice and redirect to invoice detail page"""
        response = self._create(request)
        if request.htmx:
            # The link is in an HTMx table; a plain redirect would be swapped into it.
            hx_response = HttpResponse(status=204)
            hx_response["HX-Redirect"] = response["Location"]
            return hx_response
        return response

    def _create(self, request):
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

        existing_invoice = Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).first()
        if existing_invoice:
            messages.info(request, "Invoice already exists for this user")
            return redirect(existing_invoice.get_absolute_url())

        invoice = Invoice.for_participant(auctiontos)
        invoice.recalculate()

        messages.success(request, f"Invoice created for {auctiontos.name}")
        return redirect(invoice.get_absolute_url())


def _can_adjust_invoice(user, invoice):
    """The admin test InvoiceView.dispatch applies before it marks a viewer as an admin of the invoice."""
    auction = invoice.auction or (invoice.auctiontos_user.auction if invoice.auctiontos_user else None)
    if auction:
        return bool(auction.permission_check(user))
    if invoice.club and user.is_authenticated:
        return bool(check_club_permission(user, invoice.club, "permission_add_edit"))
    return False


class InvoiceAdjustmentSave(LoginRequiredMixin, View):
    """Save, change or delete one adjustment row as the admin edits it on a draft invoice.

    Each row posts on its own, so the total is recalculated and the page never reloads. The response
    carries the saved row's pk back to the page and an ``adjustmentSaved`` event for the totals.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        invoice = get_object_or_404(Invoice, pk=pk)
        if not _can_adjust_invoice(request.user, invoice):
            raise PermissionDenied()
        row = request.POST.get("row", "")
        if not ADJUSTMENT_ROW_KEY.fullmatch(row):
            return HttpResponseBadRequest("Bad row")
        if invoice.status != "DRAFT":
            return HttpResponseBadRequest("Only a draft invoice can be adjusted")
        auction = invoice.auction or (invoice.auctiontos_user.auction if invoice.auctiontos_user else None)
        name = invoice.auctiontos_user.name if invoice.auctiontos_user else str(invoice)

        adjustment_id = request.POST.get("adjustment", "")
        if adjustment_id and not adjustment_id.isdigit():
            return HttpResponseBadRequest("Bad adjustment")
        existing = (
            InvoiceAdjustment.objects.filter(pk=adjustment_id, invoice=invoice).first() if adjustment_id else None
        )
        if adjustment_id and existing is None:
            return HttpResponseBadRequest("Bad adjustment")

        if request.POST.get("delete"):
            if existing is not None:
                existing.delete()
                if auction:
                    auction.create_history(
                        applies_to="INVOICES",
                        action=f"Removed an adjustment from invoice for {name}",
                        user=request.user,
                    )
            invoice.recalculate()
            response = HttpResponse("")
            response["HX-Trigger"] = "adjustmentSaved"
            return response

        form = InvoiceAdjustmentForm(request.POST, instance=existing, invoice=invoice)
        form.fields["notes"].required = False
        if not form.is_valid():
            if not existing and not request.POST.get("amount"):
                # A new row with no amount yet: nothing to save, and nothing to report.
                return HttpResponse(status=204)
            errors = [error for errors in form.errors.values() for error in errors]
            return HttpResponse(errors[0] if errors else "That did not save.", status=422)

        adjustment = form.save(commit=False)
        adjustment.invoice = invoice
        adjustment.user = request.user
        adjustment.save()
        if auction:
            auction.create_history(
                applies_to="INVOICES",
                action=f"Adjusted invoice for {name}",
                user=request.user,
            )
        invoice.recalculate()
        response = HttpResponse(
            format_html(
                '<input type="hidden" name="adjustment" id="adj-pk-{}" value="{}" hx-swap-oob="true">',
                row,
                adjustment.pk,
            )
        )
        response["HX-Trigger"] = "adjustmentSaved"
        return response


class InvoiceView(DetailView, FormMixin, AuctionViewMixin):
    """Show a single invoice"""

    template_name = "invoice.html"
    model = Invoice
    # Set to true when the user the invoice is for opens it.
    form_view = "opened"
    allow_non_admins = True
    authorized_by_default = False
    using_no_login_link = False

    def get_object(self):
        """The invoice, fetched once.

        dispatch, get and get_context_data all ask for it, and each was its own query *and* its own Invoice
        instance -- so the whole cached number tree was derived again for each copy.
        """
        if getattr(self, "object", None) is not None:
            return self.object
        try:
            self.object = Invoice.objects.select_related(
                "auction__created_by__userdata",
                "auctiontos_user__pickup_location",
                "auctiontos_user__user",
                "club",
                "club_member",
            ).get(pk=self.kwargs.get(self.pk_url_kwarg))
        except Invoice.DoesNotExist:
            self.object = None
            # Only the /auctions/<slug>/invoice/ route has a slug; /invoices/<pk>/ has none.
            if self.request.user.is_authenticated and self.kwargs.get("slug"):
                self.object = Invoice.objects.filter(
                    auctiontos_user__user=self.request.user,
                    auction__slug=self.kwargs["slug"],
                ).first()
        return self.object

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        auth = self.authorized_by_default
        self.is_admin = False
        invoice = self.get_object()
        if not invoice:
            slug = self.kwargs.get("slug")
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=slug).first() if slug else None
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
            if (
                invoice.club
                and invoice.buyer == request.user
                or invoice.auctiontos_user
                and invoice.auctiontos_user.user_id == request.user.pk
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
            # The one column: a full save here wrote a copy loaded before Square's webhook marked the
            # invoice paid back over PAID, on the page Square's success redirect lands on.
            invoice.save(update_fields=[self.form_view])
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
        context["adjustments"] = invoice.adjustments
        context["print_label_link"] = None
        if (
            invoice.auction
            and invoice.auctiontos_user
            and invoice.auction.is_online
            and invoice.auctiontos_user.print_labels_qs.exists()
        ):
            context["print_label_link"] = reverse(
                "print_labels_by_bidder_number",
                kwargs={
                    "slug": invoice.auction.slug,
                    "bidder_number": invoice.auctiontos_user.bidder_number,
                },
            )
        context["is_auction_admin"] = self.auction and self.is_auction_admin
        # Scanned at the pickup table on the logistics page (logistics.scan), so only around the pickups.
        tos = invoice.auctiontos_user
        if (
            invoice.auction
            and invoice.auction.is_online
            and tos
            and tos.pickup_location
            and not tos.pickup_location.pickup_by_mail
            and logistics.shows_pickup_code(invoice.auction)
        ):
            context["pickup_qr_code"] = (
                f"https://{Site.objects.get_current().domain}{reverse('invoice_by_pk', kwargs={'pk': invoice.pk})}"
            )
        is_owner = self.using_no_login_link or (
            invoice.auctiontos_user is not None and invoice.auctiontos_user.user_id == self.request.user.pk
        )
        # The survey on the page too: app users and people who skip the email see it here.
        if context["show_links"] and is_owner and auction_survey.on_invoice_page(invoice):
            context["survey_answers"] = AuctionTOS.SURVEY_ANSWERS
        context["website_focus"] = settings.WEBSITE_FOCUS
        club = invoice.auction.club if invoice.auction else None
        context["viewer_has_bap"] = club is not None and check_club_permission(
            self.request.user, club, "permission_manage_bap"
        )
        if context["viewer_has_bap"] and club:
            # Blank rather than None when the club has no flat rate: the value goes into a text box,
            # and the per-lot category rate isn't worth a query per row.
            context["bap_default_points"] = "" if club.points_per_lot is None else club.points_per_lot
        return context

    def get_success_url(self):
        return self.request.path

    def get(self, request, *args, **kwargs):
        if self.get_object().unsold_lot_warning and self.auction and self.is_auction_admin:
            messages.info(
                self.request,
                "This user still has unsold lots, make sure to sell all non-donation lots before marking this ready or paid.",
            )
        if self.auction and self.is_auction_admin:
            hint = self.get_object().square_payment_hint
            if hint:
                messages.info(self.request, hint)
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        # Recalculating slows this down; it may not belong here.
        self.object.recalculate()
        return self.render_to_response(context)


class InvoiceNoLoginView(InvoiceView):
    """Enter a uuid, go to your invoice.  This bypasses the login checks"""

    private_page = True

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
        invoice.mark_opened()
        if invoice.auctiontos_user:
            invoice.auctiontos_user.email_address_status = "VALID"
            invoice.auctiontos_user.save(update_fields=["email_address_status"])
        if invoice.club and not invoice.auction:
            return render(
                request,
                "auctions/club_membership_payment.html",
                {"club": invoice.club, "invoice": invoice},
            )
        return super().dispatch(request, *args, **kwargs)


class SquarePaymentSuccessView(InvoiceNoLoginView):
    """Square's success redirect: marks the invoice opened but does not verify the email address, since
    people scan these QR codes.
    """

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get("uuid", None)
        invoice = self.get_object()
        # Opened, but the email isn't verified: people scan these QR codes.
        invoice.mark_opened()
        # Skip the parent's dispatch, which marks the email VALID, and call InvoiceView's.
        return InvoiceView.dispatch(self, request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_payment_button"] = True
        return context
