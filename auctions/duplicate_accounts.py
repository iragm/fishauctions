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
``suggest_account_merges`` task. A pair is checked only while one of its accounts can still be told.
A run stops checking after :data:`TIME_BUDGET` seconds; pairs go in a fresh random order each run, so
the ones it didn't reach get their turn in later weeks.
"""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
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

#: Seconds a weekly run spends finding pairs before it sends; the task's soft limit is 300.
TIME_BUDGET = 150
#: Emails one run sends at most; the rest wait for the next week's run.
MAX_PER_RUN = 100

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


def _clock():
    return time.monotonic()


class _Connections:
    """Shared-IP checks for one run. Each account's addresses and each address's busyness are read once."""

    def __init__(self, now):
        self.since = now - IP_LOOKBACK
        self.busy = {}
        self.addresses = {}

    def household(self, ip) -> bool:
        """Whether few enough accounts use *ip* for it to mean anything."""
        if ip not in self.busy:
            accounts = (
                PageView.objects.filter(ip_address=ip, date_start__gte=self.since, user__isnull=False)
                .order_by()
                .values_list("user_id", flat=True)
                .distinct()[: MAX_ACCOUNTS_PER_IP + 1]
            )
            self.busy[ip] = len(list(accounts)) > MAX_ACCOUNTS_PER_IP
        return not self.busy[ip]

    def _of(self, pk):
        if pk not in self.addresses:
            self.addresses[pk] = set(
                PageView.objects.filter(user_id=pk, date_start__gte=self.since)
                .exclude(ip_address__isnull=True)
                .exclude(ip_address="")
                .order_by()
                .values_list("ip_address", flat=True)
                .distinct()
            )
            last = (
                UserData.objects.filter(user_id=pk, last_activity__gte=self.since)
                .values_list("last_ip_address", flat=True)
                .first()
            )
            if last:
                self.addresses[pk].add(last)
        return self.addresses[pk]

    def shared(self, a, b) -> bool:
        """Whether accounts *a* and *b* (pks) have used the same household-sized IP address lately."""
        return any(self.household(ip) for ip in self._of(a) & self._of(b))


def _address(user):
    return (user.email or "").strip().lower()


def _deliverable(user) -> bool:
    userdata = getattr(user, "userdata", None)
    if not userdata or userdata.has_unsubscribed or userdata.email_bounced_on:
        return False
    try:
        validate_email((user.email or "").strip())
    except ValidationError:
        return False
    return True


def _told_addresses(users):
    """The addresses, of those *users* have, that some account already got the email at."""
    wanted = {_address(user) for user in users}
    told = (
        get_user_model()
        .objects.filter(userdata__merge_suggested_on__isnull=False)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    return {email.strip().lower() for email in told.iterator()} & wanted


def likely_duplicate_pairs(now=None, deadline=None):
    """Pairs of active, non-staff account pks that are probably one person, where one of the two can still
    be told: not emailed, reachable, and not already merging.

    Checking stops at *deadline* (a :func:`_clock` reading); the pairs found by then are returned.
    """
    User = get_user_model()
    pairs = _flagged_record_pairs() | _same_name_pairs()
    if not pairs:
        return []
    users = {
        user.pk: user
        for user in User.objects.filter(
            pk__in={pk for pair in pairs for pk in pair},
            is_active=True,
            is_staff=False,
            is_superuser=False,
            userdata__merge_into_user__isnull=True,
        ).select_related("userdata")
    }
    for pk in UserData.objects.filter(merge_into_user__in=list(users)).values_list("merge_into_user", flat=True):
        users.pop(pk, None)
    told = _told_addresses(users.values())
    can_tell = {
        pk
        for pk, user in users.items()
        if not user.userdata.merge_suggested_on and _deliverable(user) and _address(user) not in told
    }
    pairs = [(a, b) for a, b in pairs if a in users and b in users and (a in can_tell or b in can_tell)]
    random.shuffle(pairs)
    connections = _Connections(now or timezone.now())
    found = []
    for a, b in pairs:
        if deadline and _clock() > deadline:
            logger.info("duplicate account scan stopped at its time budget")
            break
        if connections.shared(a, b):
            found.append((a, b))
    return sorted(found)


def accounts_to_email(pairs):
    """The accounts in *pairs* that would get the email: never sent it, reachable, one per address ever."""
    users = list(
        get_user_model()
        .objects.filter(pk__in={pk for pair in pairs for pk in pair}, userdata__merge_suggested_on__isnull=True)
        .select_related("userdata")
        .order_by("pk")
    )
    seen = _told_addresses(users)
    chosen = []
    for user in users:
        if _address(user) not in seen and _deliverable(user):
            seen.add(_address(user))
            chosen.append(user)
    return chosen


def send_merge_suggestions(now=None, budget=None) -> int:
    """Email every account :func:`accounts_to_email` picks, once each. Returns how many went.

    *budget* is the seconds allowed for finding pairs, counted from the call.
    """
    deadline = _clock() + budget if budget else None
    now = now or timezone.now()
    domain = Site.objects.get_current().domain
    sent = 0
    for user in accounts_to_email(likely_duplicate_pairs(now=now, deadline=deadline)):
        if sent >= MAX_PER_RUN:
            break
        # Claimed first, so overlapping runs can't both send; given back if the send fails.
        if not UserData.objects.filter(user=user, merge_suggested_on__isnull=True).update(merge_suggested_on=now):
            continue
        try:
            mail.send(
                user.email,
                template=TEMPLATE,
                context={"user": user, "domain": domain, "unsubscribe": str(user.userdata.unsubscribe_link)},
            )
        except SoftTimeLimitExceeded:
            # The email may already be queued: keep the claim, since twice is worse than never.
            raise
        except Exception:
            logger.exception("merge suggestion email failed for user %s", user.pk)
            UserData.objects.filter(user=user, merge_suggested_on=now).update(merge_suggested_on=None)
            continue
        sent += 1
    return sent
