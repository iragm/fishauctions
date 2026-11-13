"""Tell each account that seems to share an owner with another one, once, that accounts can be merged.

Counts only, unless given ``--send``. See ``auctions.duplicate_accounts`` for what counts as a likely
duplicate. Nothing printed names an account or an address.
"""

from django.core.management.base import BaseCommand

from auctions.duplicate_accounts import accounts_to_email, likely_duplicate_pairs, send_merge_suggestions


class Command(BaseCommand):
    help = "Email each likely duplicate account, once, that accounts can be merged. Counts only without --send."

    def add_arguments(self, parser):
        parser.add_argument("--send", action="store_true", help="Send the emails. Without it, only count.")

    def handle(self, *args, **options):
        if options["send"]:
            self.stdout.write(f"Sent {send_merge_suggestions()} emails.")
            return
        pairs = likely_duplicate_pairs()
        self.stdout.write(f"{len(pairs)} likely duplicate pairs; {len(accounts_to_email())} accounts would be emailed.")
