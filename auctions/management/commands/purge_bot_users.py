from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand
from django.utils import timezone

from auctions.models import UserData


class Command(BaseCommand):
    help = "Remove users with no verified email"

    def handle(self, *args, **options):
        emails = EmailAddress.objects.filter(verified=False, primary=True)
        for email in emails:
            userData, created = UserData.objects.get_or_create(
                user=email.user,
                defaults={},
            )
            time_difference = email.user.userdata.last_activity - email.user.date_joined
            if time_difference < timezone.timedelta(hours=24):
                print("Deleting", email.user)
                email.user.delete()
