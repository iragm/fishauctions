from django.core.management.base import BaseCommand

import requests as http_requests
from auctions.google_wallet import (
    WALLET_API_BASE,
    _object_id_for_member,
    _object_visuals,
    get_access_token,
    is_configured,
    update_generic_object_for_member,
)
from auctions.models import Club, ClubMember
from auctions.tasks import create_google_wallet_class_for_club, update_google_wallet_objects_for_club


class Command(BaseCommand):
    help = (
        "Ensure a Google Wallet GenericClass exists for every club and refresh every "
        "active member's GenericObject (logo + background color live on the object)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run inline instead of dispatching to Celery (useful for one-off backfills).",
        )
        parser.add_argument(
            "--objects-only",
            action="store_true",
            help="Skip class creation and only refresh member GenericObjects.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Read-only: show what each member's GenericObject looks like on Google right now.",
        )

    def handle(self, *args, **options):
        if not is_configured():
            self.stdout.write(self.style.WARNING("Google Wallet is not configured; nothing to do."))
            return

        if options["check"]:
            self._check()
            return

        run_inline = options["sync"]
        clubs = Club.objects.all().order_by("pk")
        class_count = 0
        member_count = 0
        for club in clubs:
            if not options["objects_only"]:
                if run_inline:
                    create_google_wallet_class_for_club.apply(args=[club.pk])
                else:
                    create_google_wallet_class_for_club.delay(club.pk)
                class_count += 1

            members = ClubMember.objects.filter(club=club, is_deleted=False)
            if run_inline:
                update_google_wallet_objects_for_club.apply(args=[club.pk])
            else:
                update_google_wallet_objects_for_club.delay(club.pk)
            member_count += members.count()

        verb = "Ran" if run_inline else "Dispatched"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} class tasks for {class_count} club(s) and object refresh for {member_count} member(s)."
            )
        )

    def _check(self):
        """Read-only: GET each member's GenericObject from Google and print the logo state."""
        token = get_access_token()
        if not token:
            self.stdout.write(self.style.ERROR("Could not obtain access token."))
            return
        headers = {"Authorization": f"Bearer {token}"}

        for club in Club.objects.all().order_by("pk"):
            self.stdout.write(f"\nclub={club.pk} ({club.name!r})  has_icon={bool(club.icon)}")
            visuals = _object_visuals(club)
            self.stdout.write(f"  logo_would_send={('logo' in visuals)!r}  hex={visuals.get('hexBackgroundColor')!r}")

            members = ClubMember.objects.filter(club=club, is_deleted=False).select_related("user")
            for member in members:
                object_id = _object_id_for_member(member)
                resp = http_requests.get(
                    f"{WALLET_API_BASE}/genericObject/{object_id}",
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 404:
                    self.stdout.write(f"  member={member.pk}: NOT SAVED TO WALLET (object 404)")
                elif resp.status_code == 200:
                    obj = resp.json()
                    logo_uri = obj.get("logo", {}).get("sourceUri", {}).get("uri", None)
                    hex_bg = obj.get("hexBackgroundColor", None)
                    self.stdout.write(
                        f"  member={member.pk} ({member.name or '?'}): "
                        f"logo={logo_uri!r}  hex={hex_bg!r}"
                    )
                else:
                    self.stdout.write(self.style.ERROR(f"  member={member.pk}: HTTP {resp.status_code}"))

        self.stdout.write(self.style.SUCCESS("\nDone."))
