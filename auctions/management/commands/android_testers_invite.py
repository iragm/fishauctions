"""Invite Android users to the Google Play closed test, once.

Who: active accounts at least six months old, with an email and not unsubscribed, that viewed a page
on an Android phone in the last 90 days. Refuses unless the closed test is on (``PLAY_TESTERS_GROUP_URL``
set, ``PLAY_STORE_URL`` not yet). Safe to re-run: anybody already sent the ``android_testers_invite``
template is skipped.

Usage::

    manage.py android_testers_invite --dry-run
    manage.py android_testers_invite
"""

import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import timezone

from auctions.models import PageView

TEMPLATE_NAME = "android_testers_invite"
ACCOUNT_AGE = datetime.timedelta(days=183)
ACTIVE_WITHIN = datetime.timedelta(days=90)


def eligible_users(now=None):
    now = now or timezone.now()
    android_viewers = (
        PageView.objects.filter(date_start__gte=now - ACTIVE_WITHIN, os="ANDROID", user__isnull=False)
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
        )
        .exclude(email="")
        .select_related("userdata")
        .order_by("pk")
    )


def already_invited():
    from post_office.models import Email

    emails = Email.objects.filter(template__name=TEMPLATE_NAME).values_list("to", flat=True)
    return {address.lower() for to in emails for address in to}


class Command(BaseCommand):
    help = "Email Android users active in the last 90 days, with accounts 6+ months old, to join the Play closed test"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count recipients without sending")

    def handle(self, *args, **options):
        if not settings.PLAY_TESTERS_GROUP_URL or settings.PLAY_STORE_URL:
            msg = "The closed test isn't on: set PLAY_TESTERS_GROUP_URL and leave PLAY_STORE_URL blank."
            raise CommandError(msg)
        from post_office import mail
        from post_office.models import EmailTemplate

        if not EmailTemplate.objects.filter(name=TEMPLATE_NAME).exists():
            msg = f"No '{TEMPLATE_NAME}' email template; run migrate."
            raise CommandError(msg)

        done = already_invited()
        recipients = [user for user in eligible_users() if user.email.lower() not in done]
        if options["dry_run"]:
            self.stdout.write(f"[DRY RUN] would email {len(recipients)} people ({len(done)} already invited).")
            return

        domain = Site.objects.get_current().domain
        help_path = reverse("help_guide", kwargs={"slug": "mobile-app"})
        for user in recipients:
            mail.send(
                user.email,
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
        self.stdout.write(self.style.SUCCESS(f"Queued {len(recipients)} email(s)."))
