"""The bulk buttons on the auction admin pages: mark paid, set won, enable bidding.

Each one takes a set of rows and applies one change to all of them. They are the only writes on the
site that touch many rows at once, which is exactly why none of them is exposed as an assistant
skill -- see the "no tool changes more than one row" rule in the MCP notes.
"""

import logging
from datetime import timedelta
from urllib.parse import unquote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import (
    FormMixin,
)
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.filters import (
    LotAdminFilter,
)
from auctions.forms import (
    BulkSellLotsToOnlineHighBidder,
    ChangeInvoiceStatusForm,
    EnableBiddingForAllForm,
    LotRefundForm,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Club,
    ClubMember,
    Invoice,
    Lot,
    add_price_info,
)
from auctions.tasks import (
    cancel_invoice_notification,
    schedule_invoice_notification,
)

from .base import (
    INVOICE_NOTIFICATION_DELAY_SECONDS,
    AuctionViewMixin,
    _ensure_invoice_renewal_state,
    _process_invoice_membership_renewal,
    close_modal_response,
)

logger = logging.getLogger(__name__)


class GetClubs(APIView):
    """Used for autocomplete on the contact info page"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        search = request.POST["search"]
        result = Club.objects.filter(Q(name__icontains=search) | Q(abbreviation__icontains=search)).values(
            "id", "name", "abbreviation"
        )
        return JsonResponse(list(result), safe=False)


class BulkSetLotsWon(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Sell all lots based on the current filter to online high bidder"""

    template_name = "auctions/generic_admin_form.html"
    form_class = BulkSellLotsToOnlineHighBidder

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.original_query = request.GET.get("query", "")
        if not self.original_query:
            self.original_query = request.POST.get("query", "")
        self.query = unquote(self.original_query)
        self.queryset = LotAdminFilter.generic(self, self.auction.lots_qs, self.query)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            for lot in self.queryset:
                try:
                    lot.sell_to_online_high_bidder
                except Exception:
                    logger.exception("sell_to_online_high_bidder failed for lot %s", lot.pk)
                    continue
                if lot.auctiontos_winner:
                    try:
                        lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
                    except Exception:
                        logger.exception("add_winner_message failed for lot %s", lot.pk)
            try:
                self.auction.create_history(
                    applies_to="LOTS",
                    action=f"Sold {self.queryset.count()} lots to online high bidder",
                    user=request.user,
                )
            except Exception:
                logger.exception("create_history failed for auction %s", self.auction.pk)
            return close_modal_response("reload-page")
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tooltip = "This is intended to be used with silent auctions where people place bids on their phones, or with hybrid online auctions where some lots will be sold ahead of time.  It will sell any lots with online bids to the current online high bidder."
        if not self.query:
            tooltip += "<br><br><span class='text-warning'>You are about to set the winners of all lots.  This is a bad idea, you should click on cancel and then type in a filter first.</span>"
        else:
            tooltip += f"<br><br>You are about to set the winners of {self.queryset.count()} lots that match the filter <span class='text-warning'>{self.query}</span>"
        context["tooltip"] = tooltip
        context["modal_title"] = "Sell lots to online high bidders"
        return context

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["query"] = self.query
        form_kwargs["auction"] = self.auction
        form_kwargs["queryset"] = self.queryset
        return form_kwargs


class InvoiceBulkUpdateStatus(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Change invoice statuses in bulk"""

    template_name = "auctions/generic_admin_form.html"
    form_class = ChangeInvoiceStatusForm
    show_checkbox = False

    def get_queryset(self):
        return Invoice.objects.filter(auctiontos_user__auction=self.auction, status=self.old_invoice_status)

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.invoice_count = self.get_queryset().count()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["invoice_count"] = self.invoice_count
        if self.invoice_count:
            form_kwargs["show_checkbox"] = self.show_checkbox
        else:
            form_kwargs["show_checkbox"] = False
        form_kwargs["post_target_url"] = "auction_invoices_" + self.new_status_display.lower()
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

    def post(self, request, *args, **kwargs):
        invoices = self.get_queryset()
        run_at = None
        # Set or clear invoice_notification_due based on new status
        if self.new_invoice_status in ("UNPAID", "PAID"):
            run_at = timezone.now() + timedelta(seconds=INVOICE_NOTIFICATION_DELAY_SECONDS)
        for invoice in invoices:
            # Core: change the status and save. Extras follow, each guarded.
            if self.new_invoice_status in ("PAID", "UNPAID") and not invoice.renewal_needed:
                try:
                    _ensure_invoice_renewal_state(invoice)
                except Exception:
                    logger.exception("Failed to ensure renewal state for invoice %s in bulk", invoice.pk)
            try:
                invoice.status = self.new_invoice_status
                invoice.invoice_notification_due = run_at
                invoice.save()
            except Exception:
                logger.exception("Failed to update invoice %s to %s in bulk", invoice.pk, self.new_invoice_status)
                continue
            try:
                invoice.recalculate()
            except Exception:
                logger.exception("recalculate failed for invoice %s in bulk", invoice.pk)
            if self.new_invoice_status == "PAID":
                try:
                    _process_invoice_membership_renewal(invoice, acting_user=request.user)
                except Exception:
                    logger.exception("membership renewal failed for invoice %s in bulk", invoice.pk)
            try:
                if run_at:
                    schedule_invoice_notification(invoice.pk, run_at)
                else:
                    cancel_invoice_notification(invoice.pk)
            except Exception:
                logger.exception("schedule/cancel notification failed for invoice %s in bulk", invoice.pk)
        action = f"Set {invoices.count()} invoices from {self.old_status_display} to {self.new_status_display}"
        try:
            self.auction.create_history(
                applies_to="INVOICES",
                action=action,
                user=request.user,
            )
        except Exception:
            logger.exception("create_history failed for bulk invoice update on auction %s", self.auction.pk)
        if self.invoice_count:
            noun = "invoice" if self.invoice_count == 1 else "invoices"
            messages.success(request, f"{self.invoice_count} {noun} marked {self.new_status_display}.")
        return close_modal_response("reload-page")


class MarkInvoicesReady(InvoiceBulkUpdateStatus):
    old_invoice_status = "DRAFT"
    new_invoice_status = "UNPAID"
    old_status_display = "open"
    new_status_display = "ready"

    show_checkbox = True

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.auction.email_users_when_invoices_ready = form.cleaned_data.get(
                "send_invoice_ready_notification_emails"
            )
            self.auction.save()
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = (
            f"Changing the invoice status to ready will block users from bidding.  You should make any needed adjustments before setting invoices to ready.  This will change the status of {self.invoice_count} invoices, and allow you to export them to PayPal."
        )
        if not self.auction.closed and self.auction.is_online:
            context["unsold_lot_warning"] = "Don't set invoices ready yet!  This auction hasn't ended."
            if not self.auction.minutes_to_end:
                active_lot_count = self.auction.lots_qs.filter(active=True).count()
                context["unsold_lot_warning"] += (
                    f" There are still {active_lot_count} lots with last-minute bids on them"
                )
        if not self.auction.is_online:
            context["unsold_lot_warning"] = (
                "You usually don't need to use this.  Set people's invoices to paid one at a time, as people leave the auction."
            )
        if not self.invoice_count:
            context["modal_title"] = "No open invoices"
            context["tooltip"] = (
                "There aren't any open invoices in this auction.  Invoices are created automatically whenever a user buys or sells a lot."
            )
        else:
            context["modal_title"] = f"Set {self.invoice_count} open invoices to ready"
        return context


class MarkInvoicesPaid(InvoiceBulkUpdateStatus):
    old_invoice_status = "UNPAID"
    new_invoice_status = "PAID"
    old_status_display = "ready"
    new_status_display = "paid"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        unchanged_invoices = Invoice.objects.filter(auctiontos_user__auction=self.auction, status="DRAFT").count()
        context["unsold_lot_warning"] = (
            "You probably don't need this.  Instead, set invoices to paid one at a time, as users pay them."
        )
        context["tooltip"] = ""
        if unchanged_invoices:
            context["tooltip"] += (
                f" There are {unchanged_invoices} open invoices in this auction that will not be changed; set them ready first if you want to change them."
            )
        context["tooltip"] += f" This will set {self.invoice_count} ready invoices to paid."
        if not self.invoice_count:
            context["modal_title"] = "No ready invoices"
            if unchanged_invoices:
                context["tooltip"] = (
                    f"There's {unchanged_invoices} invoices that are still open.  You should set open invoices to ready before using this."
                )
        else:
            context["modal_title"] = f"Set {self.invoice_count} ready invoices to paid"
        return context


class EnableBiddingForAllUsers(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Turn bidding back on for every participant in this auction who currently can't bid.

    The repair for a whole auction that lost bidding at once -- a CSV import that read a blank permission
    column as "no", or "only approved bidders" being switched off after people had already joined (which
    does not retroactively enable anyone). Without this an admin has to open every user's modal in turn.
    """

    template_name = "auctions/generic_admin_form.html"
    form_class = EnableBiddingForAllForm

    def get_queryset(self):
        return AuctionTOS.objects.filter(auction=self.auction, bidding_allowed=False)

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        if self.auction.use_check_in_mode:
            # Bidding is meant to be off until each person checks in; enabling everyone would skip it.
            raise Http404
        self.user_count = self.get_queryset().count()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["user_count"] = self.user_count
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.user_count:
            context["modal_title"] = "Everyone can already bid"
            context["tooltip"] = "There aren't any users in this auction with bidding disabled."
            return context
        noun = "user" if self.user_count == 1 else "users"
        context["modal_title"] = f"Enable bidding for {self.user_count} {noun}?"
        context["tooltip"] = (
            f"This will let all {self.user_count} {noun} who currently have bidding disabled place bids. "
            "This cannot be undone -- if some of them were blocked on purpose, you'll have to disable them "
            "again one at a time."
        )
        return context

    def post(self, request, *args, **kwargs):
        if self.user_count:
            tos_qs = self.get_queryset()
            # The participant rows in a club-managed auction are shadows of the club's member records;
            # leaving the club side saying "no" would show two different answers on two pages, and a
            # later member edit would push the stale value back down (signals.propagate_clubmember_to_
            # shadow_tos). Collect the ids before the update, which empties this queryset.
            member_pks = (
                [pk for pk in tos_qs.values_list("clubmember_id", flat=True) if pk]
                if self.auction.is_club_managed
                else []
            )
            tos_qs.update(bidding_allowed=True)
            if member_pks:
                ClubMember.objects.filter(pk__in=member_pks).update(bidding_allowed=True)
            noun = "user" if self.user_count == 1 else "users"
            self.auction.create_history(
                applies_to="USERS",
                action=f"Enabled bidding for {self.user_count} {noun}",
                user=request.user,
            )
            messages.success(request, f"{self.user_count} {noun} can now bid.")
        return close_modal_response("reload-page")


class LotRefundDialog(LoginRequiredMixin, DetailView, FormMixin, AuctionViewMixin):
    model = Lot
    template_name = "auctions/generic_admin_form.html"
    form_class = LotRefundForm
    winner_invoice = None
    seller_invoice = None

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            self.lot.auction.create_history(
                applies_to="LOTS",
                action=f"Removed/refunded lot {self.lot.lot_number_display}",
                user=request.user,
            )
            refund = form.cleaned_data["partial_refund_percent"] or 0
            self.lot.refund(refund, request.user)
            banned = form.cleaned_data["banned"]
            self.lot.remove(banned, request.user)
            if self.seller_invoice:
                self.seller_invoice.recalculate()
            if self.winner_invoice:
                self.winner_invoice.recalculate()
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)

    def dispatch(self, request, *args, **kwargs):
        pk = self.kwargs.get(self.pk_url_kwarg)
        self.lot = get_object_or_404(
            Lot,
            is_deleted=False,
            auction__isnull=False,
            auctiontos_seller__isnull=False,
            pk=pk,
        )
        self.object = self.lot
        self.auction = self.lot.auction
        self.is_auction_admin
        self.seller_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_seller).first()
        if self.lot.auctiontos_winner:
            self.winner_invoice = Invoice.objects.filter(auctiontos_user=self.lot.auctiontos_winner).first()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["lot"] = self.lot
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["lot"] = self.lot
        if not self.lot.sold:
            context["tooltip"] = (
                "This lot has not sold, there's nothing to refund.  If there's a problem with this lot, remove it."
            )
        else:
            # if a refund has already been issued for this lot, we need to calculate how much is unpaid by temporarily removing it
            existing_refund = self.lot.partial_refund_percent
            if existing_refund:
                self.lot.partial_refund_percent = 0
                self.lot.save()
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                # if we removed a refund before for math purposes, put it back now
                self.lot.partial_refund_percent = existing_refund
                self.lot.save()
                tooltip = "A refund has already been issued for this lot.  The refund percent is based on the original sale price.<br><br>"
            else:
                full_seller_refund = add_price_info(Lot.objects.filter(pk=self.lot.pk)).first().your_cut
                tooltip = "<small>This lot has sold.  If there's a problem with it, you should issue a refund which will show up on the seller and winner's invoices.</small><br><br>"
            if self.lot.winning_price:
                full_buyer_refund = self.lot.winning_price + (self.lot.winning_price * self.lot.auction.tax / 100)
            else:
                full_buyer_refund = 0
            if self.seller_invoice and self.seller_invoice.status == "DRAFT":
                tooltip += (
                    "Seller's invoice is open; $<span id='seller_refund'></span> will automatically be removed.<br>"
                )
            else:
                tooltip += "Seller's invoice is not open.  <span class='text-warning'>Collect $<span id='seller_refund'></span> from the seller</span><br>"
            if self.lot.winning_price and self.lot.auctiontos_winner:
                if self.winner_invoice and self.winner_invoice.status == "DRAFT":
                    tooltip += "Winner's invoice is open; refund of $<span id='buyer_refund'></span> will automatically be added<br>"
                else:
                    tooltip += "Winner's invoice is not open.  <span class='text-warning'>Refund the winner $<span id='buyer_refund'></span></span>"
                    if self.lot.auction.tax:
                        tooltip += f"<small> (includes {self.lot.auction.tax}% tax)</small><br>"
                    else:
                        tooltip += "<br>"
            tooltip += "<br><br>"
            extra_script = """
            <script>$('#id_partial_refund_percent').on('change keyup', function(){recalculate()});
            function recalculate(){
                var refund = $('#id_partial_refund_percent').val();var tax = """
            extra_script += f"{self.lot.auction.tax};var full_seller_refund = {full_seller_refund};var full_buyer_refund = {full_buyer_refund};"
            extra_script += """
                $('#seller_refund').text((full_seller_refund*refund/100).toFixed(2));
                if (full_buyer_refund) {
                    $('#buyer_refund').text((full_buyer_refund*refund/100).toFixed(2));
                };
            }
            $(document).ready( function(){recalculate()});
            </script>
            """
            context["extra_script"] = mark_safe(extra_script)
            context["tooltip"] = mark_safe(tooltip)
        context["modal_title"] = f"Remove or refund lot {self.lot.lot_number_display}"
        return context
