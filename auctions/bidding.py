"""Bidding logic for lots.

Kept in its own module on purpose: not in views.py (already ~8k lines) and no
longer in consumers.py, where a stalled lot websocket used to be able to silently
lose a bid. The HTTP bid view (views.PlaceBid) calls place_bid_and_broadcast; the
websocket-message half (broadcast_bid_result) stays in consumers.py.
"""

import datetime
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.sites.models import Site
from django.db import transaction
from django.utils import timezone
from post_office import mail

from .consumers import broadcast_bid_result, check_all_permissions
from .models import Bid, Invoice, Lot, LotHistory, UserInterestCategory

logger = logging.getLogger(__name__)


def check_bidding_permissions(lot, user):
    """
    Returns false if everything is OK, or a string error message
    call check_all_permissions first
    """
    if lot.ended:
        return "Bidding on this lot has ended"
    if lot.winner or lot.auctiontos_winner:
        return "This lot has already been sold"
    if lot.user and lot.user.pk == user.pk:
        return "You can't bid on your own lot"
    if lot.auction:
        tos = lot.auction.tos_for_user(user)
        if not tos:
            return "You haven't joined this auction"
        if lot.auctiontos_seller_id and lot.auctiontos_seller_id == tos.pk:
            # admin-added lots often have no lot.user set, so the check above misses them;
            # match the bidder's TOS against the seller's TOS instead
            return "You can't bid on your own lot"
        if tos.requires_check_in_before_bidding:
            # Check-in mode: joining is not enough -- the member must be checked in at the
            # event before bidding is enabled. Enforced here (not just via bidding_allowed)
            # so no join/import path can hand out bidding without a check-in.
            return "You must check in at the event before you can bid"
        if not tos.bidding_allowed:
            return "This auction requires admin approval before you can bid"
    # timing rules (auction started, online bidding windows for in-person auctions,
    # very new lots, deactivated lots) all live in one place: Lot.bidding_error
    return lot.bidding_error


def reset_lot_end_time(lot):
    """When bid are placed at the last minute on a lot, we need to bump up the end time.  Call this function any time a bid that changes the price is placed on a lot.
    Return ms-format timestamp for use on the lot page"""
    if lot.within_dynamic_end_time:
        new_end_time = timezone.now() + datetime.timedelta(minutes=15)
        if new_end_time > lot.hard_end:
            new_end_time = lot.hard_end
        if lot.date_end != new_end_time:
            lot.date_end = new_end_time
            lot.save()
            # ms - to be parsed by js and set to local time on view_lot_images.html
            return int(lot.date_end.timestamp() * 1000)
    return None


def bid_on_lot(lot, user, amount):
    """
    Check permissions to make sure the user isn't banned before calling this function
    this will return the following:
    {
        "type": "INFO", # ERROR, INFO, NEW_HIGH_BID, NEW_HIGH_BIDDER, LOT_END_WINNER, ENDED_NO_WINNER
        "message": "string",
        "send_to": 'user' or 'everyone'
        "high_bidder_pk": 1 # pk of high bidder (or winner) or None.  Used to update DOM
        "high_bidder_name": 'user' # name of high bidder or None.  Used to update DOM
        "current_high_bid": 5 # current bid or None.  Can be the winning price.  Used to update DOM
        }
    """
    try:
        # if True:
        try:
            amount_decimal = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            result = {
                "type": "ERROR",
                "message": "Invalid bid amount",
                "send_to": "user",
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
                "winner": None,
                "date_end": None,
            }
            return result
        # Reject bids with more than 2 decimal places
        if amount_decimal != amount_decimal.quantize(Decimal("0.01")):
            result = {
                "type": "ERROR",
                "message": "Bids can have at most 2 decimal places",
                "send_to": "user",
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
                "winner": None,
                "date_end": None,
            }
            return result
        amount = amount_decimal.quantize(Decimal("0.01"))
        if amount <= 0:
            result = {
                "type": "ERROR",
                "message": "Bid must be greater than zero",
                "send_to": "user",
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
                "winner": None,
                "date_end": None,
            }
            return result
        if lot.auction and lot.auction.only_whole_dollar_bids:
            if amount != amount.to_integral_value():
                result = {
                    "type": "ERROR",
                    "message": "This auction only allows whole dollar bids",
                    "send_to": "user",
                    "high_bidder_pk": None,
                    "high_bidder_name": None,
                    "current_high_bid": None,
                    "winner": None,
                    "date_end": None,
                }
                return result
        if lot.reserve_price and amount < lot.reserve_price:
            # Without this, an under-reserve first bid would be saved and announced, but
            # filtered out of lot.bids (amount__gte=reserve_price) -- a ghost bid that can
            # never win and breaks the raise-your-own-bid path.
            result = {
                "type": "ERROR",
                "message": f"You have to bid at least ${lot.reserve_price}",
                "send_to": "user",
                "high_bidder_pk": None,
                "high_bidder_name": None,
                "current_high_bid": None,
                "winner": None,
                "date_end": None,
            }
            return result
        result = {
            "type": "ERROR",
            "message": "Override this message",
            "send_to": "user",
            "high_bidder_pk": None,
            "high_bidder_name": None,
            "current_high_bid": None,
            "winner": None,
            "date_end": None,
        }
        if lot.ended:
            result["message"] = "Bidding has ended"
            return result
        if lot.bidding_error:
            result["message"] = lot.bidding_error
            return result
        if lot.winner or lot.auctiontos_winner:
            result["message"] = "This lot has already been sold"
            return result
        auction_tos = lot.auction.tos_for_user(user) if lot.auction else None
        if auction_tos:
            # resolve the invoice through the TOS (not auctiontos_user__user=user) so users
            # whose TOS is matched by email, without a linked account, don't bypass this
            invoice = Invoice.objects.filter(auctiontos_user=auction_tos, auction=lot.auction).first()
            if invoice and invoice.status != "DRAFT":
                result["message"] = (
                    "Your invoice for this auction is not open.  An administrator can reopen it and allow you to bid."
                )
                return result
        if (
            lot.auction
            and not lot.auction.is_online
            and lot.auction.online_bidding == "buy_now_only"
            and lot.buy_now_price
        ):
            if amount < lot.buy_now_price:
                result["message"] = "This auction does not allow bids, you can only buy this lot now."
                return result
        originalHighBidder = lot.high_bidder
        original_bid = lot.high_bid
        existing_bid = (
            Bid.objects.exclude(is_deleted=True).filter(user=user, lot_number=lot).order_by("-bid_time").first()
        )
        created = existing_bid is None
        # Don't persist the bid yet; save it only when the bid is accepted.
        # For sealed bids, the sealed_bid block below creates the record.
        bid = Bid(user=user, lot_number=lot, amount=amount) if created else existing_bid
        # also update category interest, max one per bid (skip lots with no category;
        # UserInterestCategory.category is non-null)
        if lot.species_category:
            UserInterestCategory.add_interest(user, lot.species_category, settings.BID_WEIGHT)
        userData = user.userdata
        userData.has_bid = True
        if userData.username_visible:
            user_string = str(user)
        else:
            user_string = "Anonymous"
        userData.save()
        if lot.sealed_bid:
            bid = Bid(user=user, lot_number=lot, amount=amount, was_high_bid=True)
            bid.save()
            result["type"] = "INFO"
            result["message"] = "Bid placed!  You can change your bid at any time until the auction ends"
            result["send_to"] = "user"
            # result["high_bidder_pk"] = user.pk
            # result["high_bidder_name"] = str(user)
            result["current_high_bid"] = bid.amount
            return result
        else:
            if not created:
                if amount <= bid.amount:
                    result["message"] = f"Bid more than your current bid (${bid.amount})"
                    logger.debug(
                        "%s tried to bid on %s less than their original bid of $%s", user_string, lot, original_bid
                    )
                    return result
                else:
                    # Create a new bid record instead of updating the existing one,
                    # so the old bid is preserved for history
                    bid = Bid(user=user, lot_number=lot, amount=amount)
            # no longer true: from here on, lot.high_bidder and lot.high_bid will include the current bid
            if lot.buy_now_price and not originalHighBidder:
                if bid.amount >= lot.buy_now_price:
                    lot.winner = user
                    if auction_tos:
                        lot.auctiontos_winner = auction_tos
                    lot.winning_price = lot.buy_now_price
                    lot.buy_now_used = True
                    if lot.label_printed:
                        lot.label_printed = False
                        lot.label_needs_reprinting = True
                    # this next line makes the lot end immediately after buy now is used
                    # I have put it in and taken it out a few times now, it is controversial because it causes lots to "disappear" when sold
                    # see also lot.ended - setting this is needed to make buy now lots go into invoices immediately
                    lot.date_end = timezone.now()
                    lot.watch_warning_email_sent = True
                    lot.save()
                    if auction_tos:
                        # Recalculate the buyer AND seller invoices only AFTER the sale is
                        # persisted. Previously this ran before winning_price/auctiontos_winner
                        # were saved, so both invoices recalculated from stale (unsold) DB state
                        # and credited $0 until a later recalculation (e.g. the endauctions cron).
                        lot.create_update_invoices()
                    result["send_to"] = "everyone"
                    result["high_bidder_pk"] = user.pk
                    result["high_bidder_name"] = user_string
                    result["type"] = "LOT_END_WINNER"
                    result["message"] = f"{user_string} bought this lot now!!"
                    result["current_high_bid"] = lot.buy_now_price
                    bid.was_high_bid = True
                    bid.last_bid_time = lot.date_end
                    bid.save()
                    LotHistory.objects.create(
                        lot=lot,
                        user=user,
                        message=result["message"],
                        changed_price=True,
                        current_price=result["current_high_bid"],
                        bid_amount=amount,
                    )
                    return result
            # lot.high_bidder can be False (not just for new lots: raising the reserve
            # above all existing bids disqualifies them), so guard before .pk
            if not originalHighBidder and (created or (lot.high_bidder and lot.high_bidder.pk == user.pk)):
                result["send_to"] = "everyone"
                result["type"] = "NEW_HIGH_BIDDER"
                result["message"] = f"{user_string} has placed the first bid on this lot"
                result["current_high_bid"] = lot.reserve_price
                result["high_bidder_pk"] = user.pk
                result["high_bidder_name"] = user_string
                bid.was_high_bid = True
                LotHistory.objects.create(
                    lot=lot,
                    user=user,
                    message=result["message"],
                    changed_price=True,
                    current_price=result["current_high_bid"],
                    bid_amount=amount,
                )
                bid.last_bid_time = timezone.now()
                bid.save()
                return result
            # bid increments - also set in views.py and in view_lot_images.html
            if lot.auction and not lot.auction.only_whole_dollar_bids:
                # 5% rounded down to nearest cent, minimum $0.01
                min_increment = max(
                    (original_bid * Decimal("0.05")).quantize(Decimal("0.01"), rounding="ROUND_DOWN"),
                    Decimal("0.01"),
                )
            else:
                # 5% rounded down to nearest dollar, minimum $1
                min_increment = max(
                    (original_bid * Decimal("0.05")).to_integral_value(rounding="ROUND_DOWN"),
                    Decimal(1),
                )
            next_allowed_amount = original_bid + min_increment
            # if bid.amount <= original_bid:  # changing this to < would allow bumping without being the high bidder
            if bid.amount < next_allowed_amount:
                # there's a high bidder already
                logger.debug("%s tried to bid on %s less than the current bid of $%s", user_string, lot, original_bid)
                result["message"] = f"You have to bid at least ${next_allowed_amount}"
                return result
            if bid.amount > next_allowed_amount:
                bid.save()
                userData = user.userdata
                userData.has_used_proxy_bidding = True
                userData.save()
            # if we get to this point, the user has bid >= the high bid
            bid.was_high_bid = True
            bid.last_bid_time = timezone.now()
            bid.save()
            if lot.high_bidder.pk == user.pk:
                try:
                    if originalHighBidder.pk == lot.high_bidder.pk:
                        # user is upping their own price, don't tell other people about it
                        result["type"] = "INFO"
                        result["message"] = f"You've raised your proxy bid to ${bid.amount}"
                        logger.debug("%s has raised their bid on %s to $%s", user_string, lot, bid.amount)
                        return result
                except AttributeError:
                    pass
                # New high bidder!  If we get to this point, the user has bid against someone else and changed the price
                result["date_end"] = reset_lot_end_time(lot)
                result["type"] = "NEW_HIGH_BIDDER"
                result["message"] = f"{lot.high_bidder_display} is now the high bidder at ${lot.high_bid}"
                if result["date_end"]:
                    result["message"] += ". End time extended!"
                result["high_bidder_pk"] = lot.high_bidder.pk
                result["high_bidder_name"] = str(lot.high_bidder_display)
                result["current_high_bid"] = lot.high_bid
                result["send_to"] = "everyone"
                LotHistory.objects.create(
                    lot=lot,
                    user=user,
                    message=result["message"],
                    changed_price=True,
                    current_price=result["current_high_bid"],
                    bid_amount=amount,
                )
                # email the old high bidder; best-effort -- the bid is already saved, so a
                # bad email address must not turn this success into a reported failure.
                # originalHighBidder is False (not None) when prior bids were all under a
                # since-raised reserve, in which case there's no one to notify
                if originalHighBidder:
                    try:
                        current_site = Site.objects.get_current()
                        logger.debug("%s has been outbid!", originalHighBidder.username)
                        mail.send(
                            originalHighBidder.email,
                            template="outbid_notification",
                            context={
                                "name": originalHighBidder.first_name,
                                "domain": current_site.domain,
                                "lot": lot,
                            },
                        )
                    except Exception:
                        logger.exception("Failed to queue outbid notification for lot %s", lot.pk)
                return result
            # bumped up against a proxy bid
            result["date_end"] = reset_lot_end_time(lot)
            result["type"] = "NEW_HIGH_BID"
            result["current_high_bid"] = lot.high_bid
            result["message"] = (
                f"{user_string} bumped the price up to ${lot.high_bid}.  {lot.high_bidder_display} is still the high bidder."
            )
            if result["date_end"]:
                result["message"] += "  End time extended!"
            result["send_to"] = "everyone"
            LotHistory.objects.create(
                lot=lot,
                user=user,
                message=result["message"],
                changed_price=True,
                current_price=result["current_high_bid"],
                bid_amount=amount,
            )
            return result
    except Exception as e:
        logger.exception(e)


def _bid_error_result(message):
    """An error result shaped exactly like bid_on_lot()'s return, sent to one user."""
    return {
        "type": "ERROR",
        "message": message,
        "send_to": "user",
        "high_bidder_pk": None,
        "high_bidder_name": None,
        "current_high_bid": None,
        "winner": None,
        "date_end": None,
    }


def place_bid_and_broadcast(lot, user, amount):
    """Place a bid and notify connected clients.

    Persistence (permission checks + bid_on_lot) runs FIRST and is never gated on
    the websocket. The broadcast is best-effort: if the channel layer is down or
    slow, the bid is still saved and we only log the broadcast failure -- so a
    websocket problem can no longer silently drop a bid (the failure mode behind
    branch fix-lot-websocket-reconnect). Returns the bid_on_lot()-shaped result.

    Safe to call from any sync context (e.g. the HTTP bid view).
    """
    if not getattr(user, "is_authenticated", False):
        return _bid_error_result("You must be logged in to bid")
    with transaction.atomic():
        # Serialize concurrent bids on the same lot. Without the row lock, two
        # simultaneous buy-nows can both pass the winner check and both "win"
        # (double-sell, last write wins), and near-simultaneous proxy bids race
        # on the same original high bid. The broadcast stays outside the
        # transaction so clients only ever see committed state.
        lot = Lot.objects.select_for_update().filter(pk=lot.pk, is_deleted=False).first()
        if not lot:
            return _bid_error_result("This lot has been removed")
        error = check_all_permissions(lot, user) or check_bidding_permissions(lot, user)
        if error:
            result = _bid_error_result(error)
        else:
            result = bid_on_lot(lot, user, amount)
            if result is None:
                # bid_on_lot swallows unexpected errors and returns None; surface as an error
                result = _bid_error_result("Something went wrong placing your bid")
    try:
        broadcast_bid_result(lot, user, result)
    except Exception:
        logger.exception(
            "Bid broadcast failed but the bid was still saved (lot=%s user=%s)",
            getattr(lot, "pk", None),
            getattr(user, "pk", None),
        )
    return result
