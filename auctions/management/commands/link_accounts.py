"""Link every participant row to its person's account, once, for rows written before saving did it.

``AuctionTOS.link_user`` and ``ClubMember.save`` link on every save, and sign-in and email changes catch
rows added before the account existed, so everything "mine" can match on ``user`` alone. This does the
same to the rows that already exist, and gives lots whose seller row is linked their ``Lot.user``.
Saving does the linking, so duplicates in one auction merge exactly as they do live. Safe to rerun.

It runs on the live site, so it goes one row at a time in this one process. Each save's side effects
(Mailchimp and Brevo syncs, mostly) run here rather than queued: thousands of them at once would hold up
the workers that send the site's email. ``--pause`` spaces the rows out. Each row is re-read just before
it is saved, so a slow run doesn't write back a copy an admin has edited since.
"""

import time

from celery import current_app
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from auctions.models import AuctionTOS, ClubMember, Lot, normalize_email


class Command(BaseCommand):
    help = (
        "Link AuctionTOS and ClubMember rows to the account their email (or club member) belongs to, "
        "merging same-auction duplicates, then fill in Lot.user from each lot's linked seller row."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
        parser.add_argument(
            "--pause", type=float, default=0.05, help="Seconds to wait after each row saved (default 0.05)."
        )

    def handle(self, *args, **options):
        # Tasks queued by the saves' signals run inline, in this process, one at a time.
        eager = current_app.conf.task_always_eager
        current_app.conf.task_always_eager = True
        try:
            self.link(options["dry_run"], options["pause"])
        finally:
            current_app.conf.task_always_eager = eager

    def link(self, dry_run, pause):
        prefix = "[dry-run] " if dry_run else ""
        # Every address with an account, fetched once: most unlinked rows are people with no account.
        accounts = {}
        for pk, email in (
            User.objects.filter(is_active=True).exclude(email="").order_by("-pk").values_list("pk", "email")
        ):
            accounts[normalize_email(email)] = pk

        # Members first, so a club-managed row can follow its member's account.
        members = [
            (pk, email)
            for pk, email in ClubMember.objects.filter(user__isnull=True, is_deleted=False)
            .exclude(email="")
            .values_list("pk", "email")
            if normalize_email(email) in accounts
        ]
        for pk, email in members:
            self.stdout.write(f"{prefix}ClubMember {pk} ({email}) -> user {accounts[normalize_email(email)]}")
            if dry_run:
                continue
            member = ClubMember.objects.filter(pk=pk, user__isnull=True).first()
            if member:
                member.save()
                time.sleep(pause)

        rows = [
            tos
            for tos in AuctionTOS.objects.filter(user__isnull=True).select_related("clubmember")
            if (tos.clubmember and tos.clubmember.user_id) or normalize_email(tos.email) in accounts
        ]
        for tos in rows:
            account = tos.clubmember.user_id if tos.clubmember and tos.clubmember.user_id else None
            account = account or accounts[normalize_email(tos.email)]
            self.stdout.write(f"{prefix}AuctionTOS {tos.pk} ({tos.email}) in {tos.auction_id} -> user {account}")
            if dry_run:
                continue
            # Gone if an earlier row's save merged it away; linked if a sign-in got there first.
            fresh = AuctionTOS.objects.filter(pk=tos.pk, user__isnull=True).first()
            if fresh:
                fresh.save()
                time.sleep(pause)

        lots = Lot.objects.filter(user__isnull=True, auctiontos_seller__user__isnull=False)
        claimed = lots.count()
        if not dry_run:
            for pk, owner in list(lots.values_list("pk", "auctiontos_seller__user")):
                Lot.objects.filter(pk=pk, user__isnull=True).update(user=owner)

        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{len(members)} club members and {len(rows)} auction participants linked to accounts; "
                f"{claimed} lots given their seller's account"
            )
        )
