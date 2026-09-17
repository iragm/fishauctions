"""Auction night: setting winners, the lot queue, and volunteers.

``DynamicSetLotWinner`` is the auctioneer's page, driven by :mod:`auctions.voice`. The queue views
decide which lot is next and notify its watchers.
"""

import logging
import re
from decimal import Decimal, InvalidOperation

import channels.layers
import requests
from asgiref.sync import async_to_sync
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import (
    Exists,
    Max,
    OuterRef,
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
    LotHistory,
    LotQueueEntry,
    MobileDevice,
    VolunteerJob,
    VolunteerSignup,
    Watch,
)
from auctions.notifications import CATEGORY_LOT_SELLING, notify_running_total, user_has_app_push
from auctions.tasks import (
    send_push_to_user,
)

from .base import AuctionViewMixin, _upsert_clubmember_shadow_tos

logger = logging.getLogger(__name__)


class DynamicSetLotWinner(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """A form to set lot winners.  Totally async with no page loads, just POST"""

    template_name = "auctions/dynamic_set_lot_winner.html"
    club_sidebar_can_view = False  # full-screen tool; sidebar would waste space

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        # Prefill the lot from the head of the in-person queue.
        head_lot = queue_head_lot(self.auction)
        context["queue_head_lot_number"] = head_lot.lot_number_display if head_lot else ""
        # Voice (app only): score cutoffs, grammar and vocabulary, so the page can match a transcript
        # itself (voice.page_config). Skipped outside the app: this page is hot during an auction.
        if getattr(self.request, "is_mobile_app", False):
            context["voice_config"] = voice.page_config(self.auction)
        return context

    def pop_queue_and_set_next(self, lot, result):
        """Remove the sold lot from the queue and set ``result["next_queued_lot_number"]`` (None when empty)
        so the page auto-advances.
        """
        pop_lot_from_queue(self.auction, lot)
        next_lot = queue_head_lot(self.auction)
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
        lot.date_end = timezone.now()
        lot.winner = None
        lot.auctiontos_winner = None
        lot.winning_price = None
        lot.active = False
        lot.save()
        message = f"{self.request.user} has marked lot {lot.lot_number_display} as not sold"
        LotHistory.objects.create(
            lot=lot,
            user=self.request.user,
            message=message,
            changed_price=True,
        )
        lot.send_websocket_message(
            {
                "type": "chat_message",
                "info": "ENDED_NO_WINNER",
                "message": message,
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
            }
        )
        return message

    def set_winner(self, lot, winning_tos, winning_price):
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
        self.pop_queue_and_set_next(lot, result)
        return result

    def post(self, request, *args, **kwargs):
        """All lot validation checks called from here"""
        lot = request.POST.get("lot", None)
        price = request.POST.get("price", None)
        winner = request.POST.get("winner", None)
        action = request.POST.get("action", "validate")

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
            result["success_message"] = lot.sell_to_online_high_bidder
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
            self.pop_queue_and_set_next(lot, result)
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
            self.pop_queue_and_set_next(lot, result)
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
                self.pop_queue_and_set_next(lot, result)
            else:
                result = {
                    "banner": "error",
                    "last_sold_lot_number": lot.lot_number_display,
                    "success_message": f"Lot {lot.lot_number_display} already sold for {lot.currency_symbol}{lot.winning_price} to {lot.auctiontos_winner.bidder_number}.  If this is not correct, you can undo this sale",
                }
        if lot and (action == "validate" or not result["success_message"]) and lot.high_bidder:
            result["online_high_bidder_message"] = (
                f"Sell to {lot.high_bidder_for_admins} for {lot.currency_symbol}{lot.high_bid}"
            )
            # JS not in place; also remove from view_lot_simple.
        if lot and not lot_error:
            lot = "valid"
        if price and not price_error:
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


class AuctionUnsellLot(LoginRequiredMixin, AuctionViewMixin, View):
    def find_lot(self, lot_number):
        """Find a lot by this auction's numbering. Shared with the palette's ``undo_sale``."""
        if not lot_number:
            return None
        if self.auction.use_seller_dash_lot_numbering:
            return self.auction.lots_qs.filter(custom_lot_number=lot_number).first()
        return self.auction.lots_qs.filter(lot_number_int=lot_number).first()

    def unsell(self, undo_lot):
        """Clear a lot's winner and record why. Returns the view's result dict. Shared with ``undo_sale``."""
        result = {
            "hide_undo_button": "true",
            "last_sold_lot_number": "",
            "success_message": f"{undo_lot.lot_number_display} {undo_lot.lot_name} now has no winner and can be sold",
        }
        undo_lot.winner = None
        undo_lot.auctiontos_winner = None
        undo_lot.winning_price = None
        if not self.auction.is_online:
            undo_lot.date_end = None
            # Only called for in-person auctions; may need changing for online.
        undo_lot.active = True
        undo_lot.admin_validated = False
        undo_lot.save()
        undo_lot.auction.create_history(
            applies_to="LOTS",
            action=f"Cleared the winner on lot {undo_lot.lot_number_display} to make it unsold",
            user=self.request.user,
        )
        return result

    def post(self, request, *args, **kwargs):
        undo_lot = self.find_lot(request.POST.get("lot_number", None))
        if undo_lot:
            result = self.unsell(undo_lot)
        else:
            result = {"message": "No lot found"}
        return JsonResponse(result)

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)


class VoiceCommandLogView(LoginRequiredMixin, AuctionViewMixin, View):
    """Log what voice heard on the set-winners page, and any correction.

    The page writes it because only the page sees the match and the operator's fix; posting the
    returned ``id`` with ``corrected_to`` updates the row. Corrections are how the grammar gets tuned.
    No ``slot`` means an unmatched utterance, rate-limited in :func:`voice.log_unmatched`. Admin-only,
    fire-and-forget: ``{"id": <pk or null>}``, never an error that interrupts a sale.
    """

    def post(self, request, *args, **kwargs):
        if not request.POST.get("slot", ""):
            return JsonResponse(
                {
                    "id": voice.log_unmatched(
                        request.user,
                        self.auction,
                        heard=request.POST.get("heard", ""),
                        confidence=request.POST.get("confidence"),
                        session_key=request.session.session_key or "",
                    )
                }
            )
        log_id = request.POST.get("id")
        try:
            log_id = int(log_id) if log_id else None
        except (TypeError, ValueError):
            log_id = None
        result_id = voice.log_command(
            request.user,
            self.auction,
            log_id=log_id,
            slot=request.POST.get("slot", ""),
            heard=request.POST.get("heard", ""),
            chosen=request.POST.get("chosen", ""),
            confidence=request.POST.get("confidence"),
            corrected_to=request.POST.get("corrected_to", ""),
        )
        return JsonResponse({"id": result_id})

    def get(self, request, *args, **kwargs):
        return self.http_method_not_allowed(request, *args, **kwargs)


class VoiceVocabularyView(LoginRequiredMixin, AuctionViewMixin, View):
    """Current lot and bidder numbers for voice matching. The page never reloads, so its rendered
    vocabulary goes stale mid-auction; the mobile endpoint is JWT-only, so this serves the session.
    """

    def get(self, request, *args, **kwargs):
        from auctions.mobile.services import voice as voice_service

        return JsonResponse(voice_service.build_vocabulary(self.auction))


def notify_watchers_lot_selling_soon(lot, request_user=None, position=None):
    """Send a "coming up soon" or "about to be sold" web push to a lot's watchers.

    Coming up (``position`` 2-10) dedupes on ``Lot.coming_up_push_sent``; about to be sold
    (``position`` 1 or None) on ``Lot.selling_push_notification_sent``. Both share a tag, so the second
    replaces the first. ``request_user`` is excluded. Returns True when a pass ran. App users get only
    the app push, since a phone's browser and its app can't be told apart.
    """
    if not lot or lot.sold or not lot.auction:
        return False
    coming_up = position is not None and position > 1
    if coming_up:
        # Never downgrade after "about to be sold".
        if lot.coming_up_push_sent or lot.selling_push_notification_sent:
            return False
        lot.coming_up_push_sent = True
        lot.save(update_fields=["coming_up_push_sent"])
        head = f"{lot.lot_name} is coming up soon"
        body = (
            f"Lot {lot.lot_number_display} is coming up soon -- {position} lots away. Don't miss out!  "
            "You're getting this notification because you watched this lot."
        )
    else:
        if lot.selling_push_notification_sent:
            return False
        lot.selling_push_notification_sent = True
        lot.save(update_fields=["selling_push_notification_sent"])
        head = f"{lot.lot_name} is about to be sold"
        body = (
            f"Lot {lot.lot_number_display}  Don't miss out, bid now!  "
            "You're getting this notification because you watched this lot."
        )
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


def broadcast_queue_update(auction):
    """Tell open queue and kiosk screens to re-fetch after a queue change. Best-effort."""
    try:
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"auctions_{auction.pk}",
            {"type": "queue_updated"},
        )
    except Exception:
        logger.exception("Failed to send queue_updated websocket for auction %s", auction.pk)


def process_queue_notifications(auction):
    """Push to watchers of lots now in the queue's top 10, and refresh open queue screens.

    Deduped per lot, so it's safe after every mutation. Pushes honour
    ``message_users_when_lots_sell``; the websocket refresh always fires.
    """
    if auction.message_users_when_lots_sell:
        entries = LotQueueEntry.objects.filter(auction=auction).select_related("lot").order_by("order")
        for index, entry in enumerate(entries, start=1):
            if index > 10:
                break
            if entry.lot.sold:
                continue
            notify_watchers_lot_selling_soon(entry.lot, position=index)
    broadcast_queue_update(auction)


def queue_head_lot(auction):
    """The lot at the top of the queue (sold next), or None if the queue is empty."""
    entry = LotQueueEntry.objects.filter(auction=auction).select_related("lot").order_by("order").first()
    return entry.lot if entry else None


def pop_lot_from_queue(auction, lot):
    """Remove a lot's queue entry and re-run notifications. Used when a lot sells."""
    if lot is None:
        return
    deleted, _ = LotQueueEntry.objects.filter(auction=auction, lot=lot).delete()
    if deleted:
        process_queue_notifications(auction)


class LotQueueMixin(LoginRequiredMixin, AuctionViewMixin):
    """Helpers for the in-person lot queue (LotQueueEntry), built by scanning or typing lots; set winners
    pulls its head.
    """

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

    def queue_entries(self):
        return list(LotQueueEntry.objects.filter(auction=self.auction).select_related("lot").order_by("order"))

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
        """Add a lot to the end of the queue. Returns an error string or None."""
        if not lot:
            return "No lot found"
        if lot.sold:
            return f"Lot {lot.lot_number_display} has already been sold"
        if LotQueueEntry.objects.filter(auction=self.auction, lot=lot).exists():
            return f"Lot {lot.lot_number_display} is already in the queue"
        max_order = LotQueueEntry.objects.filter(auction=self.auction).aggregate(m=Max("order"))["m"] or 0
        LotQueueEntry.objects.create(auction=self.auction, lot=lot, order=max_order + 1, added_by=self.request.user)
        # Sticky, for the queue-usage stat.
        if not lot.added_to_queue:
            lot.added_to_queue = True
            lot.save(update_fields=["added_to_queue"])
        process_queue_notifications(self.auction)
        return None

    def apply_reorder(self, ordered_ids):
        """Persist a new order given a list of entry ids (top first)."""
        entries = {e.pk: e for e in LotQueueEntry.objects.filter(auction=self.auction)}
        order = 1
        for raw in ordered_ids:
            try:
                pk = int(raw)
            except (ValueError, TypeError):
                continue
            entry = entries.pop(pk, None)
            if entry:
                if entry.order != order:
                    entry.order = order
                    entry.save(update_fields=["order"])
                order += 1
        # Unmentioned entries keep their relative order after.
        for entry in sorted(entries.values(), key=lambda e: e.order):
            entry.order = order
            entry.save(update_fields=["order"])
            order += 1
        process_queue_notifications(self.auction)

    def render_list(self, error=None):
        context = {"auction": self.auction, "entries": self.queue_entries(), "error": error}
        return render(self.request, "auctions/lot_queue_list.html", context)


class LotQueueView(LotQueueMixin, TemplateView):
    """The lot queue page. GET renders it (``?partial=list`` for the list); POST adds, removes, reorders,
    or takes a scanner ``lot_pk`` (JSON).
    """

    template_name = "auctions/lot_queue.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("partial") == "list":
            return self.render_list()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["entries"] = self.queue_entries()
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
            LotQueueEntry.objects.filter(auction=self.auction, pk=request.POST.get("entry_id")).delete()
            process_queue_notifications(self.auction)
            return self.render_list()
        if action == "reorder":
            ordered_ids = request.POST.getlist("order[]") or request.POST.get("order", "").split(",")
            self.apply_reorder(ordered_ids)
            return self.render_list()
        return self.render_list(error="Unknown action")


class LotQueueKioskView(LotQueueMixin, TemplateView):
    """Projector partial: the head lot large, plus the next few. Refreshed over websocket, with a slow poll
    fallback. No ViewLotSimple notification side effect.
    """

    template_name = "auctions/lot_queue_kiosk.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entries = self.queue_entries()
        context["auction"] = self.auction
        context["lot"] = entries[0].lot if entries else None
        context["upcoming"] = [entry.lot for entry in entries[1:6]]
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
        filled = False
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
            if job.bounty:
                invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
                if not invoice:
                    invoice = Invoice.objects.create(auctiontos_user=tos, auction=self.auction)
                adjustment = InvoiceAdjustment.objects.create(
                    invoice=invoice,
                    user=request.user,
                    adjustment_type="DISCOUNT",
                    amount=int(round(job.bounty)),
                    notes=f"Volunteer: {job.description}"[:150],
                )
            VolunteerSignup.objects.create(job=job, auctiontos=tos, invoice_adjustment=adjustment)
            self.auction.create_history(
                applies_to="USERS",
                action=f"{tos.name or request.user.username} signed up for {job.description}",
                user=request.user,
            )
            filled = job.is_full
        if filled:
            self.auction.create_history(
                applies_to="USERS", action=f"Volunteer job filled: {self.job.description}", user=None
            )
            withdraw_volunteer_notification(self.job)
        messages.success(request, "You're signed up!")
        return redirect(redirect_url)
