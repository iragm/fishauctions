from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from auctions.site_setup import ensure_single_club_membership_for_user, get_single_club, single_club_manage_mode


class Command(BaseCommand):
    help = "Ensure startup defaults like single-club mode and a debug admin user."

    def handle(self, *args, **options):
        User = get_user_model()

        if settings.DEBUG and not User.objects.exists():
            admin_email = getattr(settings, "ADMIN_EMAIL", "admin@example.com") or "admin@example.com"
            user = User.objects.create_superuser("admin", admin_email, "example")
            EmailAddress.objects.get_or_create(
                user=user,
                email=admin_email,
                defaults={"verified": True, "primary": True},
            )
            self.stdout.write(
                self.style.WARNING("Created debug admin account username=admin ****** Change the password immediately.")
            )

        if not getattr(settings, "SINGLE_CLUB_MODE", False):
            return

        club = get_single_club(create=True)
        if club:
            self.stdout.write(self.style.SUCCESS(f"Single club mode ready: {club.name}"))

        for user in User.objects.order_by("pk"):
            ensure_single_club_membership_for_user(user)

        from auctions.models import Auction

        updated = Auction.objects.filter(club__isnull=True, is_deleted=False).update(
            club=club,
            manage_users_through_club=single_club_manage_mode(),
        )
        if updated:
            self.stdout.write(self.style.SUCCESS(f"Assigned {updated} auction(s) to the single club"))
