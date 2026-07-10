from django.core.management.base import BaseCommand, CommandError

from auctions.models import Auction, Club, ClubHistory, ClubMember


class Command(BaseCommand):
    help = (
        "Assign every clubless auction whose name matches a filter to a club, "
        "and make each auction's creator a club admin.\n\n"
        'Usage: assign_auction_to_club "auction filter" "club name"\n'
        "Run with no arguments to list every non-deleted auction that has no club."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "auction_filter",
            nargs="?",
            help="Case-insensitive substring matched against auction names. Omit to list all clubless auctions.",
        )
        parser.add_argument(
            "club_name", nargs="?", help="Case-insensitive substring matched against club names (first match wins)"
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt (assume yes).",
        )

    def handle(self, *args, **options):
        auction_filter = options["auction_filter"]
        club_name = options["club_name"]

        # No filter given: just list every clubless auction so the operator can pick one.
        if not auction_filter:
            self._list_clubless_auctions()
            return

        if not club_name:
            msg = (
                "Provide a club name to assign matching auctions to (run with no arguments to list clubless auctions)."
            )
            raise CommandError(msg)

        club = Club.objects.filter(name__icontains=club_name).order_by("pk").first()
        if not club:
            msg = f"No club matches '{club_name}'."
            raise CommandError(msg)

        auctions = list(
            Auction.objects.filter(
                title__icontains=auction_filter,
                club__isnull=True,
                is_deleted=False,
            ).order_by("pk")
        )
        if not auctions:
            self.stdout.write(self.style.WARNING(f"No clubless auctions match '{auction_filter}'. Nothing to do."))
            return

        self.stdout.write(f"Club: {club.name} (id={club.pk})")
        self.stdout.write(f"The following {len(auctions)} auction(s) will be assigned to {club.name}:")
        for auction in auctions:
            creator = auction.created_by
            creator_label = creator.username if creator else "(no creator)"
            self.stdout.write(f"  - {auction.title} (id={auction.pk}) — creator: {creator_label}")

        if not options["yes"]:
            answer = input("Continue? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                self.stdout.write(self.style.WARNING("Aborted. No changes made."))
                return

        assigned = 0
        admins_granted = 0
        for auction in auctions:
            auction.club = club
            # save() books the club ledger for already-settled invoices when a club is first attached.
            auction.save(update_fields=["club"])
            auction.create_history(
                applies_to="RULES",
                action=f"Assigned to club '{club}' via assign_auction_to_club management command.",
                user=None,
            )
            assigned += 1

            creator = auction.created_by
            if creator and self._ensure_club_admin(club, creator):
                admins_granted += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Assigned {assigned} auction(s) to {club.name}; granted admin to {admins_granted} creator(s)."
            )
        )

    def _list_clubless_auctions(self):
        auctions = Auction.objects.filter(club__isnull=True, is_deleted=False).order_by("pk")
        count = auctions.count()
        if not count:
            self.stdout.write(self.style.WARNING("No non-deleted auctions without a club."))
            return
        self.stdout.write(f"{count} non-deleted auction(s) with no club:")
        for auction in auctions:
            creator = auction.created_by
            creator_label = creator.username if creator else "(no creator)"
            self.stdout.write(f"  - {auction.title} (id={auction.pk}) — creator: {creator_label}")

    def _ensure_club_admin(self, club, user):
        """Make `user` an admin of `club`. Returns True if admin was newly granted."""
        member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).first()
        if member and member.permission_admin:
            return False
        if not member:
            member = ClubMember(club=club, user=user, source="manually_added")
        # Populate contact fields from the user account without overwriting anything already set.
        member.name = member.name or user.get_full_name() or user.username
        if not member.email:
            member.email = user.email or None
        member.permission_admin = True
        member.save()
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Granted admin permissions to {member.name} via assign_auction_to_club management command.",
            applies_to="MEMBERS",
        )
        return True
