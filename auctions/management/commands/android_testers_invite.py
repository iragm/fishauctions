"""Invite Android users to the Google Play closed test, once.

Who: active accounts at least six months old, with an email, not unsubscribed and not being deleted,
that viewed a page in an Android browser in the last 90 days. Refuses unless the closed test is on
(``PLAY_TESTERS_GROUP_URL`` and ``PLAY_TESTING_URL`` set, ``PLAY_STORE_URL`` not yet).

Safe to re-run: a ``PushNotificationSent`` row per user is the ledger (post_office's own rows are
deleted after ``MAIL_RETENTION_DAYS``), claimed in the same transaction that queues the email, under a
lock so two runs can't overlap. One inbox gets one email even when several accounts share it.

Usage::

    manage.py android_testers_invite --dry-run
    manage.py android_testers_invite
"""

import datetime
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from auctions.models import PageView, PushNotificationSent

TEMPLATE_NAME = "android_testers_invite"
CATEGORY = "android_testers_invite"
ACCOUNT_AGE = datetime.timedelta(days=183)
ACTIVE_WITHIN = datetime.timedelta(days=90)
LOCK_KEY = "android_testers_invite:running"
LOCK_SECONDS = 60 * 60


def eligible_users(now=None):
    now = now or timezone.now()
    # PageViewCreate stores the user agent's OS family in ``platform``; ``os`` is always UNKNOWN.
    android_viewers = (
        PageView.objects.filter(date_start__gte=now - ACTIVE_WITHIN, platform="Android", user__isnull=False)
        .order_by()
        .values_list("user_id", flat=True)
        .distinct()
    )
    return (
        User.objects.filter(
            pk__in=android_viewers,
            is_active=True,
            date_joined__lte=now - ACCOUNT_AGE,
            userdata__has_unsubscribed=False,
            userdata__account_deletion_requested__isnull=True,
        )
        .exclude(email="")
        .exclude(pushnotificationsent__category=CATEGORY)
        .select_related("userdata")
        .order_by("pk")
    )


def invited_addresses():
    """Inboxes already invited, through any account."""
    emails = User.objects.filter(pushnotificationsent__category=CATEGORY).values_list("email", flat=True)
    return {email.strip().lower() for email in emails}


class Command(BaseCommand):
    help = "Email Android users active in the last 90 days, with accounts 6+ months old, to join the Play closed test"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count recipients without sending")

    def handle(self, *args, **options):
        if not settings.PLAY_TESTERS_GROUP_URL or not settings.PLAY_TESTING_URL or settings.PLAY_STORE_URL:
            msg = "The closed test isn't on: set PLAY_TESTERS_GROUP_URL and PLAY_TESTING_URL, and leave PLAY_STORE_URL blank."
            raise CommandError(msg)
        from post_office.models import EmailTemplate

        if not EmailTemplate.objects.filter(name=TEMPLATE_NAME).exists():
            msg = f"No '{TEMPLATE_NAME}' email template; run migrate."
            raise CommandError(msg)

        token = uuid.uuid4().hex
        if not cache.add(LOCK_KEY, token, timeout=LOCK_SECONDS):
            msg = "Another run is in progress."
            raise CommandError(msg)
        try:
            self._run(options["dry_run"])
        finally:
            if cache.get(LOCK_KEY) == token:
                cache.delete(LOCK_KEY)

    def _run(self, dry_run):
        seen = invited_addresses()
        recipients = []
        for user in eligible_users():
            address = user.email.strip().lower()
            if address not in seen:
                seen.add(address)
                recipients.append(user)
        if dry_run:
            self.stdout.write(f"[DRY RUN] would email {len(recipients)} people.")
            return

        domain = Site.objects.get_current().domain
        help_path = reverse("help_guide", kwargs={"slug": "mobile-app"})
        sent = skipped = 0
        for user in recipients:
            try:
                with transaction.atomic():
                    PushNotificationSent.objects.create(user=user, category=CATEGORY)
                    self._send(user, domain, help_path)
            except ValidationError:
                # A malformed address: post_office refuses it, and the rest of the run goes on.
                skipped += 1
                continue
            sent += 1
        self.stdout.write(self.style.SUCCESS(f"Queued {sent} email(s); skipped {skipped} bad address(es)."))

    @staticmethod
    def _send(user, domain, help_path):
        from post_office import mail

        mail.send(
            user.email.strip(),
            template=TEMPLATE_NAME,
            context={
                "name": user.first_name,
                "domain": domain,
                "group_url": settings.PLAY_TESTERS_GROUP_URL,
                "testing_url": settings.PLAY_TESTING_URL,
                "help_path": help_path,
                "unsubscribe": user.userdata.unsubscribe_link,
            },
        )
