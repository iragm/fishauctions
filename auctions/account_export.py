""" "Download my data": everything this site holds about one person, as one JSON file.

The mirror of :mod:`auctions.account_deletion`. Deletion was built for the app stores; this is built
for the state privacy laws that reach a non-profit -- Colorado's and Oregon's do, where California's
CCPA does not -- and all of them pair a right to delete with a right to a copy. It is also the
cheapest answer to "what do you actually know about me", which is a question this site gets asked in
plain language rather than as a legal request.

What is in it is chosen, not introspected, with two exceptions: the profile block walks
``UserData``'s own fields so that a preference added next year is exported without anybody
remembering to come back here, and ``SKIP_PROFILE_FIELDS`` names the few that aren't the reader's
business or aren't theirs.

Three things are deliberately left out, and :data:`NOT_INCLUDED` says so inside the file rather than
leaving the reader to wonder:

* **Credentials.** Passwords, API keys, push tokens, wallet-pass tokens, payment-processor tokens.
  Exporting a credential into a file that then sits in a downloads folder is a way to lose an
  account, and none of it is information *about* the person.
* **The page-by-page browsing log.** ``PageView`` goes back to 2020 and is never purged, so a busy
  organizer's log is tens of thousands of rows: the summary says how many and over what period, and
  the page says how to ask for the rest.
* **Free-text notes a club or auction admin wrote about a member.** Those live in someone else's
  records, and handing them over automatically changes what a club can safely write down. The file
  names them so nobody has to guess they exist.
"""

import logging
from datetime import date

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Not exported from ``UserData``: the row's own plumbing, and the unsubscribe token, which is a
#: bearer credential -- anyone holding the file could turn off that person's email.
SKIP_PROFILE_FIELDS = frozenset({"id", "user", "unsubscribe_link"})

NOT_INCLUDED = [
    "Passwords, API keys, app push tokens, membership-pass tokens, and anything connecting your "
    "PayPal or Square account. Those are credentials, not information about you.",
    "Other people's records: what somebody else bid, sold, or wrote.",
    "Your full page-by-page browsing history. There is a summary of it below; ask us and we will send you the rest.",
    "Free-text notes a club or auction admin has written about you. Those sit in that club's own "
    "records - ask the club, or ask us and we will pass the request on.",
]


def _plain(value):
    """A JSON-safe version of one field value: a related object becomes what it calls itself."""
    from decimal import Decimal

    from django.db.models import Model

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Model):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _profile(userdata):
    """Every field on ``UserData``, so a preference added later is exported without an edit here."""
    if not userdata:
        return {}
    out = {}
    for field in userdata._meta.get_fields():
        if not getattr(field, "concrete", False) or field.name in SKIP_PROFILE_FIELDS:
            continue
        out[field.name] = _plain(getattr(userdata, field.name, None))
    return out


def _page_view_summary(user):
    """Counts and dates, not rows. See the module docstring."""
    from django.db.models import Max, Min

    from auctions.models import PageView

    views = PageView.objects.filter(user=user)
    bounds = views.aggregate(first=Min("date_start"), last=Max("date_start"))
    return {
        "pages_viewed": views.count(),
        "first": _plain(bounds["first"]),
        "last": _plain(bounds["last"]),
        "note": "Pages viewed and the IP address they came from, kept so we can see which parts of "
        "the site people struggle with. Ask us for the full log.",
    }


def _lots_sold(user):
    """Lots this person sold, reached either way.

    ``Lot.user`` is who typed it in; ``auctiontos_seller`` is the bidder number it was sold under, and
    an in-person lot an admin entered at the door has only the second. Filtering on one of them
    silently leaves a whole auction's worth of somebody's lots out of their own data.
    """
    from auctions.models import Lot

    return (
        Lot.objects.filter(Q(user=user) | Q(auctiontos_seller__user=user), is_deleted=False)
        .select_related("auction")
        .distinct()
    )


def _lots_won(user):
    """Lots this person won, reached either way. Same reason as :func:`_lots_sold`."""
    from auctions.models import Lot

    return Lot.objects.filter(Lot.won_by_q(user), is_deleted=False).select_related("auction").distinct()


def _invoices(user):
    """Invoices in this person's name, whether the account or the bidder number is what links them."""
    from auctions.models import Invoice

    return (
        Invoice.objects.filter(Q(buyer=user) | Q(auctiontos_user__user=user))
        .select_related("auction", "club")
        .distinct()
    )


def export(user):
    """Everything this site holds about *user*, as nested dicts ready for ``json.dumps``."""
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount

    from auctions.models import (
        AuctionTOS,
        Bid,
        ClubMember,
        Document,
        LotHistory,
        MobileDevice,
        SearchHistory,
        UserIgnoreCategory,
        UserInterestCategory,
        Watch,
    )

    userdata = getattr(user, "userdata", None)
    return {
        "exported_on": timezone.now().isoformat(),
        "what_this_is": (
            "Everything this site holds about your account, in one file. Sections are empty where "
            "there is nothing to report."
        ),
        "not_included": NOT_INCLUDED,
        "account": {
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "joined": _plain(user.date_joined),
            "last_signed_in": _plain(user.last_login),
        },
        "profile": _profile(userdata),
        "email_addresses": [
            {"email": row.email, "verified": row.verified, "primary": row.primary}
            for row in EmailAddress.objects.filter(user=user)
        ],
        # The provider, never the provider's user id or token: one identifies the connection, the
        # other is a credential.
        "signed_in_with": [
            {"provider": row.provider, "connected": _plain(row.date_joined)}
            for row in SocialAccount.objects.filter(user=user)
        ],
        "app_devices": [
            {
                "name": row.device_name,
                "platform": row.platform,
                "app_version": row.app_version,
                "first_seen": _plain(row.created_at),
                "last_seen": _plain(row.last_seen),
                "push_enabled": row.push_enabled,
            }
            for row in MobileDevice.objects.filter(user=user)
        ],
        "auctions_joined": [
            {
                "auction": _plain(row.auction),
                "joined": _plain(row.createdon),
                "bidder_number": row.bidder_number,
                "pickup_location": _plain(row.pickup_location),
                "name_given": row.name,
                "email_given": row.email,
                "phone_given": row.phone_number,
                "address_given": row.address,
                "is_admin": row.is_admin,
                "checked_in": _plain(row.checked_in),
                "added_by_an_admin_at_the_door": row.manually_added,
                "how_was_it": row.get_survey_answer_display(),
                "feedback_on_the_auction": row.survey_comments,
            }
            for row in AuctionTOS.objects.filter(user=user).select_related("auction", "pickup_location")
        ],
        "club_memberships": [
            {
                "club": _plain(row.club),
                "joined": _plain(row.createdon),
                "member_number": row.membership_number,
                "name_given": row.name,
                "email_given": row.email,
                "phone_given": row.phone_number,
                "address_given": row.address,
                "dues_last_paid": _plain(row.membership_last_paid),
                "membership_expires": _plain(row.membership_expiration_date),
                "breeder_award_points": row.bap_points,
                "horticultural_award_points": row.hap_points,
                "culture_points": row.culture_points,
            }
            for row in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club")
        ],
        "library_documents": [
            {
                "title": row.display_title,
                "file_name": row.original_name,
                "club": _plain(row.club),
                "uploaded": _plain(row.createdon),
            }
            for row in Document.objects.filter(owner=user).select_related("club")
        ],
        "lots_sold": [
            {
                "lot_number": row.lot_number,
                "name": row.lot_name,
                "auction": _plain(row.auction),
                "listed": _plain(row.date_posted),
                "quantity": row.quantity,
                "sold_for": _plain(row.winning_price),
                "i_bred_this": row.i_bred_this_fish,
            }
            for row in _lots_sold(user)
        ],
        "lots_won": [
            {
                "lot_number": row.lot_number,
                "name": row.lot_name,
                "auction": _plain(row.auction),
                "paid": _plain(row.winning_price),
                "ended": _plain(row.date_end),
            }
            for row in _lots_won(user)
        ],
        "bids": [
            {
                "lot": _plain(row.lot_number),
                "amount": _plain(row.amount),
                "placed": _plain(row.bid_time),
                "was_the_high_bid": row.was_high_bid,
            }
            for row in Bid.objects.filter(user=user, is_deleted=False).select_related("lot_number")
        ],
        "invoices": [
            {
                "auction": _plain(row.auction),
                "club": _plain(row.club),
                "date": _plain(row.date),
                "status": row.status,
                "total": _plain(row.calculated_total),
                "paid_on": _plain(row.date_paid),
            }
            for row in _invoices(user)
        ],
        "chat_messages": [
            {"lot": _plain(row.lot), "message": row.message, "when": _plain(row.timestamp)}
            # changed_price rows are the site's own lines ("X was removed as the winner"), written with the
            # acting admin as user: about somebody else, and not anything this person said.
            for row in LotHistory.objects.filter(user=user, changed_price=False)
            .exclude(message="")
            .select_related("lot")
        ],
        "watched_lots": [
            {"lot": _plain(row.lot_number), "watched_since": _plain(row.createdon)}
            for row in Watch.objects.filter(user=user).select_related("lot_number")
        ],
        "searches": [
            {"searched_for": row.search, "when": _plain(row.createdon), "auction": _plain(row.auction)}
            for row in SearchHistory.objects.filter(user=user).select_related("auction")
        ],
        "categories": {
            "hidden": [_plain(row.category) for row in UserIgnoreCategory.objects.filter(user=user)],
            "interest_scores": {
                _plain(row.category): row.interest for row in UserInterestCategory.objects.filter(user=user)
            },
        },
        "browsing": _page_view_summary(user),
    }


def filename(user, today=None):
    """What the browser saves it as."""
    stamp = (today or date.today()).isoformat()  # noqa: DTZ011 - a filename, not a timestamp
    safe_username = "".join(character if character.isalnum() else "-" for character in user.username)
    return f"{safe_username}-my-data-{stamp}.json"
