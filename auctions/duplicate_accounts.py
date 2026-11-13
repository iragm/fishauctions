"""Finding people who seem to have two accounts, and telling each account once that merging exists.

A pair of active accounts is a likely duplicate when both of these hold:

* **The same name.** Their auction or club records were flagged as possible duplicates of each other
  (``AuctionTOS.possible_duplicate``, ``ClubMember.possible_duplicate``; exact or nickname match), or
  the accounts' own first and last names are the same. A one-word name is never enough.
* **The same connection.** They share an IP address: ``UserData.last_ip_address``, or a page view
  within :data:`IP_LOOKBACK`. An address seen for more than :data:`MAX_ACCOUNTS_PER_IP` accounts is a
  club venue, a school or a carrier, not a household, so it's no evidence.

Each account in such a pair gets the ``account_merge_suggestion`` email once
(``UserData.merge_suggested_on``). It says only that accounts can be merged: it never names, counts or
hints at the other account, and nothing here shows an IP address to anyone. Run by hand with
``manage.py suggest_account_merges``, which only counts unless given ``--send``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from post_office import mail

from auctions.models import AuctionTOS, ClubMember, PageView, UserData

logger = logging.getLogger(__name__)

#: Page views older than this aren't looked at.
IP_LOOKBACK = timedelta(days=365)
#: An address shared by more accounts than this says nothing about any two of them.
MAX_ACCOUNTS_PER_IP = 4
#: Bigger groups of accounts with one name are a common name, not one person.
MAX_SAME_NAME_GROUP = 6

TEMPLATE = "account_merge_suggestion"


def _full_name(name) -> bool:
    return len((name or "").split()) >= 2


def _pair(a, b):
    return (a, b) if a < b else (b, a)


def _flagged_record_pairs():
    """Account pairs whose auction or club records were flagged as the same person."""
    pairs = set()
    tos = AuctionTOS.objects.filter(
        possible_duplicate__isnull=False,
        user__isnull=False,
        possible_duplicate__user__isnull=False,
    ).values_list("user_id", "possible_duplicate__user_id", "name", "possible_duplicate__name")
    members = ClubMember.objects.filter(
        is_deleted=False,
        possible_duplicate__isnull=False,
        possible_duplicate__is_deleted=False,
        user__isnull=False,
        possible_duplicate__user__isnull=False,
    ).values_list("user_id", "possible_duplicate__user_id", "name", "possible_duplicate__name")
    for rows in (tos, members):
        for a, b, name_a, name_b in rows.order_by().iterator():
            if a != b and _full_name(name_a) and _full_name(name_b):
                pairs.add(_pair(a, b))
    return pairs


def _same_name_pairs():
    """Account pairs with the same first and last name."""
    User = get_user_model()
    named = (
        User.objects.filter(is_active=True)
        .annotate(f=Lower(Trim("first_name")), l=Lower(Trim("last_name")))
        .exclude(f="")
        .exclude(l="")
    )
    groups = named.values("f", "l").annotate(n=Count("pk")).filter(n__gt=1, n__lte=MAX_SAME_NAME_GROUP).order_by()
    pairs = set()
    for group in groups:
        pks = sorted(named.filter(f=group["f"], l=group["l"]).values_list("pk", flat=True))
        pairs.update((a, b) for i, a in enumerate(pks) for b in pks[i + 1 :])
    return pairs


def _household_ip(ip, since) -> bool:
    """Whether few enough accounts use *ip* for it to mean anything."""
    page_view_accounts = (
        PageView.objects.filter(ip_address=ip, date_start__gte=since, user__isnull=False)
        .order_by()
        .values("user_id")
        .distinct()[: MAX_ACCOUNTS_PER_IP + 1]
        .count()
    )
    if page_view_accounts > MAX_ACCOUNTS_PER_IP:
        return False
    return UserData.objects.filter(last_ip_address=ip).count() <= MAX_ACCOUNTS_PER_IP


def share_a_connection(a, b, now=None) -> bool:
    """Whether accounts *a* and *b* (pks) have used the same household-sized IP address."""
    since = (now or timezone.now()) - IP_LOOKBACK
    last = dict(UserData.objects.filter(user_id__in=[a, b]).values_list("user_id", "last_ip_address"))
    candidates = set()
    if last.get(a) and last.get(a) == last.get(b):
        candidates.add(last[a])
    candidates.update(
        PageView.objects.filter(user_id__in=[a, b], date_start__gte=since)
        .exclude(ip_address__isnull=True)
        .exclude(ip_address="")
        .order_by()
        .values("ip_address")
        .annotate(n=Count("user_id", distinct=True))
        .filter(n=2)
        .values_list("ip_address", flat=True)[:20]
    )
    return any(_household_ip(ip, since) for ip in candidates)


def likely_duplicate_pairs(now=None):
    """Pairs of active, non-staff account pks that are probably one person."""
    User = get_user_model()
    pairs = _flagged_record_pairs() | _same_name_pairs()
    if not pairs:
        return []
    eligible = set(
        User.objects.filter(
            pk__in={pk for pair in pairs for pk in pair}, is_active=True, is_staff=False, is_superuser=False
        ).values_list("pk", flat=True)
    )
    return sorted((a, b) for a, b in pairs if a in eligible and b in eligible and share_a_connection(a, b, now=now))


def _deliverable(user) -> bool:
    userdata = getattr(user, "userdata", None)
    if not userdata or userdata.has_unsubscribed or userdata.email_bounced_on:
        return False
    try:
        validate_email((user.email or "").strip())
    except ValidationError:
        return False
    return True


def accounts_to_email(now=None):
    """The accounts that would get the email: in a likely pair, never sent it, and reachable."""
    User = get_user_model()
    pks = {pk for pair in likely_duplicate_pairs(now=now) for pk in pair}
    users = (
        User.objects.filter(pk__in=pks, userdata__merge_suggested_on__isnull=True)
        .select_related("userdata")
        .order_by("pk")
    )
    return [user for user in users if _deliverable(user)]


def send_merge_suggestions(now=None) -> int:
    """Email every account in :func:`accounts_to_email`, once each. Returns how many went."""
    now = now or timezone.now()
    domain = Site.objects.get_current().domain
    sent = 0
    for user in accounts_to_email(now=now):
        # Claimed first, so overlapping runs can't both send.
        if not UserData.objects.filter(user=user, merge_suggested_on__isnull=True).update(merge_suggested_on=now):
            continue
        try:
            mail.send(
                user.email,
                template=TEMPLATE,
                context={"user": user, "domain": domain, "unsubscribe": str(user.userdata.unsubscribe_link)},
            )
        except Exception:
            logger.exception("merge suggestion email failed for user %s", user.pk)
            continue
        sent += 1
    return sent
