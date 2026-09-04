"""Invoices as a person reads them: the list, one invoice, and the no-login link.

``InvoiceView`` is the page a buyer or seller is sent to after an auction. ``InvoiceNoLoginView`` is
the same page reached by a signed link, which is how someone who never made an account pays.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.forms import modelformset_factory
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.generic import DetailView, View
from django.views.generic.edit import (
    FormMixin,
)

from auctions.filters import (
    InvoiceFilter,
)
from auctions.forms import (
    InvoiceAdjustmentForm,
    InvoiceAdjustmentFormSetHelper,
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


class Invoices(LoginRequiredMixin, HTMxTableView):
    """Get all invoices for the current user"""

    model = Invoice
    table_class = InvoiceHTMxTable
    filterset_class = InvoiceFilter
    template_name = "all_invoices.html"
    filter_placeholder_text = "Search your invoices"

    def get_queryset(self):
        """Newest first.

        The table's own default sort (`InvoiceHTMxTable.Meta.order_by`) is the one that
        actually decides what the page opens on; this order_by keeps the queryset itself
        sensible for anything reading it without the table.
        """
        return (
            Invoice.objects.filter(
                Q(auctiontos_user__user=self.request.user) | Q(auctiontos_user__email=self.request.user.email)
            )
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
    """Create a new invoice for a user in an auction"""

    def get(self, request, *args, **kwargs):
        """Create invoice and redirect to invoice detail page"""
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

        # Check for existing invoices - get the oldest one (first created)
        existing_invoice = (
            Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).order_by("date").first()
        )

        if existing_invoice:
            # Check for and delete any duplicate invoices (keep the oldest)
            duplicate_invoices = Invoice.objects.filter(auctiontos_user=auctiontos, auction=auctiontos.auction).exclude(
                pk=existing_invoice.pk
            )

            duplicate_count = duplicate_invoices.count()
            if duplicate_count > 0:
                duplicate_invoices.delete()
                messages.info(request, f"Removed {duplicate_count} duplicate invoice(s)")

            # Redirect to existing invoice
            messages.info(request, "Invoice already exists for this user")
            return redirect(existing_invoice.get_absolute_url())

        # Create new invoice
        invoice = Invoice.objects.create(auctiontos_user=auctiontos, auction=auctiontos.auction)
        invoice.recalculate()

        messages.success(request, f"Invoice created for {auctiontos.name}")
        return redirect(invoice.get_absolute_url())


class InvoiceView(DetailView, FormMixin, AuctionViewMixin):
    """Show a single invoice"""

    template_name = "invoice.html"
    model = Invoice
    # form_class = InvoiceUpdateForm
    # expects opened or printed, this field will be set to true when the user the invoice is for opens it
    form_view = "opened"
    allow_non_admins = True
    authorized_by_default = False
    using_no_login_link = False

    def get_object(self):
        """The invoice, fetched once.

        dispatch, get and get_context_data all ask for it, and every one of those was its own
        query *and* its own Invoice instance -- so the whole cached number tree (net, subtotal,
        tax, the adjustment totals) was derived again for each copy. Memoized on the view.
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
            if self.request.user.is_authenticated:
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
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.kwargs["slug"]).first()
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
                and (
                    invoice.auctiontos_user.email == request.user.email or invoice.auctiontos_user.user == request.user
                )
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
            invoice.save()
        self.InvoiceAdjustmentFormSet = modelformset_factory(
            InvoiceAdjustment, extra=1, can_delete=True, form=InvoiceAdjustmentForm
        )
        self.queryset = invoice.adjustments
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
        context["print_label_link"] = None
        if invoice.auction and invoice.auctiontos_user and invoice.auction.is_online:
            context["print_label_link"] = reverse(
                "print_labels_by_bidder_number",
                kwargs={
                    "slug": invoice.auction.slug,
                    "bidder_number": invoice.auctiontos_user.bidder_number,
                },
            )
        context["is_auction_admin"] = self.auction and self.is_auction_admin
        context["website_focus"] = settings.WEBSITE_FOCUS
        club = invoice.auction.club if invoice.auction else None
        context["viewer_has_bap"] = club is not None and check_club_permission(
            self.request.user, club, "permission_manage_bap"
        )
        if context["viewer_has_bap"] and club:
            # Blank rather than None when the club has no flat rate: the value goes straight into a
            # text box, and the per-lot category rate isn't worth a query per row on an invoice.
            context["bap_default_points"] = "" if club.points_per_lot is None else club.points_per_lot
        return context

    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        adjustment_formset = self.InvoiceAdjustmentFormSet(
            self.request.POST,
            form_kwargs={"invoice": self.get_object()},
            queryset=self.queryset,
        )
        if adjustment_formset.is_valid() and self.is_admin:
            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.invoice = self.get_object()
                adjustment.user = request.user
                adjustment.save()
            if adjustments:
                messages.success(self.request, "Invoice adjusted")
                if self.auction:
                    self.auction.create_history(
                        applies_to="INVOICES",
                        action=f"Adjusted invoice for {self.get_object().auctiontos_user.name if self.get_object().auctiontos_user else self.get_object()}",
                        user=request.user,
                    )
            for form in adjustment_formset.deleted_forms:
                if form.instance.pk:
                    form.instance.delete()
            return redirect(reverse("invoice_by_pk", kwargs={"pk": self.get_object().pk}))
        context = self.get_context_data(**kwargs)
        context["formset"] = adjustment_formset
        context["helper"] = InvoiceAdjustmentFormSetHelper()
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        if self.get_object().unsold_lot_warning and self.auction and self.is_auction_admin:
            messages.info(
                self.request,
                "This user still has unsold lots, make sure to sell all non-donation lots before marking this ready or paid.",
            )
        self.object = self.get_object()
        invoice_adjustment_formset = self.InvoiceAdjustmentFormSet(
            form_kwargs={"invoice": self.get_object()}, queryset=self.queryset
        )
        helper = InvoiceAdjustmentFormSetHelper()
        context = self.get_context_data(object=self.object)
        context["formset"] = invoice_adjustment_formset
        context["helper"] = helper
        # recaluclating slows things down,
        # I am not sure if it's a good idea to have it here or not
        self.object.recalculate()
        return self.render_to_response(context)


class InvoiceNoLoginView(InvoiceView):
    """Enter a uuid, go to your invoice.  This bypasses the login checks"""

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
        invoice.opened = True
        invoice.save()
        if invoice.auctiontos_user:
            invoice.auctiontos_user.email_address_status = "VALID"
            invoice.auctiontos_user.save()
        if invoice.club and not invoice.auction:
            return render(
                request,
                "auctions/club_membership_payment.html",
                {"club": invoice.club, "invoice": invoice},
            )
        return super().dispatch(request, *args, **kwargs)


class SquarePaymentSuccessView(InvoiceNoLoginView):
    """
    Success redirect for Square payment links.
    Marks invoice as opened but does NOT verify email address.
    This prevents incorrectly marking emails as valid when users scan QR codes.
    """

    def dispatch(self, request, *args, **kwargs):
        self.uuid = kwargs.get("uuid", None)
        invoice = self.get_object()
        # Mark invoice as opened but don't verify email
        invoice.opened = True
        invoice.save()
        # Skip the parent's dispatch which marks email as VALID
        # Call grandparent (InvoiceView) dispatch instead
        return InvoiceView.dispatch(self, request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_payment_button"] = True
        return context
