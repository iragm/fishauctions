"""Auction night: setting winners, the lot queue, and volunteers.

``DynamicSetLotWinner`` is the auctioneer's page, driven by :mod:`auctions.voice`. The queue decides
which lot is on the block, and its watchers are notified as the queue moves, not as winners are set.
"""

import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import channels.layers
import requests
from asgiref.sync import async_to_sync
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import (
    Exists,
    OuterRef,
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View
from pywebpush import WebPushException
from webpush import send_user_notification
from webpush.models import PushInformation

from auctions import voice
from auctions.bidding import MAX_BID
from auctions.forms import (
    VolunteerJobForm,
)
from auctions.models import (
    AuctionHistory,
    AuctionTOS,
    ClubMember,
    Invoice,
    InvoiceAdjustment,
    Lot,
    LotQueueEntry,
    MobileDevice,
    VoiceGrammar,
    VolunteerJob,
    VolunteerSignup,
    Watch,
)
from auctions.notifications import CATEGORY_LOT_SELLING, notify_running_total, user_has_app_push
from auctions.tasks import (
    send_push_to_user,
)

from .base import (
    AuctionViewMixin,
    _lot_invoices,
    _recalculate_invoices,
    _upsert_clubmember_shadow_tos,
    close_modal_response,
)

logger = logging.getLogger(__name__)


class DynamicSetLotWinner(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """A form to set lot winners.  Totally async with no page loads, just POST"""

    template_name = "auctions/dynamic_set_lot_winner.html"
    club_sidebar_can_view = False  # full-screen tool; sidebar would waste space

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # Prefill the lot from the in-person queue.
        next_lot = queue_next_to_record(self.auction)
        context["queue_head_lot_number"] = next_lot.lot_number_display if next_lot else ""
        # Voice: the app listens through its own recognizer, and through OpenAI when that's on, as a browser does.
        grammar = VoiceGrammar.load()
        user = self.request.user
        if getattr(self.request, "is_mobile_app", False) or voice.cloud_model(grammar, user):
            context["voice_config"] = voice.page_config(self.auction, user, grammar or VoiceGrammar())
        return context

    def advance_queue_and_set_next(self, lot, result, double_check=False):
        """Tell the queue ``lot`` was recorded and set ``result["next_queued_lot_number"]`` (None when there
        isn't one) so the page auto-advances.
        """
        queue_lot_recorded(self.auction, lot)
        next_lot = queue_next_to_record(self.auction, after_lot=lot, include_recorded=double_check)
        result["next_queued_lot_number"] = next_lot.lot_number_display if next_lot else None

    def validate_lot(self, lot, action):
        """Returns (Lot or None, error or None)"""
        error = None
        result_lot = None
        if not lot and action != "validate":
            error = "Enter a lot number"
        else:
            # Can't search by custom_lot_number with seller-dash numbering; revisit if custom numbers return.
            result_lot_qs = Lot.objects.none()
            if self.auction.use_seller_dash_lot_numbering:
                result_lot_qs = self.auction.lots_qs.filter(custom_lot_number=lot)
            else:
                try:
                    lot = int(lot)
                except ValueError:
                    error = "Lot number must be a number"
                if not error and lot:
                    result_lot_qs = self.auction.lots_qs.filter(lot_number_int=lot)
                if error and not lot and action == "validate":
                    error = ""
            # Two lots submitted in the same instant; unlikely but cheap to catch.
            if result_lot_qs.count() > 1:
                error = "Multiple lots with this lot number.  Go to the lot's page and set the winner there."
            else:
                result_lot = result_lot_qs.first()
            if not result_lot and lot and not error:
                error = "No lot found"
        if (
            result_lot
            and result_lot.auctiontos_seller
            and result_lot.auctiontos_seller.invoice
            and result_lot.auctiontos_seller.invoice.status != "DRAFT"
        ):
            if action != "force_save":
                error = "The seller's invoice is not open"
        if result_lot and result_lot.auctiontos_winner and result_lot.winning_price and action != "force_save":
            error = "This lot has already been sold"
        return result_lot, error

    def validate_price(self, price, action):
        """Returns (Decimal or None, error or None)"""
        result_price = None
        error = None
        try:
            result_price = Decimal(str(price)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            if action == "save":
                error = "Enter the winning price"
            if action == "force_save":
                error = "You can skip some errors, but you still need to enter a price"
        # "NaN" quantizes without complaint and then raises on the first comparison; a negative or
        # enormous price can't be skipped past with force_save either, since it can't be invoiced.
        if result_price is not None and (not result_price.is_finite() or result_price < 0 or result_price > MAX_BID):
            error = f"Enter a price between $0 and ${MAX_BID:,.2f}"
            result_price = None
        if result_price is not None and self.auction.only_whole_dollar_bids:
            if result_price != result_price.to_integral_value():
                error = "This auction only allows whole dollar amounts"
                result_price = None
        return result_price, error

    def validate_winner(self, winner, action):
        """Returns (AuctionTOS or None, error or None)"""
        error = None
        tos = None
        if not winner and (action == "force_save" or action == "save"):
            error = "Enter the winning bidder's number"
        else:
            tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=winner).order_by("-createdon").first()
            if not tos and winner and self.auction.is_club_managed:
                # Club-managed: the member owns the bidder number; ensure a shadow TOS.
                cm = ClubMember.objects.filter(club=self.auction.club, bidder_number=winner, is_deleted=False).first()
                if cm:
                    tos = _upsert_clubmember_shadow_tos(
                        self.auction,
                        cm,
                        bidding_allowed=cm.bidding_allowed,
                        selling_allowed=cm.selling_allowed,
                    )
            if not tos and winner:
                error = "No bidder found"
            else:
                if tos and tos.invoice and tos.invoice.status != "DRAFT" and action != "force_save":
                    error = "This user's invoice is not open"
                if tos and tos.requires_check_in_before_bidding and action != "force_save":
                    error = "This bidder has not been checked in yet"
        return tos, error

    def end_unsold(self, lot):
        """Mark lot unsold"""
        return lot.end_unsold(self.request.user)

    def set_winner(self, lot, winning_tos, winning_price):
        # A force_save over an earlier sale takes the lot off the old winner's invoice.
        stale_invoices = _lot_invoices(lot)
        lot.auctiontos_winner = winning_tos
        lot.winning_price = winning_price
        lot.date_end = timezone.now()
        lot.active = False
        lot.save()
        if (
            lot.auction
            and lot.auction.use_check_in_mode
            and lot.auctiontos_seller
            and not lot.auctiontos_seller.checked_in
        ):
            seller = lot.auctiontos_seller
            seller.checked_in = timezone.now()
            update_fields = ["checked_in"]
            if not seller.bidding_allowed:
                seller.bidding_allowed = True
                update_fields.append("bidding_allowed")
            seller.save(update_fields=update_fields)
            lot.auction.create_history(
                applies_to="USERS",
                action=f"Checked in {seller.name} (lot sold)",
                user=self.request.user,
            )
        try:
            lot.add_winner_message(self.request.user, winning_tos, winning_price)
        except Exception:
            logger.exception("add_winner_message failed for lot %s", lot.pk)
        try:
            _recalculate_invoices(stale_invoices)
        except Exception:
            logger.exception("Recalculating the previous invoices failed for lot %s", lot.pk)
        # After add_winner_message, which creates the invoice this total reads.
        try:
            notify_running_total(lot)
        except Exception:
            logger.exception("notify_running_total failed for lot %s", lot.pk)
        if lot.auction and lot.auction.club and not lot.bap_points_awarded and not lot.manually_approved:
            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
        return f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.lot_number_display}"

    def cross_check_price_and_winner(self, lot, price, winner, action, lot_error, price_error, winner_error):
        """Price and winner checks needing both resolved. Shared with the palette's ``set_lot_winner``.
        Returns ``(price_error, winner_error)``.
        """
        if (
            not price_error
            and lot
            and winner
            and lot.high_bidder
            and lot.auction.online_bidding == "allow"
            and action != "force_save"
        ):
            if price and price <= lot.max_bid and f"{winner}" != f"{lot.high_bidder_for_admins}":
                price_error = "Lower than an online bid"
                winner_error = f"Bidder {lot.high_bidder_for_admins} has bid more than this"
        if not price_error and price and lot and not lot_error and action != "force_save":
            if lot.reserve_price and price < lot.reserve_price:
                price_error = f"This lot's minimum bid is ${lot.reserve_price}"
            if price < self.auction.minimum_bid:
                price_error = f"Minimum bid is ${self.auction.minimum_bid}"
        return price_error, winner_error

    def commit_winner(self, lot, winner, price, action, result):
        """Record the sale: winner, check-in on force_save, history, queue advance. Shared with the palette."""
        result["success_message"] = self.set_winner(lot, winner, price)
        if action == "force_save" and lot.auction and lot.auction.use_check_in_mode and not winner.checked_in:
            winner.checked_in = timezone.now()
            update_fields = ["checked_in"]
            if not winner.bidding_allowed:
                winner.bidding_allowed = True
                update_fields.append("bidding_allowed")
            winner.save(update_fields=update_fields)
            lot.auction.create_history(
                applies_to="USERS",
                action=f"Checked in {winner.name} (ignored errors, lot sold)",
                user=self.request.user,
            )
        try:
            lot.auction.create_history(
                applies_to="LOTS",
                action=f"{'Ignored errors and set ' if action == 'force_save' else 'Set'} lot {lot.lot_number_display} as sold",
                user=self.request.user,
            )
        except Exception:
            logger.exception("create_history failed for lot %s", lot.pk)
        self.advance_queue_and_set_next(lot, result)
        return result

    def post(self, request, *args, **kwargs):
        """All lot validation checks called from here"""
        lot = request.POST.get("lot", None)
        price = request.POST.get("price", None)
        winner = request.POST.get("winner", None)
        action = request.POST.get("action", "validate")
        # A price of 0 and no winner is how the form says "unsold".
        if action in ("save", "force_save") and not winner:
            try:
                if Decimal(str(price)) == 0:
                    action = "end_unsold"
            except (InvalidOperation, ValueError, TypeError):
                pass

        result = {
            "price": None,
            "winner": None,
            "lot": None,
            "last_sold_lot_number": None,
            "success_message": None,
            "online_high_bidder_message": None,
            "auction_minutes_to_end": None,
            "next_queued_lot_number": None,
        }
        lot, lot_error = self.validate_lot(lot, action)
        if lot and not lot_error and action == "to_online_high_bidder":
            result["success_message"] = lot.sell_to_online_high_bidder()
            result["last_sold_lot_number"] = lot.lot_number_display
            try:
                lot.add_winner_message(self.request.user, lot.auctiontos_winner, lot.winning_price)
            except Exception:
                logger.exception("add_winner_message failed for lot %s", lot.pk)
            try:
                lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Sold lot {lot.lot_number_display} to online high bidder",
                    user=self.request.user,
                )
            except Exception:
                logger.exception("create_history failed for lot %s", lot.pk)
            self.advance_queue_and_set_next(lot, result)
            return JsonResponse(result)
        price, price_error = self.validate_price(price, action)
        winner, winner_error = self.validate_winner(winner, action)
        if lot and not lot_error and action == "end_unsold":
            result["success_message"] = self.end_unsold(lot)
            result["last_sold_lot_number"] = lot.lot_number_display
            try:
                lot.auction.create_history(
                    applies_to="LOTS",
                    action=f"Marked lot {lot.lot_number_display} as ended without being sold",
                    user=self.request.user,
                )
            except Exception:
                logger.exception("create_history failed for lot %s", lot.pk)
            self.advance_queue_and_set_next(lot, result)
            return JsonResponse(result)
        price_error, winner_error = self.cross_check_price_and_winner(
            lot, price, winner, action, lot_error, price_error, winner_error
        )
        if not lot_error and not price_error and not winner_error:
            if action != "validate":
                result["last_sold_lot_number"] = lot.lot_number_display
            if action == "force_save" or action == "save":
                self.commit_winner(lot, winner, price, action, result)
        # With two people recording, check whether the lot was already sold.
        if (
            lot
            and winner
            and price
            and not price_error
            and not winner_error
            and lot_error == "This lot has already been sold"
            and (action == "force_save" or action == "save")
        ):
            if winner == lot.auctiontos_winner and price == lot.winning_price:
                # Lot has been double checked -- mark it as good
                lot.admin_validated = True
                lot.save()
                result["success_message"] = "This lot has been double checked"
                result["last_sold_lot_number"] = lot.lot_number_display
                self.advance_queue_and_set_next(lot, result, double_check=True)
            else:
                result = {
                    "banner": "error",
                    "last_sold_lot_number": lot.lot_number_display,
                    "success_message": f"Lot {lot.lot_number_display} already sold for {lot.currency_symbol}{lot.winning_price} to {lot.auctiontos_winner.bidder_number}.  If this is not correct, you can undo this sale",
                }
                # A double check that didn't match: the banner and its undo carry the problem, and the
                # form moves on as a matching check would, so this person keeps their place in the queue.
                next_lot = queue_next_to_record(self.auction, after_lot=lot, include_recorded=True)
                result["next_queued_lot_number"] = next_lot.lot_number_display if next_lot else None
        if lot and (action == "validate" or not result["success_message"]) and lot.high_bidder:
            result["online_high_bidder_message"] = (
                f"Sell to {lot.high_bidder_for_admins} for {lot.currency_symbol}{lot.high_bid}"
            )
            # JS not in place; also remove from view_lot_simple.
        if lot and not lot_error:
            lot = "valid"
        if price is not None and not price_error:
            price = "valid"
        if winner and not winner_error:
            winner = "valid"
        result["lot"] = lot_error or lot
        result["price"] = price_error or price
        result["winner"] = winner_error or winner
        if not lot_error and not price_error and not winner_error:
            result["auction_minutes_to_end"] = self.auction.estimate_end
            result["unsold_lot_count"] = self.auction.total_unsold_lots
        return JsonResponse(result)


class LotEndUnsold(LoginRequiredMixin, AuctionViewMixin, View):
    """The admin lot list's "End lot unsold": ``Lot.end_unsold`` by pk, with set-winners' checks."""

    def dispatch(self, request, *args, **kwargs):
        self.lot = get_object_or_404(Lot, pk=kwargs.pop("pk"), is_deleted=False, auction__isnull=False)
        self.auction = self.lot.auction
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.require_auction_admin()
        lot = self.lot
        error = None
        if self.auction.is_online:
            error = "Lots in an online auction end on their own"
        elif lot.sold:
            error = "This lot has already been sold"
        elif lot.ended_unsold:
            error = "This lot has already ended unsold"
        elif lot.sellers_invoice and lot.sellers_invoice.status != "DRAFT":
            error = "The seller's invoice is not open"
        if error:
            return close_modal_response(toast=f"Lot {lot.lot_number_display}: {error}", toast_type="danger")
        lot.end_unsold(request.user)
        self.auction.create_history(
            applies_to="LOTS",
            action=f"Marked lot {lot.lot_number_display} as ended without being sold",
            user=request.user,
        )
        queue_lot_recorded(self.auction, lot)
        return close_modal_response("reload-page")


class AuctionUnsellLot(LoginRequiredMixin, AuctionViewMixin, View):
    def find_lot(self, lot_number):
        """Find a lot by this auction's numbering. Shared with the palette's ``undo_sale``."""
        lot_number = str(lot_number or "").strip()
        if not lot_number:
            return None
        if self.auction.use_seller_dash_lot_numbering:
            return self.auction.lots_qs.filter(custom_lot_number=lot_number).first()
        # An integer column: anything else raised ValueError.
        if not lot_number.isdigit():
            return None
        return self.auction.lots_qs.filter(lot_number_int=int(lot_number)).first()

    def unsell(self, undo_lot):
        """Clear a lot's winner and record why. Returns the view's result dict. Shared with ``undo_sale``."""
        result = {
            "hide_undo_button": "true",
            "last_sold_lot_number": "",
            "success_message": f"{undo_lot.lot_number_display} {undo_lot.lot_name} now has no winner and can be sold",
        }
        stale_invoices = _lot_invoices(undo_lot)
        undo_lot.winner = None
        undo_lot.auctiontos_winner = None
        undo_lot.winning_price = None
        if not self.auction.is_online:
            undo_lot.date_end = None
            # Only called for in-person auctions; may need changing for online.
        undo_lot.active = True
        undo_lot.admin_validated = False
        undo_lot.save()
        _recalculate_invoices(stale_invoices)
        undo_lot.auction.create_history(
            applies_to="LOTS",
            action=f"Cleared the winner on lot {undo_lot.lot_number_display} to make it unsold",
            user=self.request.user,
        )
        return result

    def post(self, request, *args, **kwargs):
        from auctions.palette_actions import _settled_invoice_warning

        if self.auction.is_online:
            # Online winners come from bids, and reopening would leave the lot's end date in the past.
            return JsonResponse({"message": "Sales in an online auction can't be undone here"})
        undo_lot = self.find_lot(request.POST.get("lot_number", None))
        if not undo_lot:
            return JsonResponse({"message": "No lot found"})
        # As the palette's undo_sale: refuse to change a settled invoice unless forced.
        settled = _settled_invoice_warning(undo_lot)
        if settled and not request.POST.get("force"):
            return JsonResponse(
                {
                    "banner": "error",
                    "last_sold_lot_number": undo_lot.lot_number_display,
                    "success_message": f"{settled}. Reopen the invoice first.",
                }
            )
        return JsonResponse(self.unsell(undo_lot))

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)


#: A lot already announced "about to be sold" is announced again only when it comes up again at least
#: this long afterwards: the first was a mistyped lot number, or the room passed it and came back.
#: Shorter than that is Next pressed once too often, or the same lot pulled up twice.
SELLING_PUSH_COOLDOWN = timedelta(minutes=2)

#: Queue positions, counting the lot on the block as 1, whose watchers hear "coming up soon".
COMING_UP_POSITIONS = 10


def queue_entry_done(entry):
    """Sold, or ended unsold since it was queued: nothing left for set winners to do. A lot that didn't
    sell and was queued again is getting another go.
    """
    lot = entry.lot
    if lot.sold:
        return True
    return bool(lot.ended_unsold and lot.date_end and lot.date_end >= entry.createdon)


def notify_watchers_lot_selling_soon(lot, request_user=None, position=None):
    """Send a "coming up soon" or "about to be sold" web push to a lot's watchers.

    Coming up (``position`` 2-10) is once per lot, and never after "about to be sold"; about to be sold
    (``position`` 1 or None) is once per ``SELLING_PUSH_COOLDOWN``. Both share a tag, so the second
    replaces the first. ``request_user`` is excluded. Returns True when a pass ran. App users get only
    the app push, since a phone's browser and its app can't be told apart.
    """
    if not lot or lot.sold or not lot.auction:
        return False
    now = timezone.now()
    coming_up = position is not None and position > 1
    # A conditional UPDATE, so two screens pulling up one lot at once can't both send.
    if coming_up:
        if lot.coming_up_push_sent or lot.selling_push_sent_at:
            return False
        claimed = Lot.objects.filter(pk=lot.pk, coming_up_push_sent=False, selling_push_sent_at__isnull=True).update(
            coming_up_push_sent=True
        )
        lot.coming_up_push_sent = True
        head = f"{lot.lot_name} is coming up soon"
        body = (
            f"Lot {lot.lot_number_display} is coming up soon -- {position} lots away. Don't miss out!  "
            "You're getting this notification because you watched this lot."
        )
    else:
        if lot.selling_push_sent_at and now - lot.selling_push_sent_at < SELLING_PUSH_COOLDOWN:
            return False
        claimed = (
            Lot.objects.filter(pk=lot.pk)
            .filter(Q(selling_push_sent_at__isnull=True) | Q(selling_push_sent_at__lte=now - SELLING_PUSH_COOLDOWN))
            .update(selling_push_sent_at=now)
        )
        lot.selling_push_sent_at = now
        head = f"{lot.lot_name} is about to be sold"
        body = (
            f"Lot {lot.lot_number_display}  Don't miss out, bid now!  "
            "You're getting this notification because you watched this lot."
        )
    if not claimed:
        return False
    watchers = Watch.objects.filter(
        lot_number=lot.pk, user__userdata__push_notifications_when_lots_sell=True
    ).select_related("user__userdata")
    if request_user is not None:
        # Not on the projector.
        watchers = watchers.exclude(user=request_user)
    lot_url = "https://" + lot.full_lot_link
    # Shared tag, so the second alert replaces the first.
    tag = f"lot_sell_notification_{lot.pk}"
    for watch in watchers:
        if user_has_app_push(watch.user):
            send_push_to_user.delay(
                watch.user.pk,
                title=head,
                body=body,
                url=lot_url,
                category=CATEGORY_LOT_SELLING,
                collapse_key=tag,
                auction_pk=lot.auction.pk,
            )
            continue
        # does the user actually have a subscription?
        push_info = PushInformation.objects.filter(user=watch.user).first()
        if not push_info:
            continue
        payload = {
            "head": head,
            "body": body,
            "url": lot_url,
            "tag": tag,
        }
        if lot.thumbnail:
            payload["icon"] = lot.thumbnail.display_url
        try:
            send_user_notification(user=watch.user, payload=payload, ttl=10000)
        except (requests.exceptions.RequestException, WebPushException):
            # Invalid endpoint: delete it and log to auction history. django-webpush only handles
            # 410; FCM expires with 404.
            push_info.delete()
            AuctionHistory.objects.create(
                auction=lot.auction,
                user=None,
                action=f"push notification error occurred for {watch.user.username}",
                applies_to="USERS",
            )
    return True


def announce_lot_on_the_block(lot, request_user=None):
    """Tell the lot's own page, and its watchers, that it's being sold now."""
    lot.send_websocket_message(
        {
            "type": "chat_message",
            "info": "CHAT",
            "message": "This lot is about to be sold!",
            "pk": -1,
            "username": "System",
        }
    )
    notify_watchers_lot_selling_soon(lot, request_user=request_user)


def broadcast_queue_update(auction):
    """Tell open queue and projector screens to re-fetch after a queue change. Best-effort."""
    try:
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"auctions_{auction.pk}",
            {"type": "queue_updated"},
        )
    except Exception:
        logger.exception("Failed to send queue_updated websocket for auction %s", auction.pk)


# ── The lot queue ──────────────────────────────────────────────────────────────────────────────
#
# Entries stay after their lot sells. The passed ones are a prefix of the running order, and the first
# entry not passed is the lot on the block. Two things move that line, and the watchers' pushes follow
# it rather than set winners: Next and Back on the queue screens, and recording the lot on the block.
# Recording a lot behind the line (winners written down and entered later) or ahead of it (sold out
# of turn) leaves it where the room is.


def queue_entries(auction):
    return list(LotQueueEntry.objects.filter(auction=auction).select_related("lot").order_by("order"))


def queue_split(entries):
    """``(passed, on_the_block or None, still_to_come)`` of an ordered entry list."""
    index = next((i for i, entry in enumerate(entries) if entry.passed_at is None), None)
    if index is None:
        return entries, None, []
    return entries[:index], entries[index], entries[index + 1 :]


def queue_has_reached(lot):
    """True when the queue has got to this lot: it's on the block, announced from there, or behind it,
    where set winners is catching up on lots the room has already sold.
    """
    entry = LotQueueEntry.objects.filter(lot=lot).first()
    if entry is None:
        return False
    if entry.passed_at:
        return True
    return not LotQueueEntry.objects.filter(
        auction_id=entry.auction_id, passed_at__isnull=True, order__lt=entry.order
    ).exists()


def process_queue_notifications(auction):
    """Announce the lot on the block if this turn of it hasn't been, tell the next lots' watchers they're
    coming up, and refresh open queue screens.

    Safe after every change: a lot is announced once per turn on the block (and the push has its own
    cooldown), and coming up is once per lot. Pushes honour ``message_users_when_lots_sell``; the
    websocket refresh always fires.
    """
    if auction.message_users_when_lots_sell:
        _passed, on_the_block, to_come = queue_split(queue_entries(auction))
        # A lot's turn on the block ends when anything else is on it, so coming back is a new turn.
        LotQueueEntry.objects.filter(auction=auction, announced=True).exclude(
            pk=getattr(on_the_block, "pk", None)
        ).update(announced=False)
        if on_the_block:
            claimed = LotQueueEntry.objects.filter(pk=on_the_block.pk, announced=False).update(announced=True)
            if claimed and not queue_entry_done(on_the_block):
                announce_lot_on_the_block(on_the_block.lot)
            upcoming = [entry.lot for entry in to_come if not queue_entry_done(entry)]
            for position, lot in enumerate(upcoming[: COMING_UP_POSITIONS - 1], start=2):
                notify_watchers_lot_selling_soon(lot, position=position)
    broadcast_queue_update(auction)


def _locked_queue(auction):
    return list(
        LotQueueEntry.objects.select_for_update().filter(auction=auction).select_related("lot").order_by("order")
    )


def advance_queue(auction, from_entry_id=None):
    """Pass the lot on the block, and any already recorded lots right after it. Returns True if it moved.

    ``from_entry_id`` is the entry the caller saw on the block. If the queue has moved since, nothing
    happens: Next pressed just as a sale moved the queue on doesn't skip a lot too.
    """
    with transaction.atomic():
        _passed, on_the_block, to_come = queue_split(_locked_queue(auction))
        if on_the_block is None or (from_entry_id is not None and on_the_block.pk != from_entry_id):
            return False
        passing = [on_the_block.pk]
        for entry in to_come:
            if not queue_entry_done(entry):
                break
            passing.append(entry.pk)
        LotQueueEntry.objects.filter(pk__in=passing).update(passed_at=timezone.now())
    process_queue_notifications(auction)
    return True


def rewind_queue(auction, from_entry_id=None):
    """Put the last passed lot back on the block. Returns True if it moved.

    ``from_entry_id`` as ``advance_queue``, with 0 for a screen that showed the queue finished.
    """
    with transaction.atomic():
        passed, on_the_block, _to_come = queue_split(_locked_queue(auction))
        showing = on_the_block.pk if on_the_block else 0
        if not passed or (from_entry_id is not None and showing != from_entry_id):
            return False
        LotQueueEntry.objects.filter(pk=passed[-1].pk).update(passed_at=None)
    process_queue_notifications(auction)
    return True


def queue_lot_recorded(auction, lot):
    """A lot's winner, or no sale, was just recorded. On the block, the queue moves on; anywhere else it
    stays put, but screens still refresh to show the sale.
    """
    entry = LotQueueEntry.objects.filter(auction=auction, lot=lot).first()
    if entry and not advance_queue(auction, from_entry_id=entry.pk):
        process_queue_notifications(auction)


def queue_next_to_record(auction, after_lot=None, include_recorded=False):
    """The lot set winners should pull up next, or None.

    After a queued lot, the next one after it still to be recorded, so each person recording works down
    the queue at their own pace, behind the room or with it. ``include_recorded`` is for a double check:
    the next lot whatever its state, since the first person has probably recorded it already. Otherwise
    the lot on the block, or the first still to be recorded.
    """
    entries = queue_entries(auction)
    after_pk = getattr(after_lot, "pk", None)
    index = next((i for i, entry in enumerate(entries) if entry.lot_id == after_pk), None)
    if index is not None:
        for entry in entries[index + 1 :]:
            if include_recorded or not queue_entry_done(entry):
                return entry.lot
        return None
    passed, on_the_block, to_come = queue_split(entries)
    for entry in ([on_the_block] if on_the_block else []) + to_come + passed:
        if not queue_entry_done(entry):
            return entry.lot
    return None


def add_lot_to_queue(auction, lot, user):
    """Add a lot to the end of the queue. A lot the room passed unsold goes back on the end. Returns an
    error string or None.
    """
    if not lot:
        return "No lot found"
    if lot.sold:
        return f"Lot {lot.lot_number_display} has already been sold"
    # Two scanners on one lot at once: the loser's insert hits the one-to-one on lot.
    try:
        with transaction.atomic():
            last_order = (
                LotQueueEntry.objects.select_for_update()
                .filter(auction=auction)
                .order_by("-order")
                .values_list("order", flat=True)
                .first()
            ) or 0
            entry, created = LotQueueEntry.objects.get_or_create(
                lot=lot,
                defaults={"auction": auction, "order": last_order + 1, "added_by": user},
            )
            if not created and entry.passed_at:
                entry.order = last_order + 1
                entry.passed_at = None
                entry.createdon = timezone.now()
                entry.save(update_fields=["order", "passed_at", "createdon"])
                created = True
    except IntegrityError:
        created = False
    if not created:
        return f"Lot {lot.lot_number_display} is already in the queue"
    # Sticky, for the queue-usage stat.
    if not lot.added_to_queue:
        lot.added_to_queue = True
        lot.save(update_fields=["added_to_queue"])
    process_queue_notifications(auction)
    return None


def reorder_queue(auction, ordered_ids):
    """Persist a new order given a list of entry ids (top first). Passed entries stay in front, and
    entries not mentioned keep their relative order after the ones that are.
    """
    entries = LotQueueEntry.objects.filter(auction=auction).order_by("order")
    passed = [entry for entry in entries if entry.passed_at]
    to_come = {entry.pk: entry for entry in entries if not entry.passed_at}
    moved = []
    for raw in ordered_ids:
        try:
            pk = int(raw)
        except (ValueError, TypeError):
            continue
        entry = to_come.pop(pk, None)
        if entry:
            moved.append(entry)
    rest = sorted(to_come.values(), key=lambda e: e.order)
    for order, entry in enumerate(passed + moved + rest, start=1):
        if entry.order != order:
            entry.order = order
            entry.save(update_fields=["order"])
    process_queue_notifications(auction)


class LotQueueMixin(LoginRequiredMixin, AuctionViewMixin):
    """Helpers for the in-person lot queue (LotQueueEntry), built by scanning or typing lots."""

    club_sidebar_can_view = False  # full-screen tool; sidebar would waste space

    def dispatch(self, request, *args, **kwargs):
        # Let LoginRequiredMixin redirect anonymous users first.
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        # get_auction raises PermissionDenied for non-admins.
        self.get_auction(kwargs.get("slug", ""))
        if self.auction and self.auction.is_online:
            msg = "The lot queue is only available for in-person auctions"
            raise Http404(msg)
        return super().dispatch(request, *args, **kwargs)

    def queue_context(self):
        passed, on_the_block, to_come = queue_split(queue_entries(self.auction))
        return {"auction": self.auction, "passed": passed, "on_the_block": on_the_block, "to_come": to_come}

    def resolve_lot_from_value(self, value):
        """A scanned lot QR URL or typed lot number to ``(Lot or None, error or None)``."""
        value = (value or "").strip()
        if not value:
            return None, "Enter or scan a lot number"
        # A lot QR is https://{domain}/qr/{pk}/; a USB scanner types the whole URL.
        qr_match = re.search(r"/qr/(\d+)", value)
        if qr_match:
            lot = self.auction.lots_qs.filter(pk=qr_match.group(1)).first()
            if not lot:
                return None, "That lot is not part of this auction"
            return lot, None
        if self.auction.use_seller_dash_lot_numbering:
            result_lot_qs = self.auction.lots_qs.filter(custom_lot_number=value)
        else:
            try:
                number = int(value)
            except (ValueError, TypeError):
                return None, "Lot number must be a number"
            result_lot_qs = self.auction.lots_qs.filter(lot_number_int=number)
        if result_lot_qs.count() > 1:
            return None, "More than one lot has this number -- scan the lot's QR code instead"
        lot = result_lot_qs.first()
        if not lot:
            return None, "No lot found with that number"
        return lot, None

    def add_lot(self, lot):
        return add_lot_to_queue(self.auction, lot, self.request.user)

    def apply_reorder(self, ordered_ids):
        reorder_queue(self.auction, ordered_ids)

    def render_list(self, error=None):
        return render(self.request, "auctions/lot_queue_list.html", {**self.queue_context(), "error": error})


class LotQueueView(LotQueueMixin, TemplateView):
    """The lot queue page. GET renders it (``?partial=list`` for the list); POST adds, removes, reorders,
    moves the queue on (``next``/``back``, with ``from``: the entry id the screen showed, 0 for none), or
    takes a scanner ``lot_pk`` (JSON).
    """

    template_name = "auctions/lot_queue.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("partial") == "list":
            return self.render_list()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.queue_context())
        context["show_camera_scanner"] = True
        # For lot QR scans through the ribbon's barcode scanner.
        context["barcode_lot_scan_url"] = self.request.path
        return context

    def post(self, request, *args, **kwargs):
        if "lot_pk" in request.POST:
            pk = (request.POST.get("lot_pk") or "").strip()
            lot = self.auction.lots_qs.filter(pk=pk).first() if pk.isdigit() else None
            if not lot:
                return JsonResponse({"ok": False, "message": "That lot is not part of this auction"})
            error = self.add_lot(lot)
            if error:
                return JsonResponse({"ok": False, "message": error})
            return JsonResponse({"ok": True, "message": f"Added lot {lot.lot_number_display} to the queue"})
        action = request.POST.get("action", "")
        if action == "add":
            lot, error = self.resolve_lot_from_value(request.POST.get("value", ""))
            if lot and not error:
                error = self.add_lot(lot)
            return self.render_list(error=error)
        if action == "remove":
            entry_id = (request.POST.get("entry_id") or "").strip()
            if entry_id.isdigit():
                LotQueueEntry.objects.filter(auction=self.auction, pk=entry_id).delete()
            process_queue_notifications(self.auction)
            return self.render_list()
        if action == "reorder":
            ordered_ids = request.POST.getlist("order[]") or request.POST.get("order", "").split(",")
            self.apply_reorder(ordered_ids)
            return self.render_list()
        if action in ("next", "back"):
            raw = (request.POST.get("from") or "").strip()
            from_entry_id = int(raw) if raw.isdigit() else None
            move = advance_queue if action == "next" else rewind_queue
            if move(self.auction, from_entry_id=from_entry_id):
                return self.render_list()
            on_the_block = self.queue_context()["on_the_block"]
            if from_entry_id not in (None, on_the_block.pk if on_the_block else 0):
                # Someone else moved it first; the fresh list is the answer.
                return self.render_list()
            if action == "next":
                return self.render_list(error="That was the last lot in the queue")
            return self.render_list(error="This is the first lot in the queue")
        return self.render_list(error="Unknown action")


class LotQueueFullscreenView(LotQueueMixin, TemplateView):
    """Fullscreen queue partial: the lot on the block large, plus the next few. Refreshed over websocket,
    with a slow poll fallback. No ViewLotSimple notification side effect.
    """

    template_name = "auctions/lot_queue_fullscreen.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queue = self.queue_context()
        context["auction"] = self.auction
        context["on_the_block"] = queue["on_the_block"]
        context["upcoming"] = [entry.lot for entry in queue["to_come"] if not queue_entry_done(entry)][:5]
        return context


class LotQueueCurrentLotView(LotQueueMixin, TemplateView):
    """Fullscreen current lot: its own page for a projector, the lot on the block and only its details (no
    bids, no queue). ``?partial=lot`` is the lot alone, re-fetched as the queue moves.
    """

    template_name = "auctions/lot_queue_current_lot.html"

    def get_template_names(self):
        if self.request.GET.get("partial") == "lot":
            return ["auctions/lot_queue_current_lot_partial.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["on_the_block"] = self.queue_context()["on_the_block"]
        return context


# ── Volunteers (Part 7): recruit help for a job ────────────────────────────────────────────────


def volunteer_eligible_tos(auction):
    """AuctionTOS rows reachable by push right now (a live token).

    Push-only, since a late email is useless. In check-in auctions, only the checked in (the geofence
    is the proximity signal); otherwise everyone joined with the app, which the page warns about.
    """
    from auctions.notifications import push_configured

    if not push_configured():
        # Push not configured: nobody is reachable.
        return AuctionTOS.objects.none()
    qs = AuctionTOS.objects.filter(auction=auction, user__isnull=False)
    if auction.use_check_in_mode:
        qs = qs.filter(checked_in__isnull=False)
    # Exists(), not a join: a join would drop anyone who owns any tokenless device.
    live_device = MobileDevice.objects.filter(user=OuterRef("user"), push_enabled=True).exclude(fcm_token="")
    return qs.filter(Exists(live_device))


def volunteer_helper_count(auction):
    """Recipients counted per user, matching what ``notify_volunteers_of_job`` sends."""
    return volunteer_eligible_tos(auction).values("user").distinct().count()


def _volunteer_job_url(job):
    from django.contrib.sites.models import Site

    domain = Site.objects.get_current().domain
    path = reverse("auction_volunteer_job", kwargs={"slug": job.auction.slug, "job_pk": job.pk})
    return f"https://{domain}{path}"


# Fixed and short so "help needed" survives notification truncation.
VOLUNTEER_PUSH_TITLE = "Auction help needed"


def _volunteer_notification_text(job):
    body = job.description
    if job.bounty:
        body += f" (${job.bounty:.0f} bounty)"
    return VOLUNTEER_PUSH_TITLE, body


def notify_volunteers_of_job(job):
    """Push a job announcement to every reachable helper. No email fallback. A per-job collapse tag lets it
    be retracted.
    """
    from auctions.notifications import CATEGORY_VOLUNTEER

    title, body = _volunteer_notification_text(job)
    url = _volunteer_job_url(job)
    collapse_key = f"volunteer_job_{job.pk}"
    seen = set()
    for tos in volunteer_eligible_tos(job.auction).select_related("user"):
        user = tos.user
        if user.pk in seen:
            continue
        seen.add(user.pk)
        send_push_to_user.delay(
            user.pk,
            title=title,
            body=body,
            url=url,
            category=CATEGORY_VOLUNTEER,
            collapse_key=collapse_key,
            auction_pk=job.auction.pk,
        )


def withdraw_volunteer_notification(job):
    """Retract a job's announcement when filled or cancelled. Best-effort: the accept page is the source of
    truth and signup enforces first come, first served.
    """
    logger.info("Volunteer job %s filled/canceled; retracting its announcement (tag volunteer_job_%s)", job.pk, job.pk)


class AuctionVolunteers(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Admin page: ask app users to help with a job, and review past jobs. In-person only."""

    template_name = "auctions/auction_volunteers.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        _ = self.can_add_edit_people  # enforces admin (raises PermissionDenied otherwise)
        if self.auction.is_online:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["form"] = kwargs.get("form") or VolunteerJobForm()
        context["jobs"] = self.auction.volunteer_jobs.all()
        context["helper_count"] = volunteer_helper_count(self.auction)
        return context

    def post(self, request, *args, **kwargs):
        redirect_url = reverse("auction_volunteers", kwargs={"slug": self.auction.slug})
        if request.POST.get("action") == "cancel":
            job = get_object_or_404(VolunteerJob, pk=request.POST.get("job_pk"), auction=self.auction)
            if not job.canceled:
                job.canceled = True
                job.save(update_fields=["canceled"])
                self.auction.create_history(
                    applies_to="USERS", action=f"Canceled volunteer job: {job.description}", user=request.user
                )
                withdraw_volunteer_notification(job)
            return redirect(redirect_url)
        form = VolunteerJobForm(request.POST)
        if form.is_valid():
            job = form.save(commit=False)
            job.auction = self.auction
            job.created_by = request.user
            job.save()
            bounty_txt = f" (bounty ${job.bounty:.0f})" if job.bounty else ""
            self.auction.create_history(
                applies_to="USERS",
                action=f"Asked for {job.people_needed} people: {job.description}{bounty_txt}",
                user=request.user,
            )
            notify_volunteers_of_job(job)
            messages.success(request, "Your request for help has been sent.")
            return redirect(redirect_url)
        return self.render_to_response(self.get_context_data(form=form))


class VolunteerJobAccept(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """The page a job notification opens: joined users sign up while spots remain."""

    template_name = "auctions/volunteer_job_accept.html"
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.get("slug", ""))
        self.job = get_object_or_404(VolunteerJob, pk=kwargs.get("job_pk"), auction=self.auction)
        return super().dispatch(request, *args, **kwargs)

    def _tos(self):
        return AuctionTOS.objects.filter(auction=self.auction, user=self.request.user).first()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tos = self._tos()
        context["auction"] = self.auction
        context["job"] = self.job
        context["has_tos"] = tos is not None
        context["already_signed_up"] = bool(
            tos and VolunteerSignup.objects.filter(job=self.job, auctiontos=tos).exists()
        )
        context["rules_url"] = self.auction.get_absolute_url()
        return context

    def post(self, request, *args, **kwargs):
        redirect_url = reverse("auction_volunteer_job", kwargs={"slug": self.auction.slug, "job_pk": self.job.pk})
        tos = self._tos()
        if tos is None:
            messages.info(request, "Join the auction first, then you can sign up to help.")
            return redirect(redirect_url)
        if self.job.canceled:
            messages.info(request, "This job was canceled.")
            return redirect(redirect_url)
        # Only the people the job was announced to, so a bounty goes to someone who's there.
        tos = volunteer_eligible_tos(self.auction).filter(user=request.user).first()
        if tos is None:
            if self.auction.use_check_in_mode:
                messages.info(request, "Check in at the auction first, then you can sign up to help.")
            else:
                messages.info(request, "Turn on notifications in the app to sign up to help.")
            return redirect(redirect_url)
        filled = False
        invoice = None
        with transaction.atomic():
            # Locked so two people can't take the last spot.
            job = VolunteerJob.objects.select_for_update().get(pk=self.job.pk)
            if VolunteerSignup.objects.filter(job=job, auctiontos=tos).exists():
                messages.info(request, "You're already signed up for this one.")
                return redirect(redirect_url)
            if job.is_full:
                messages.info(request, "This job already has enough people.")
                return redirect(redirect_url)
            adjustment = None
            # InvoiceAdjustment.amount is unsigned; a negative bounty would charge the volunteer anyway.
            bounty = int(round(job.bounty)) if job.bounty else 0
            if bounty > 0:
                invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
                if invoice and invoice.status != "DRAFT":
                    messages.info(request, "Your invoice for this auction is closed, so ask an admin to sign you up.")
                    return redirect(redirect_url)
                if not invoice:
                    invoice = Invoice.for_participant(tos, self.auction)
                adjustment = InvoiceAdjustment.objects.create(
                    invoice=invoice,
                    user=request.user,
                    adjustment_type="DISCOUNT",
                    amount=bounty,
                    notes=f"Volunteer: {job.description}"[:150],
                )
            VolunteerSignup.objects.create(job=job, auctiontos=tos, invoice_adjustment=adjustment)
            self.auction.create_history(
                applies_to="USERS",
                action=f"{tos.name or request.user.username} signed up for {job.description}",
                user=request.user,
            )
            filled = job.is_full
        if invoice:
            invoice.recalculate()
        if filled:
            self.auction.create_history(
                applies_to="USERS", action=f"Volunteer job filled: {self.job.description}", user=None
            )
            withdraw_volunteer_notification(self.job)
        messages.success(request, "You're signed up!")
        return redirect(redirect_url)
