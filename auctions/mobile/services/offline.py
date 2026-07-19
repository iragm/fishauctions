"""Offline-mode service for the mobile app's in-person sale screens.

Backs GET /api/mobile/offline/snapshot/ and POST /api/mobile/offline/sync/. The Flutter app keeps
running an in-person sale while disconnected — listing users with their total bought, adding users,
adding lots, and setting lot winners — as native screens that mirror the web pages
(``/auctions/<slug>/users/``, the add-user modal, bulk add lots, ``/lots/set-winners/``). Offline
changes queue locally and push here when the connection returns.

Design: the app is a dumb queue + display. All id assignment, validation and conflict rules live
here; the server copy always wins. A conflicted op is never applied — the admin resolves it on the
website. Applied ops are recorded in :class:`~auctions.models.MobileOfflineOp` so a resent queue
(dropped response) returns ``already_applied`` instead of duplicating rows, and so a later op can
reference an offline-created row by ``op:<op_id>``.

This module intentionally re-implements the same effects as the web views it mirrors
(:class:`auctions.views.AuctionTOSAdmin`, :class:`auctions.views.BulkAddLots`,
:class:`auctions.views.DynamicSetLotWinner`) rather than calling them, because those are
request/response views tied to sessions, forms and the in-person lot queue. Keep the two in sync.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Invoice,
    Lot,
    LotHistory,
    MobileOfflineOp,
    PickupLocation,
)

logger = logging.getLogger(__name__)

# The app chunks; a single sync call applies at most this many ops (a larger batch is a 400).
MAX_OPS_PER_SYNC = 500


# ---------------------------------------------------------------------------
# "Last admin auction" resolution + snapshot
# ---------------------------------------------------------------------------


def _admin_auction_candidates(user):
    """Non-deleted auctions ``user`` might administer, newest first.

    A superset of what :meth:`Auction.permission_check` allows (every True branch of it is covered:
    creator, is_admin AuctionTOS, or club admin/manage-auctions), so callers can filter the result
    through ``permission_check`` to get the exact set. Superusers pass every auction, so we hand back
    all non-deleted auctions for them.
    """
    qs = Auction.objects.filter(is_deleted=False).order_by("-date_start")
    if user.is_superuser:
        return qs
    admin_tos_auction_ids = AuctionTOS.objects.filter(is_admin=True, user=user).values_list("auction_id", flat=True)
    # Club paths only grant permission when the auction is club-managed; permission_check enforces
    # that, so including these clubs' auctions here (some of which may not be club-managed) is fine —
    # the caller re-checks each candidate with permission_check.
    from auctions.models import ClubMember

    club_ids = (
        ClubMember.objects.filter(user=user, is_deleted=False)
        .filter(Q(permission_admin=True) | Q(permission_manage_auctions=True))
        .values_list("club_id", flat=True)
    )
    return qs.filter(Q(created_by=user) | Q(pk__in=admin_tos_auction_ids) | Q(club_id__in=club_ids))


def get_last_admin_auction(user):
    """The caller's "last admin auction", or ``None`` when they administer nothing.

    ``userdata.last_auction_used`` when the caller passes ``permission_check`` for it; otherwise the
    most recent (by ``date_start``) non-deleted auction the caller can administer.
    """
    userdata = getattr(user, "userdata", None)
    last = getattr(userdata, "last_auction_used", None) if userdata else None
    if last and not last.is_deleted and last.permission_check(user):
        return last
    for auction in _admin_auction_candidates(user).iterator():
        if auction.permission_check(user):
            return auction
    return None


def _invoice_status_by_tos(auction):
    """Map ``auctiontos_user_id`` → latest invoice status for the auction, in one query.

    Mirrors ``AuctionTOS.invoice`` (latest by ``-date``) without an N+1 across the users list.
    """
    status_by_tos = {}
    for tos_id, tos_status in (
        Invoice.objects.filter(auctiontos_user__auction=auction)
        .order_by("auctiontos_user_id", "-date")
        .values_list("auctiontos_user_id", "status")
    ):
        # First row seen per user is the latest (‑date), so setdefault keeps it.
        status_by_tos.setdefault(tos_id, tos_status)
    return status_by_tos


def _lot_number_display(auction, lot):
    """The lot's display number as a string, without touching ``lot.auction`` (avoids an N+1).

    Same rule as ``Lot.lot_number_display`` but with the already-loaded ``auction``.
    """
    if auction.use_seller_dash_lot_numbering and lot.custom_lot_number:
        return str(lot.custom_lot_number)
    if lot.lot_number_int:
        return str(lot.lot_number_int)
    return str(lot.lot_number)


def build_snapshot(auction):
    """The compact per-auction payload the offline screens need, or ``{"auction": None}``.

    ``users`` are every AuctionTOS ordered by name (matches the web users page); ``lots`` are every
    non-deleted, non-banned lot. No images, no pagination — a bounded per-auction payload.
    """
    if auction is None:
        return {"auction": None}

    status_by_tos = _invoice_status_by_tos(auction)
    users = []
    for tos in AuctionTOS.objects.filter(auction=auction).order_by("name"):
        users.append(
            {
                "pk": tos.pk,
                "bidder_number": tos.bidder_number or "",
                "name": tos.name or "",
                "email": tos.email or "",
                "phone_number": tos.phone_number or "",
                "invoice_status": status_by_tos.get(tos.pk, "NONE"),
            }
        )

    lots = []
    for lot in Lot.objects.filter(auction=auction, is_deleted=False, banned=False).order_by("lot_number"):
        lots.append(
            {
                "pk": lot.pk,
                "lot_number": _lot_number_display(auction, lot),
                "lot_name": lot.lot_name,
                "quantity": lot.quantity,
                "donation": lot.donation,
                "seller_pk": lot.auctiontos_seller_id,
                "winner_pk": lot.auctiontos_winner_id,
                "winning_price": str(lot.winning_price) if lot.winning_price is not None else None,
                "active": lot.active,
            }
        )

    return {
        "auction": {
            "slug": auction.slug,
            "title": auction.title,
            "currency_symbol": auction.currency_symbol,
            "use_seller_dash_lot_numbering": auction.use_seller_dash_lot_numbering,
            "only_whole_dollar_bids": auction.only_whole_dollar_bids,
            "date_start": auction.date_start,
        },
        "users": users,
        "lots": lots,
        "generated_at": timezone.now(),
    }


# ---------------------------------------------------------------------------
# Sync — apply a batch of queued ops
# ---------------------------------------------------------------------------


class _OpApplier:
    """Applies one batch of offline ops against ``auction`` for the syncing ``user``, in order.

    Instances are single-use per sync call. ``created_rows`` maps an ``op_id`` created earlier in
    this batch to the resulting object, so ``op:<op_id>`` references resolve before the ledger row
    is even needed.

    Each ``_apply_*`` handler returns ``(status, payload)``: on a conflict the payload carries
    ``conflict`` + ``message`` and nothing is mutated (the server copy always wins); on
    applied/already_applied the payload carries the echoed numbers and the ledger row is recorded.
    """

    def __init__(self, auction, user):
        self.auction = auction
        self.user = user
        self.created_rows = {}  # op_id -> AuctionTOS | Lot created in this batch

    @staticmethod
    def _conflict(conflict, message):
        """A per-op conflict result: not applied, surfaced to the app for the admin to resolve."""
        return "conflict", {"conflict": conflict, "message": message}

    # -- reference resolution -------------------------------------------------

    def _resolve_op_ref(self, ref, op_type, model):
        """Resolve an ``op:<op_id>`` reference to the row created by an earlier op, or None.

        Looks in this batch first, then the persisted ledger. A reference to an op that conflicted
        (never recorded) resolves to None → the referencing op reports ``not_found``.
        """
        op_id = ref[len("op:") :]
        row = self.created_rows.get(op_id)
        if row is not None:
            return row if isinstance(row, model) else None
        led = MobileOfflineOp.objects.filter(op_id=op_id, auction=self.auction, op_type=op_type).first()
        if led and led.result_pk:
            return model.objects.filter(pk=led.result_pk).first()
        return None

    def resolve_user(self, ref):
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith("op:"):
            return self._resolve_op_ref(ref, "add_user", AuctionTOS)
        return AuctionTOS.objects.filter(auction=self.auction, bidder_number=ref).order_by("-createdon").first()

    def resolve_lot(self, ref):
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith("op:"):
            return self._resolve_op_ref(ref, "add_lot", Lot)
        # Display-number lookup mirrors DynamicSetLotWinner.validate_lot.
        if self.auction.use_seller_dash_lot_numbering:
            return self.auction.lots_qs.filter(custom_lot_number=ref).first()
        try:
            number = int(ref)
        except (TypeError, ValueError):
            return None
        return self.auction.lots_qs.filter(lot_number_int=number).first()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _invoice_blocks(tos):
        """True when this user has an invoice that is no longer open (UNPAID/PAID)."""
        invoice = tos.invoice if tos else None
        return bool(invoice and invoice.status != "DRAFT")

    def _record(self, op_id, op_type, result_pk, echo):
        """Persist the applied-op ledger row so replays dedupe and later ops can reference it."""
        MobileOfflineOp.objects.create(
            op_id=op_id,
            auction=self.auction,
            user=self.user,
            op_type=op_type,
            result_pk=result_pk,
            result_data=echo,
        )

    # -- op handlers ----------------------------------------------------------

    def _apply_add_user(self, op):
        requested = (op.get("bidder_number") or "").strip()
        name = (op.get("name") or "").strip()
        existing = None
        if requested:
            existing = (
                AuctionTOS.objects.filter(auction=self.auction, bidder_number=requested).order_by("-createdon").first()
            )
        if existing:
            # Same number + same name = the same person double-entered (idempotent); different name =
            # someone claimed that number on the server meanwhile (a real conflict).
            if (existing.name or "").strip().casefold() == name.casefold():
                echo = {"bidder_number": existing.bidder_number}
                self._record(op["op_id"], "add_user", existing.pk, echo)
                self.created_rows[op["op_id"]] = existing
                return "already_applied", {**echo}
            return self._conflict(
                "user_conflict",
                f"Bidder number {requested} is already used by {existing.name or 'another user'} on the server",
            )

        pickup = PickupLocation.objects.filter(auction=self.auction).order_by("-is_default", "pk").first()
        if not pickup:
            return self._conflict("not_found", "This auction has no pickup location to add a user to")

        tos = AuctionTOS(
            auction=self.auction,
            pickup_location=pickup,
            manually_added=True,
            bidder_number=requested,
            name=name,
            email=(op.get("email") or "").strip(),
            phone_number=(op.get("phone_number") or "").strip(),
        )
        tos.save()  # AuctionTOS.save() auto-assigns a free bidder_number when the requested one is blank
        self.auction.create_history(applies_to="USERS", action=f"Added {name}", user=self.user)
        echo = {"bidder_number": tos.bidder_number}
        self._record(op["op_id"], "add_user", tos.pk, echo)
        self.created_rows[op["op_id"]] = tos
        return "applied", {**echo}

    def _apply_add_lot(self, op):
        seller = self.resolve_user(op.get("seller"))
        if seller is None:
            return self._conflict("not_found", "Seller not found")
        if self._invoice_blocks(seller):
            return self._conflict("invoice_not_open", f"{seller.name or 'The seller'}'s invoice is not open")

        requested = (op.get("lot_number") or "").strip()
        try:
            quantity = int(op.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1
        if quantity < 1:
            quantity = 1

        lot = Lot(
            lot_name=(op.get("lot_name") or "")[:40],
            quantity=quantity,
            donation=bool(op.get("donation")),
            auctiontos_seller=seller,
            auction=self.auction,
            added_by=self.user,
            user=seller.user,
        )
        # Honor the requested display number when it is still free; otherwise leave it unset so
        # Lot.save() assigns the next free one (the remap the echo reports back to the app).
        if requested:
            if self.auction.use_seller_dash_lot_numbering:
                if not self.auction.lots_qs.filter(custom_lot_number=requested).exists():
                    lot.custom_lot_number = requested
            else:
                try:
                    number = int(requested)
                except (TypeError, ValueError):
                    number = None
                if number is not None and not Lot.objects.filter(auction=self.auction, lot_number_int=number).exists():
                    lot.lot_number_int = number
        lot.save()

        invoice = Invoice.objects.filter(auctiontos_user=seller, auction=self.auction).first()
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=seller, auction=self.auction)
        invoice.recalculate()
        self.auction.create_history(applies_to="LOTS", action=f"Bulk added 1 lots for {seller.name}", user=self.user)

        echo = {"lot_number": _lot_number_display(self.auction, lot)}
        self._record(op["op_id"], "add_lot", lot.pk, echo)
        self.created_rows[op["op_id"]] = lot
        return "applied", {**echo}

    def _apply_set_winner(self, op):
        lot = self.resolve_lot(op.get("lot"))
        if lot is None:
            return self._conflict("not_found", "Lot not found")

        server_sold = bool(lot.auctiontos_winner_id and lot.winning_price is not None)

        if op.get("unsold"):
            if server_sold:
                return self._conflict("winner_conflict", self._server_sold_message(lot))
            if self._invoice_blocks(lot.auctiontos_seller):
                return self._conflict("invoice_not_open", "The seller's invoice is not open")
            self._end_unsold(lot)
            self._record(op["op_id"], "set_winner", None, {})
            return "applied", {}

        winner = self.resolve_user(op.get("winner"))
        if winner is None:
            return self._conflict("not_found", "Bidder not found")
        price = self._parse_price(op.get("winning_price"))
        if price is None:
            return self._conflict("invalid_price", "A valid winning price is required")

        if server_sold:
            # The server copy wins: same winner + price is an idempotent no-op, anything else is a
            # conflict the admin resolves on the website. Either way we do NOT mutate the row.
            if lot.auctiontos_winner_id == winner.pk and lot.winning_price == price:
                self._record(op["op_id"], "set_winner", None, {})
                return "already_applied", {}
            return self._conflict("winner_conflict", self._server_sold_message(lot))

        if self._invoice_blocks(lot.auctiontos_seller):
            return self._conflict("invoice_not_open", "The seller's invoice is not open")
        if self._invoice_blocks(winner):
            return self._conflict("invoice_not_open", f"Bidder {winner.bidder_number}'s invoice is not open")

        self._set_winner(lot, winner, price)
        self._record(op["op_id"], "set_winner", None, {})
        return "applied", {}

    # -- effects (mirror DynamicSetLotWinner) ---------------------------------

    def _parse_price(self, raw):
        try:
            price = Decimal(str(raw)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if self.auction.only_whole_dollar_bids and price != price.to_integral_value():
            return None
        return price

    def _server_sold_message(self, lot):
        symbol = self.auction.currency_symbol
        bidder = lot.auctiontos_winner.bidder_number if lot.auctiontos_winner else "?"
        return (
            f"Lot {_lot_number_display(self.auction, lot)} was already sold to bidder "
            f"{bidder} for {symbol}{lot.winning_price} on the server"
        )

    def _end_unsold(self, lot):
        """Mirror DynamicSetLotWinner.end_unsold: mark unsold, history, websocket."""
        lot.date_end = timezone.now()
        lot.winner = None
        lot.auctiontos_winner = None
        lot.winning_price = None
        lot.active = False
        lot.save()
        message = f"{self.user} has marked lot {lot.lot_number_display} as not sold"
        LotHistory.objects.create(lot=lot, user=self.user, message=message, changed_price=True)
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
        self.auction.create_history(
            applies_to="LOTS",
            action=f"Marked lot {lot.lot_number_display} as ended without being sold",
            user=self.user,
        )

    def _set_winner(self, lot, winning_tos, winning_price):
        """Mirror DynamicSetLotWinner.set_winner: winner, check-in side effects, history, bap."""
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
                user=self.user,
            )
        try:
            lot.add_winner_message(self.user, winning_tos, winning_price)
        except Exception:
            logger.exception("add_winner_message failed for lot %s", lot.pk)
        if lot.auction and lot.auction.club and not lot.bap_points_awarded and not lot.manually_approved:
            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
        self.auction.create_history(
            applies_to="LOTS",
            action=f"Set lot {lot.lot_number_display} as sold",
            user=self.user,
        )

    # -- dispatch -------------------------------------------------------------

    _HANDLERS = {
        "add_user": "_apply_add_user",
        "add_lot": "_apply_add_lot",
        "set_winner": "_apply_set_winner",
    }

    def apply_one(self, op):
        """Apply a single op and return its result dict (never raises for a per-op problem)."""
        op_id = op.get("op_id")
        result = {"op_id": op_id}

        # Idempotent replay: a recorded op_id returns its original numbers with an already_applied
        # status instead of re-running (the phone resent the queue after a dropped response).
        led = MobileOfflineOp.objects.filter(op_id=op_id).first()
        if led:
            result.update(led.result_data or {})
            result["status"] = "already_applied"
            return result

        handler_name = self._HANDLERS.get(op.get("type"))
        if handler_name is None:
            result.update(status="conflict", conflict="not_found", message="Unknown op type")
            return result

        try:
            # One savepoint per op: a conflict returns before mutating (nothing to roll back), and an
            # unexpected mid-apply failure rolls back just this op without aborting the whole batch.
            with transaction.atomic():
                status, payload = getattr(self, handler_name)(op)
        except Exception:
            logger.exception("Offline op %s (%s) failed unexpectedly", op_id, op.get("type"))
            result.update(status="conflict", conflict="error", message="This change could not be applied")
            return result

        result.update(payload)
        result["status"] = status
        return result


def apply_ops(auction, user, ops):
    """Apply a batch of queued offline ops in order; return per-op results (never all-or-nothing).

    Each op is independent: a conflict on one leaves the rest to apply (except ops that reference a
    conflicted op's row, which resolve to ``not_found``). Duplicate op_ids in the same batch, and
    ops already in the ledger, return ``already_applied``.
    """
    applier = _OpApplier(auction, user)
    results = []
    for op in ops:
        results.append(applier.apply_one(op if isinstance(op, dict) else {}))
    return results
