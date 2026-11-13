"""Finding people who seem to have two accounts, and telling each account once that merging exists.

A pair of active accounts is a likely duplicate when both of these hold:

* **The same name.** Their auction or club records were flagged as possible duplicates of each other
  (``AuctionTOS.possible_duplicate``, ``ClubMember.possible_duplicate``; exact or nickname match), or
  the accounts' own first and last names are the same. A one-word name is never enough.
* **The same connection.** They share an IP address: ``UserData.last_ip_address``, or a page view, within
  :data:`IP_LOOKBACK`. An address seen for more than :data:`MAX_ACCOUNTS_PER_IP` accounts is a
  club venue, a school or a carrier, not a household, so it's no evidence.

Each account in such a pair gets the ``account_merge_suggestion`` email once
(``UserData.merge_suggested_on``). It says only that accounts can be merged: it never names, counts or
hints at the other account, and nothing here shows an IP address to anyone. Run by hand with
``manage.py suggest_account_merges`` (only counts unless given ``--send``), and weekly by the
``suggest_account_merges`` task. The scan stops checking pairs after :data:`TIME_BUDGET` seconds; the
rest are checked the next week, since nothing about a pair is stored until its email goes.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count
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

#: Seconds spent checking pairs for a shared address before stopping (the task's soft limit is 300).
TIME_BUDGET = 200

TEMPLATE = "account_merge_suggestion"


def _full_name(name) -> bool:
    return len((name or "").split()) >= 2


def _pair(a, b):
    return (a, b) if a < b else (b, a)


def _flagged_record_pairs():
    """Account pairs whose auction or club records were flagged as the same person."""
    pairs = set()
    tos = AuctionTOS.objects.filter(
        auction__is_deleted=False,
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
    """Account pairs with the same first and last name. One read, grouped here."""
    groups = defaultdict(list)
    rows = (
        get_user_model()
        .objects.filter(is_active=True)
        .exclude(first_name="")
        .exclude(last_name="")
        .order_by("pk")
        .values_list("pk", "first_name", "last_name")
    )
    for pk, first, last in rows.iterator():
        key = (first.strip().lower(), last.strip().lower())
        if all(key):
            groups[key].append(pk)
    pairs = set()
    for pks in groups.values():
        if 1 < len(pks) <= MAX_SAME_NAME_GROUP:
            pairs.update((a, b) for i, a in enumerate(pks) for b in pks[i + 1 :])
    return pairs


class _Connections:
    """Shared-IP checks for one run, caching how busy each address is."""

    def __init__(self, now):
        self.since = now - IP_LOOKBACK
        self.busy = {}

    def household(self, ip) -> bool:
        """Whether few enough accounts use *ip* for it to mean anything."""
        if ip not in self.busy:
            accounts = set(
                PageView.objects.filter(ip_address=ip, date_start__gte=self.since, user__isnull=False)
                .order_by()
                .values_list("user_id", flat=True)
                .distinct()[: MAX_ACCOUNTS_PER_IP + 1]
            )
            if len(accounts) <= MAX_ACCOUNTS_PER_IP:
                accounts.update(
                    UserData.objects.filter(last_ip_address=ip).values_list("user_id", flat=True)[
                        : MAX_ACCOUNTS_PER_IP + 1
                    ]
                )
            self.busy[ip] = len(accounts) > MAX_ACCOUNTS_PER_IP
        return not self.busy[ip]

    def shared(self, a, b) -> bool:
        """Whether accounts *a* and *b* (pks) have used the same household-sized IP address lately."""
        last = dict(
            UserData.objects.filter(user_id__in=[a, b], last_activity__gte=self.since).values_list(
                "user_id", "last_ip_address"
            )
        )
        candidates = set()
        if last.get(a) and last.get(a) == last.get(b):
            candidates.add(last[a])
        candidates.update(
            PageView.objects.filter(user_id__in=[a, b], date_start__gte=self.since)
            .exclude(ip_address__isnull=True)
            .exclude(ip_address="")
            .order_by()
            .values("ip_address")
            .annotate(n=Count("user_id", distinct=True))
            .filter(n=2)
            .values_list("ip_address", flat=True)
        )
        return any(self.household(ip) for ip in candidates)


def likely_duplicate_pairs(now=None, budget=None):
    """Pairs of active, non-staff account pks that are probably one person and aren't already merging.

    Pairs whose accounts were both already emailed are skipped, and so are pairs not yet checked after
    *budget* seconds.
    """
    User = get_user_model()
    pairs = _flagged_record_pairs() | _same_name_pairs()
    if not pairs:
        return []
    eligible = set(
        User.objects.filter(
            pk__in={pk for pair in pairs for pk in pair},
            is_active=True,
            is_staff=False,
            is_superuser=False,
            userdata__merge_into_user__isnull=True,
        ).values_list("pk", flat=True)
    )
    eligible -= set(UserData.objects.filter(merge_into_user__in=eligible).values_list("merge_into_user", flat=True))
    emailed = set(
        UserData.objects.filter(user_id__in=eligible, merge_suggested_on__isnull=False).values_list(
            "user_id", flat=True
        )
    )
    connections = _Connections(now or timezone.now())
    stop = time.monotonic() + budget if budget else None
    found = []
    # Newest accounts first: when the budget runs out, the pairs left are the ones checked in earlier weeks.
    for a, b in sorted(pairs, key=lambda pair: pair[1], reverse=True):
        if stop and time.monotonic() > stop:
            logger.info("duplicate account scan stopped at its time budget")
            break
        if a in emailed and b in emailed:
            continue
        if a in eligible and b in eligible and connections.shared(a, b):
            found.append((a, b))
    return sorted(found)


def _deliverable(user) -> bool:
    userdata = getattr(user, "userdata", None)
    if not userdata or userdata.has_unsubscribed or userdata.email_bounced_on:
        return False
    try:
        validate_email((user.email or "").strip())
    except ValidationError:
        return False
    return True


def accounts_to_email(pairs):
    """The accounts in *pairs* that would get the email: never sent it, reachable, one per address."""
    users = (
        get_user_model()
        .objects.filter(pk__in={pk for pair in pairs for pk in pair}, userdata__merge_suggested_on__isnull=True)
        .select_related("userdata")
        .order_by("pk")
    )
    seen = set()
    chosen = []
    for user in users:
        address = (user.email or "").strip().lower()
        if address not in seen and _deliverable(user):
            seen.add(address)
            chosen.append(user)
    return chosen


def send_merge_suggestions(now=None, budget=None) -> int:
    """Email every account :func:`accounts_to_email` picks, once each. Returns how many went."""
    now = now or timezone.now()
    domain = Site.objects.get_current().domain
    sent = 0
    for user in accounts_to_email(likely_duplicate_pairs(now=now, budget=budget)):
        # Claimed first, so overlapping runs can't both send; given back if the send fails.
        claimed = UserData.objects.filter(user=user, merge_suggested_on__isnull=True)
        if not claimed.update(merge_suggested_on=now):
            continue
        try:
            mail.send(
                user.email,
                template=TEMPLATE,
                context={"user": user, "domain": domain, "unsubscribe": str(user.userdata.unsubscribe_link)},
            )
        except Exception:
            logger.exception("merge suggestion email failed for user %s", user.pk)
            UserData.objects.filter(user=user, merge_suggested_on=now).update(merge_suggested_on=None)
            continue
        sent += 1
    return sent
