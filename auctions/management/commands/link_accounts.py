"""Link every participant row to its person's account, once, for rows written before saving did it.

``AuctionTOS.link_user`` and ``ClubMember.save`` link on every save, and sign-in and email changes catch
rows added before the account existed, so everything "mine" can match on ``user`` alone. This does the
same to the rows that already exist, and gives lots whose seller row is linked their ``Lot.user``.
Saving does the linking, so duplicates in one auction merge exactly as they do live. Safe to rerun.
"""

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

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        prefix = "[dry-run] " if dry_run else ""
        # Every address with an account, fetched once: most unlinked rows are people with no account.
        accounts = {}
        for pk, email in (
            User.objects.filter(is_active=True).exclude(email="").order_by("-pk").values_list("pk", "email")
        ):
            accounts[normalize_email(email)] = pk

        # Members first, so a club-managed row can follow its member's account.
        members = [
            member
            for member in ClubMember.objects.filter(user__isnull=True, is_deleted=False).exclude(email="")
            if normalize_email(member.email) in accounts
        ]
        for member in members:
            self.stdout.write(
                f"{prefix}ClubMember {member.pk} ({member.email}) -> user {accounts[normalize_email(member.email)]}"
            )
            if not dry_run:
                member.save()

        rows = [
            tos
            for tos in AuctionTOS.objects.filter(user__isnull=True).select_related("clubmember")
            if (tos.clubmember and tos.clubmember.user_id) or normalize_email(tos.email) in accounts
        ]
        for tos in rows:
            account = tos.clubmember.user_id if tos.clubmember and tos.clubmember.user_id else None
            account = account or accounts[normalize_email(tos.email)]
            self.stdout.write(f"{prefix}AuctionTOS {tos.pk} ({tos.email}) in {tos.auction_id} -> user {account}")
            # An earlier row's save may have merged this one away.
            if not dry_run and AuctionTOS.objects.filter(pk=tos.pk).exists():
                tos.save()

        lots = Lot.objects.filter(user__isnull=True, auctiontos_seller__user__isnull=False)
        claimed = lots.count()
        if not dry_run:
            for pk, owner in lots.values_list("pk", "auctiontos_seller__user"):
                Lot.objects.filter(pk=pk).update(user=owner)

        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{len(members)} club members and {len(rows)} auction participants linked to accounts; "
                f"{claimed} lots given their seller's account"
            )
        )
