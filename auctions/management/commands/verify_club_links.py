"""Fetch every club's links and record what answered.  See auctions/club_verification.py."""

import time

from django.core.management.base import BaseCommand

from auctions import club_verification
from auctions.models import Club


class Command(BaseCommand):
    help = (
        "Fetch each club's homepage and Facebook page, record whether they answered, and list the "
        "clubs where every signal of existence is now absent.\n\n"
        "Run with --dry-run to see what would be checked, or --all to re-check clubs that are not "
        "due yet."
    )

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Check every club, not only those due.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many clubs.")
        parser.add_argument("--dry-run", action="store_true", help="List what would be checked and stop.")
        parser.add_argument(
            "--delay",
            type=float,
            default=club_verification.PER_HOST_DELAY_SECONDS,
            help="Seconds between clubs. These are small sites on shared hosting; be polite.",
        )

    def handle(self, *args, **options):
        clubs = Club.objects.all() if options["all"] else club_verification.clubs_due_for_verification()
        if options["limit"]:
            clubs = clubs[: options["limit"]]
        clubs = list(clubs)
        if not clubs:
            self.stdout.write(self.style.SUCCESS("Every club has been verified within the last year."))
            return
        if options["dry_run"]:
            self.stdout.write(f"{len(clubs)} club(s) would be checked:")
            for club in clubs:
                self.stdout.write(f"  - {club.name} ({club.homepage or 'no homepage'})")
            return
        for index, club in enumerate(clubs):
            fields = club_verification.verify_club(club)
            self.stdout.write(f"{club.name}: {fields['link_check_note']}")
            # Deliberately between clubs rather than between requests: two fetches to one club are
            # its homepage and its Facebook page, which are not the same host.
            if options["delay"] and index < len(clubs) - 1:
                time.sleep(options["delay"])
        self.stdout.write(self.style.SUCCESS(f"Checked {len(clubs)} club(s)."))
        # Never written by this command.  `active` takes a club off the public map, and a club that
        # is merely quiet on the web is not a club that has folded -- a person agrees or does not.
        dead = club_verification.dead_candidates()
        if dead:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(f"{len(dead)} club(s) show no sign of existing any more:"))
            for club, reason in dead:
                self.stdout.write(f"  - {club.name} (id={club.pk}) -- {reason}")
            self.stdout.write("Set active=False by hand on any of these you agree about.")
