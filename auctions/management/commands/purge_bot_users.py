import logging

from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

#: A bot account is one that never came back, which a week is long enough to tell from a slow verifier.
MINIMUM_ACCOUNT_AGE = timezone.timedelta(days=7)


class Command(BaseCommand):
    help = "Remove users with no verified email who were only active on the day they joined"

    def handle(self, *args, **options):
        emails = EmailAddress.objects.filter(
            verified=False, primary=True, user__date_joined__lte=timezone.now() - MINIMUM_ACCOUNT_AGE
        ).select_related("user__userdata")
        for email in emails:
            time_difference = email.user.userdata.last_activity - email.user.date_joined
            if time_difference < timezone.timedelta(hours=24):
                logger.info("Deleting %s", email.user)
                email.user.delete()
