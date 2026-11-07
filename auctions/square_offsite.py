"""Square payments taken in a connected account without this site: a club's own Payment Link or register.

Square sends them to our webhook because the account is connected. Every charge this site makes names its
invoice (``reference_id``), so a payment with none came from somewhere else. For each one we keep the amount,
time and the buyer's name and email for :data:`RETENTION_DAYS`, and nothing about the order reaches the log.

They are used three ways:

- the members page offers the newest dues-sized payment as "add this person" or "renew this member"
  (:func:`next_dues_payment`, :func:`resolve_dues_payment`);
- an unpaid invoice whose balance matches one says the buyer may have paid already
  (:func:`invoice_payment_hint`);
- a club that opts in (``Club.record_offsite_square_payments``) gets them in its treasurer report.
"""

import logging
from datetime import timedelta
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from auctions.models import Club, ClubHistory, ClubMember, ClubMoney, SquareOffsitePayment

logger = logging.getLogger(__name__)

#: Rows older than this are deleted as new ones arrive: they hold buyers' names and emails.
RETENTION_DAYS = 45
#: How far back a payment is still offered on the members page or against an invoice.
WINDOW_DAYS = 30
#: Square re-sends old payments (a refund fires payment.updated again). Only payments this recent are
#: new to the treasurer report, so turning the option on doesn't book months-old sales.
LEDGER_GRACE = timedelta(days=3)


def square_time(value):
    """A Square RFC 3339 timestamp as an aware datetime, or None."""
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def order_source(order):
    """Where Square says an order came from ("Payment Links", "Square Point of Sale"), or ""."""
    name = getattr(getattr(order, "source", None), "name", None)
    return name if isinstance(name, str) else ""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _buyer(payment, order):
    """(name, email) as Square has them on the payment, or on the order's fulfillment recipient."""
    email = _text(payment.get("buyer_email_address"))
    name = ""
    for key in ("billing_address", "shipping_address"):
        address = payment.get(key) or {}
        name = " ".join(part for part in (_text(address.get("first_name")), _text(address.get("last_name"))) if part)
        if name:
            break
    fulfillments = getattr(order, "fulfillments", None)
    for fulfillment in fulfillments if isinstance(fulfillments, list) else []:
        details = getattr(fulfillment, "shipment_details", None) or getattr(fulfillment, "pickup_details", None)
        recipient = getattr(details, "recipient", None)
        name = name or _text(getattr(recipient, "display_name", None))
        email = email or _text(getattr(recipient, "email_address", None))
    if not name:
        card = ((payment.get("card_details") or {}).get("card")) or {}
        name = _text(card.get("cardholder_name"))
    return name[:200], email[:254]


def _amount(money):
    try:
        return Decimal((money or {}).get("amount", 0)) / 100
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ledger_club(merchant_id):
    """The one club this merchant collects for; None when it is a personal account or shared by clubs."""
    clubs = list(Club.objects.filter(square_seller__square_merchant_id=merchant_id)[:2])
    return clubs[0] if len(clubs) == 1 else None


def _book(club, external_id, amount, date, description):
    """Write or correct the one ledger row for a Square payment or refund. Square re-fires, often
    concurrently, so the club row is locked and the row is found by its Square id.
    """
    with transaction.atomic():
        Club.objects.select_for_update().filter(pk=club.pk).first()
        row = ClubMoney.objects.filter(club=club, external_id=external_id).first()
        if row is None:
            ClubMoney.objects.create(
                club=club,
                external_id=external_id,
                amount=amount,
                category=ClubMoney.CATEGORY_SQUARE_OFFSITE,
                date=date,
                description=description[: ClubMoney.DESCRIPTION_MAX_LENGTH],
            )
        elif row.amount != amount and row.category == ClubMoney.CATEGORY_SQUARE_OFFSITE:
            row.amount = amount
            row.save(update_fields=["amount"])


def record_payment(merchant_id, payment, order):
    """Keep a completed off-site payment, and book it for a club that opted in."""
    payment_id = payment.get("id")
    money = payment.get("total_money") or payment.get("amount_money") or {}
    amount = _amount(money)
    if not payment_id or not merchant_id or amount is None or amount <= 0:
        return
    currency = money.get("currency") or "USD"
    paid_at = square_time(payment.get("created_at")) or timezone.now()
    now = timezone.now()
    # Only a club's account: a personal one's register sales are nobody's business here. And never
    # past retention, which a re-sent old payment would otherwise write back.
    if (
        paid_at < now - timedelta(days=RETENTION_DAYS)
        or not Club.objects.filter(square_seller__square_merchant_id=merchant_id).exists()
    ):
        return
    name, email = _buyer(payment, order)
    source = order_source(order)
    purge()
    SquareOffsitePayment.objects.update_or_create(
        payment_id=payment_id,
        defaults={
            "merchant_id": merchant_id,
            "amount": amount,
            "currency": currency,
            "paid_at": paid_at,
            "buyer_name": name,
            "buyer_email": email,
            "source": source[:100],
        },
    )

    club = _ledger_club(merchant_id)
    if not club or not club.record_offsite_square_payments:
        return
    seller = club.effective_square_seller
    if seller and currency != seller.currency:
        # The ledger has no currency of its own; another currency would add up as if it were this one.
        return
    if paid_at < now - LEDGER_GRACE and not ClubMoney.objects.filter(club=club, external_id=payment_id).exists():
        return
    items = []
    line_items = getattr(order, "line_items", None)
    for line_item in line_items if isinstance(line_items, list) else []:
        item = _text(getattr(line_item, "name", None))
        if item:
            quantity = getattr(line_item, "quantity", None)
            quantity = quantity if isinstance(quantity, str) else "1"
            items.append(item if quantity == "1" else f"{item} x{quantity}")
    description = f"Square ({source})" if source else "Square"
    if items:
        description += ": " + ", ".join(items)
    _book(club, payment_id, amount, timezone.localdate(paid_at), description)


def purge(merchant_id=None):
    """Delete kept payments past retention, or all of a merchant's (its account was disconnected)."""
    if merchant_id:
        return SquareOffsitePayment.objects.filter(merchant_id=merchant_id).delete()
    cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
    return SquareOffsitePayment.objects.filter(paid_at__lt=cutoff).delete()


def record_refund(merchant_id, refund):
    """A completed refund of an off-site payment: hide the payment from suggestions and, if the payment is
    in a treasurer report, take the refund off it there. True when the payment was one of ours to track.
    """
    refund_id = refund.get("id")
    payment_id = refund.get("payment_id")
    if not refund_id or not payment_id or not merchant_id:
        return False
    tracked = SquareOffsitePayment.objects.filter(payment_id=payment_id, merchant_id=merchant_id).update(refunded=True)
    booked = (
        ClubMoney.objects.filter(external_id=payment_id, club__square_seller__square_merchant_id=merchant_id)
        .select_related("club")
        .first()
    )
    amount = _amount(refund.get("amount_money"))
    if booked and amount and amount > 0:
        date = timezone.localdate(square_time(refund.get("created_at")) or timezone.now())
        _book(booked.club, refund_id, -amount, date, "Square refund")
    return bool(tracked or booked)


def _recent(merchant_id, since=None):
    window_start = timezone.now() - timedelta(days=WINDOW_DAYS)
    since = max(since, window_start) if since else window_start
    return SquareOffsitePayment.objects.filter(merchant_id=merchant_id, paid_at__gte=since, refunded=False)


def _member_for(club, payment):
    """The member a payment belongs to (the carrier, for a carried membership), or None: by email, or with
    no email (a register sale), by a name only one member has.
    """
    members = ClubMember.objects.filter(club=club, is_deleted=False).select_related("membership_carried_by")
    if payment.buyer_email:
        member = members.filter(email__iexact=payment.buyer_email).order_by("-membership_expiration_date", "pk").first()
    elif payment.buyer_name:
        named = list(members.filter(name__iexact=payment.buyer_name)[:2])
        member = named[0] if len(named) == 1 else None
    else:
        member = None
    if member and member.membership_carried_by_id:
        return member.membership_carried_by
    return member


def next_dues_payment(club):
    """The newest unhandled off-site payment of exactly one year's dues, with the member it renews (or
    None, meaning "add them"). ``(None, None)`` when there isn't one.
    """
    seller = club.effective_square_seller
    if not club.charges_dues or not seller or not seller.square_merchant_id:
        return None, None
    candidates = (
        _recent(seller.square_merchant_id)
        .filter(resolution="", amount=club.membership_annual_fee, currency=seller.currency)
        .exclude(buyer_name="", buyer_email="")
        .order_by("-paid_at", "-pk")
    )
    for payment in candidates[:25]:
        member = _member_for(club, payment)
        if not _renewed_since(member, payment):
            return payment, member
    return None, None


def _renewed_since(member, payment):
    """The member was renewed on or after the payment's day, some other way: this money is spent."""
    return bool(
        member and member.membership_last_paid and member.membership_last_paid >= timezone.localdate(payment.paid_at)
    )


def resolve_dues_payment(club, payment_pk, action, user):
    """Act on a payment from :func:`next_dues_payment`: ``add`` a new member, ``renew`` the matching one, or
    ``dismiss`` it. Returns the member renewed or added, or None. Raises ValueError when it no longer applies.
    """
    # Lazily: the views import this module.
    from auctions.views.base import renew_club_member

    seller = club.effective_square_seller
    if not seller or not seller.square_merchant_id or action not in ("add", "renew", "dismiss"):
        msg = "Nothing to do"
        raise ValueError(msg)
    with transaction.atomic():
        payment = _recent(seller.square_merchant_id).select_for_update().filter(pk=payment_pk, resolution="").first()
        if payment is None:
            msg = "That payment has already been handled"
            raise ValueError(msg)
        member = None
        if action == "dismiss":
            payment.resolution = SquareOffsitePayment.RESOLUTION_DISMISSED
        else:
            if payment.amount != club.membership_annual_fee or not club.charges_dues:
                msg = "That payment isn't one year's dues"
                raise ValueError(msg)
            member = _member_for(club, payment)
            if action == "renew" and member is None:
                msg = "No member matches that payment"
                raise ValueError(msg)
            if _renewed_since(member, payment):
                msg = f"{member} has already been renewed since that payment"
                raise ValueError(msg)
            if action == "add":
                if member is not None:
                    msg = "That payment matches a member already"
                    raise ValueError(msg)
                member = ClubMember.objects.create(
                    club=club,
                    name=payment.buyer_name or payment.buyer_email,
                    email=payment.buyer_email,
                    added_by=user,
                    source="Square",
                )
                ClubHistory.objects.create(
                    club=club, user=user, action=f"Added member {member}", applies_to="MEMBERS", member=member
                )
            # Already in the treasurer report as an off-site sale: it becomes the dues, not a second row.
            booked = ClubMoney.objects.filter(
                club=club, external_id=payment.payment_id, category=ClubMoney.CATEGORY_SQUARE_OFFSITE
            ).first()
            renew_club_member(
                member,
                acting_user=user,
                money_description=f"Membership dues for {member}, paid in Square",
                book_money=booked is None,
                external_id=payment.payment_id,
            )
            if booked:
                booked.category = ClubMoney.CATEGORY_MEMBERSHIP
                booked.description = f"Membership dues for {member}, paid in Square"[: ClubMoney.DESCRIPTION_MAX_LENGTH]
                booked.save(update_fields=["category", "description"])
            payment.resolution = (
                SquareOffsitePayment.RESOLUTION_ADDED if action == "add" else SquareOffsitePayment.RESOLUTION_RENEWED
            )
            payment.member = member
        payment.resolved_by = user
        payment.resolved_on = timezone.now()
        payment.save(update_fields=["resolution", "resolved_by", "resolved_on", "member"])
    return member


def invoice_payment_hint(invoice):
    """One line when an off-site payment made after this unpaid invoice was created matches its balance."""
    if invoice.status == "PAID" or not invoice.pk:
        return ""
    owed = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
    host = invoice.auction or invoice.club
    seller = host.effective_square_seller if host else None
    if owed <= 0 or not seller or not seller.square_merchant_id:
        return ""
    payment = (
        _recent(seller.square_merchant_id, since=invoice.date)
        .filter(amount=owed, currency=invoice.currency)
        .exclude(resolution__in=(SquareOffsitePayment.RESOLUTION_ADDED, SquareOffsitePayment.RESOLUTION_RENEWED))
        .order_by("-paid_at", "-pk")
        .first()
    )
    if payment is None:
        return ""
    when = timezone.localtime(payment.paid_at).strftime("%b %-d, %-I:%M %p")
    who = payment.buyer or "an unknown buyer"
    return f"This user may have already paid their invoice: Square payment from {who}, {when}"
