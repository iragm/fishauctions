"""Auction night: setting winners, the lot queue, and the volunteers who help.

``DynamicSetLotWinner`` is the page an auctioneer actually stands in front of, and the one the voice
grammar in :mod:`auctions.voice` drives. The queue views below it decide which lot is up next and
notify the people watching it.
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
from auctions.notifications import CATEGORY_LOT_SELLING, user_has_app_push
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
        # Don't want notifications to show up on the projector
        # context['disable_websocket'] = True
        # Prefill the lot field from the head of the in-person lot queue (if any), so scanning lots
        # into the queue elsewhere flows straight into selling them here.
        head_lot = queue_head_lot(self.auction)
        context["queue_head_lot_number"] = head_lot.lot_number_display if head_lot else ""
        # Voice input (mobile app only — the app listens, this page owns the form). The page gets
        # the score cutoffs, so that "green" here and "confident" there mean the same thing after
        # somebody tunes them in the admin; and it gets the grammar and this auction's vocabulary
        # too, so it can match a transcript itself when the app sends one and no command follows.
        # See voice.page_config for why that fallback exists.
        #
        # Only looked up for the app: the template renders every voice element behind the same
        # is_mobile_app check, and this page is the busiest thing on the site while an auction is
        # actually running, so queries nothing on screen can use don't belong in that path.
        if getattr(self.request, "is_mobile_app", False):
            context["voice_config"] = voice.page_config(self.auction)
        return context

    def pop_queue_and_set_next(self, lot, result):
        """Drop the just-sold/ended lot from the in-person queue and report the new head lot number.

        Sets result["next_queued_lot_number"] to the new top lot's display number, or None when the
        queue is now empty. The set-winners JS uses this to auto-advance to the next lot."""
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
            # this next line makes it so you cannot search by custom_lot_number in a use_seller_dash_lot_numbering auction
            # if custom lot numbers are ever reenabled, change this
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
            # This can happen if two people are submitting lots at the exact same millisecond.  It seems very unlikely but an easy enough edge case to catch.
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
                # In club-managed mode, the source of truth for bidder numbers is ClubMember.
                # Look up the member by bidder number; if found, ensure a shadow AuctionTOS exists.
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
        if lot.auction and lot.auction.club and not lot.bap_points_awarded and not lot.manually_approved:
            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
        return f"Bidder {winning_tos.bidder_number} is now the winner of lot {lot.lot_number_display}"

    def cross_check_price_and_winner(self, lot, price, winner, action, lot_error, price_error, winner_error):
        """The price/winner checks that need the lot, price and winner all resolved together.

        Split out of ``post`` (unchanged behaviour) so the command palette's ``set_lot_winner``
        action runs exactly these checks rather than a parallel copy of them.
        Returns the possibly-updated ``(price_error, winner_error)``.
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
        """Record the sale: set the winner, check the buyer in on force_save, log history, advance the queue.

        Split out of ``post`` (unchanged behaviour) so the command palette's ``set_lot_winner``
        action commits through this exact code instead of reimplementing it.
        """
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
        # if two people are recording bids, we can validate whether or not a lot was sold
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
                # Mismatch between what's been saved in the db and the current request
                result = {
                    "banner": "error",
                    "last_sold_lot_number": lot.lot_number_display,
                    "success_message": f"Lot {lot.lot_number_display} already sold for {lot.currency_symbol}{lot.winning_price} to {lot.auctiontos_winner.bidder_number}.  If this is not correct, you can undo this sale",
                }
        if lot and (action == "validate" or not result["success_message"]) and lot.high_bidder:
            result["online_high_bidder_message"] = (
                f"Sell to {lot.high_bidder_for_admins} for {lot.currency_symbol}{lot.high_bid}"
            )
            # js code is not in place for this, also remove code from view_lot_simple
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
        """Look a lot up the way this auction numbers its lots.

        Split out of ``post`` (unchanged behaviour) so the command palette's ``undo_sale`` action
        finds lots by exactly the same rule the Undo button does.
        """
        if not lot_number:
            return None
        if self.auction.use_seller_dash_lot_numbering:
            return self.auction.lots_qs.filter(custom_lot_number=lot_number).first()
        return self.auction.lots_qs.filter(lot_number_int=lot_number).first()

    def unsell(self, undo_lot):
        """Clear the winner on a lot and record why. Returns the view's own result dict.

        Split out of ``post`` (unchanged behaviour) so the command palette's ``undo_sale`` action
        produces the identical database change and history entry as the Undo button.
        """
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
            # this might need changing for online auctions
            # but as it is now, this view is only ever called for in-person auctions
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
    """Record what the app's voice recognition heard on the set-winners page, and any correction.

    The page writes this, not the app, because the page is the only side that sees both halves: the
    app tells it what it heard and what it matched, and the page is where the operator then fixes a
    wrong bidder number before saving. Posting the returned ``id`` back with ``corrected_to`` lands
    the correction on the same row.

    This is the whole reason voice can be tuned at all. The first version's fatal flaw wasn't the
    speech engine — it was having no record of *what* it misheard, which left grammar changes as
    guesswork. Every row with a ``corrected_to`` names a word to fix in the Voice grammar admin.

    A post with no ``slot`` is the utterance that matched nothing, which is the row we most needed
    and never had: a log of accepted commands can only return words that already work. Those are
    rate-limited per session in :func:`auctions.voice.log_unmatched`, because a continuous
    recognizer hears the room and would otherwise file a transcript of the whole auction.

    Admin-only via ``AuctionViewMixin`` (which raises PermissionDenied for non-admins), and
    fire-and-forget from the page: form-encoded in, ``{"id": <pk>}`` out (``null`` when the row was
    dropped), and never an error that could interrupt a sale.
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


def notify_watchers_lot_selling_soon(lot, request_user=None, position=None):
    """Send a "coming up soon" or "about to be sold" web push to a lot's watchers.

    Two phases, each deduped once per lot, sharing one notification tag so the second overwrites the
    first on the device rather than stacking a duplicate alert:

    - **Coming up soon** (``position`` given and > 1): fired while the lot sits at position 2-10 of
      the in-person queue. Deduped via ``Lot.coming_up_push_sent``.
    - **About to be sold** (``position`` is 1 or None): fired when the lot reaches the head of the
      queue, or is pulled up on the set-winners screen (``position=None``). Deduped via
      ``Lot.selling_push_notification_sent``. This fires even after the coming-up push and, sharing
      the tag, overwrites it -- so a watcher who saw "coming up soon" now sees "about to be sold".

    ``request_user`` (the admin viewing/projecting the lot) is excluded so their own screen doesn't
    light up. Returns True when a push pass actually ran, False when skipped as a dedupe. The
    transient websocket "about to be sold" chat message is handled by the caller, not here.

    Delivery is per watcher: anyone who can receive an app notification gets it there *only*, and
    their browser subscription is skipped -- we can't tell a phone's browser apart from the app
    installed on that same phone, so sending both would buzz one person twice for one lot."""
    if not lot or lot.sold or not lot.auction:
        return False
    coming_up = position is not None and position > 1
    if coming_up:
        # Don't downgrade to "coming up" once the stronger "about to be sold" push already went out.
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
        # it would be awkward to have notifications pop up when you're projecting an image of the lot
        watchers = watchers.exclude(user=request_user)
    lot_url = "https://" + lot.full_lot_link
    # Shared by both delivery paths so the "about to be sold" alert replaces the earlier
    # "coming up soon" one on the device instead of stacking a second alert.
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
            # The push endpoint is invalid or unreachable; remove the stale subscription
            # and record the failure in the auction history so admins can see it.
            # Note: django-webpush only auto-deletes on HTTP 410, but FCM uses
            # HTTP 404 for expired tokens, so we must also handle that here.
            push_info.delete()
            AuctionHistory.objects.create(
                auction=lot.auction,
                user=None,
                action=f"push notification error occurred for {watch.user.username}",
                applies_to="USERS",
            )
    return True


def broadcast_queue_update(auction):
    """Poke the admin auction websocket group so any open Lot queue / kiosk screen re-fetches.

    Fires after every queue mutation (add/remove/reorder/pop-on-sale), so the projector/kiosk
    view advances to the next lot in real time as winners are set on another device -- no waiting
    on the slow htmx poll fallback. Best-effort: a channel-layer hiccup must not fail the mutation."""
    try:
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"auctions_{auction.pk}",
            {"type": "queue_updated"},
        )
    except Exception:
        logger.exception("Failed to send queue_updated websocket for auction %s", auction.pk)


def process_queue_notifications(auction):
    """Notify watchers of any lot now in the top 10 of the queue, and poke any open queue/kiosk
    screens to refresh over the websocket.

    Each lot at position 2-10 gets one "coming up soon" push; the head lot (position 1) gets the
    "about to be sold" push, which overwrites the coming-up one. Both dedupe on the per-lot flags
    (Lot.coming_up_push_sent / Lot.selling_push_notification_sent), so re-running this after every
    queue mutation (add/remove/reorder/pop-on-sale) never double-notifies.

    Watcher notifications honour the auction's message_users_when_lots_sell setting, the same gate
    the set-lot-winners screen uses -- turning it off also hides the opt-in on the lot page, so an
    auction that opted out must not notify from the queue either. The websocket poke is unrelated to
    that setting and always fires, otherwise the kiosk would stop following the queue."""
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
    """Remove a lot's queue entry, wherever it sits, and re-run notifications for the new top.

    Used when a lot is sold / ended on the set-winners page so it drops out of the queue."""
    if lot is None:
        return
    deleted, _ = LotQueueEntry.objects.filter(auction=auction, lot=lot).delete()
    if deleted:
        process_queue_notifications(auction)


class LotQueueMixin(LoginRequiredMixin, AuctionViewMixin):
    """Shared helpers for the in-person "Lot queue" tool.

    The queue is an ordered list of lots about to be sold (LotQueueEntry). Admins build it by
    scanning lot QR codes / typing lot numbers on the queue page; the set-lot-winners page pulls
    the head of the queue automatically. This is an in-person-only feature."""

    club_sidebar_can_view = False  # full-screen tool; sidebar would waste space

    def dispatch(self, request, *args, **kwargs):
        # Let LoginRequiredMixin redirect anonymous users to login before we run any auction lookup.
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        # get_auction runs the admin permission check (raises PermissionDenied for non-admins).
        self.get_auction(kwargs.get("slug", ""))
        if self.auction and self.auction.is_online:
            msg = "The lot queue is only available for in-person auctions"
            raise Http404(msg)
        return super().dispatch(request, *args, **kwargs)

    def queue_entries(self):
        return list(LotQueueEntry.objects.filter(auction=self.auction).select_related("lot").order_by("order"))

    def resolve_lot_from_value(self, value):
        """Turn a scanned value (a full/partial lot QR URL) or a typed lot number into a Lot.

        Returns (Lot or None, error string or None)."""
        value = (value or "").strip()
        if not value:
            return None, "Enter or scan a lot number"
        # A lot QR code is https://{domain}/qr/{pk}/ -- a USB scanner types the whole URL.
        qr_match = re.search(r"/qr/(\d+)", value)
        if qr_match:
            lot = self.auction.lots_qs.filter(pk=qr_match.group(1)).first()
            if not lot:
                return None, "That lot is not part of this auction"
            return lot, None
        # Otherwise treat it as a typed lot number, using this auction's numbering scheme.
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
        """Add a lot to the end of the queue. Returns an error string, or None on success."""
        if not lot:
            return "No lot found"
        if lot.sold:
            return f"Lot {lot.lot_number_display} has already been sold"
        if LotQueueEntry.objects.filter(auction=self.auction, lot=lot).exists():
            return f"Lot {lot.lot_number_display} is already in the queue"
        max_order = LotQueueEntry.objects.filter(auction=self.auction).aggregate(m=Max("order"))["m"] or 0
        LotQueueEntry.objects.create(auction=self.auction, lot=lot, order=max_order + 1, added_by=self.request.user)
        # Sticky flag for the "how much was the queue used" auction stat: never unset, even after the
        # entry is removed or the lot sells.
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
        # Any entries the client didn't mention keep going after, preserving their relative order.
        for entry in sorted(entries.values(), key=lambda e: e.order):
            entry.order = order
            entry.save(update_fields=["order"])
            order += 1
        process_queue_notifications(self.auction)

    def render_list(self, error=None):
        context = {"auction": self.auction, "entries": self.queue_entries(), "error": error}
        return render(self.request, "auctions/lot_queue_list.html", context)


class LotQueueView(LotQueueMixin, TemplateView):
    """The Lot queue page: scan/type lots to build an ordered queue, drag to reorder, remove.

    GET renders the full page (or just the list partial with ?partial=list). POST handles the
    htmx-style mutations add/remove/reorder (returning the refreshed list partial) and the
    scanner add path (lot_pk present -> JSON, for the USB HID / camera pipeline)."""

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
        # Threaded into the (club-only) ribbon barcode_scanner.html include so lot QR scans on a club
        # auction build the queue; the no-club path wires the same URL up itself in the template.
        context["barcode_lot_scan_url"] = self.request.path
        return context

    def post(self, request, *args, **kwargs):
        # Scanner (USB HID / camera) path: adds by lot pk and expects a JSON reply.
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
    """Kiosk (projector) partial: the current head lot rendered big plus the next few queued lots.

    Re-fetched by the queue page over the admin auction websocket (queue_updated) as lots are sold
    on another device, with a slow htmx poll as a fallback. Renders the head lot with
    view_lot_simple.html WITHOUT ViewLotSimple's notification side effect."""

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
    """AuctionTOS rows we can ask for help: people we can reach in the app *right now*.

    A volunteer request is push-only -- an email asking someone to help carry tanks is useless by
    the time it's read -- so the audience is exactly the people holding a device with a live push
    token, not everyone who ever installed the app. Someone who installed it and denied
    notifications, or signed out (which clears the token), is not reachable and is not counted.

    In check-in-mode auctions this is further limited to people who have checked in, which is the
    only proximity signal available: auto-check-in fires inside a ~500 ft geofence, so a checked-in
    person is genuinely at the venue. Without check-in mode there is nothing to tell who is in the
    room, so everyone who joined and has the app is asked -- the volunteers page warns admins about
    exactly that."""
    from auctions.notifications import push_configured

    if not push_configured():
        # Nothing can be delivered, so nobody is reachable. Being honest here keeps the page's
        # "N reachable" count from promising an audience that doesn't exist.
        return AuctionTOS.objects.none()
    qs = AuctionTOS.objects.filter(auction=auction, user__isnull=False)
    if auction.use_check_in_mode:
        qs = qs.filter(checked_in__isnull=False)
    # Exists() rather than a join filter: `.filter(devices__push_enabled=True).exclude(devices__
    # fcm_token="")` spans two joins and would drop anyone owning *any* tokenless device.
    live_device = MobileDevice.objects.filter(user=OuterRef("user"), push_enabled=True).exclude(fcm_token="")
    return qs.filter(Exists(live_device))


def volunteer_helper_count(auction):
    """How many people will actually receive the push (the tooltip count).

    Counted per user, not per TOS row, so a duplicate TOS record can't inflate it -- this has to
    match what notify_volunteers_of_job really sends."""
    return volunteer_eligible_tos(auction).values("user").distinct().count()


def _volunteer_job_url(job):
    from django.contrib.sites.models import Site

    domain = Site.objects.get_current().domain
    path = reverse("auction_volunteer_job", kwargs={"slug": job.auction.slug, "job_pk": job.pk})
    return f"https://{domain}{path}"


# Fixed and short so it survives the notification tray on both platforms: an auction title in the
# title pushes "needs help" past the truncation point, which is the one word that has to be read.
VOLUNTEER_PUSH_TITLE = "Auction help needed"


def _volunteer_notification_text(job):
    body = job.description
    if job.bounty:
        body += f" (${job.bounty:.0f} bounty)"
    return VOLUNTEER_PUSH_TITLE, body


def notify_volunteers_of_job(job):
    """Fan out a job announcement to every helper we can reach in the app.

    Push-only, with no email fallback: this is a "someone is needed in this room now" message, and
    an email that lands after the auction is over is worse than nothing. volunteer_eligible_tos
    already restricts the audience to people who can actually receive it. Uses a per-job collapse
    tag so the later 'filled' retract can target it."""
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
    """Retract a job's announcement once it fills or is canceled (per-job collapse tag).

    The app-side data-only handler that cancels the displayed notification is future FCM work (see
    PUSH.md); until then the accept page is the source of truth — a stale tap is told the job is full.
    First-come-first-serve is enforced at signup time, never by the notification, so this is
    best-effort by design."""
    logger.info("Volunteer job %s filled/canceled; retracting its announcement (tag volunteer_job_%s)", job.pk, job.pk)


class AuctionVolunteers(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Admin ribbon page: ask app users for help with a job, and review past jobs. In-person only."""

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
    """The accept page a job notification opens: any joined user can sign up while spots remain."""

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
            # Lock the job row so two people racing for the last spot can't both win.
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
