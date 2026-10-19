"""The Tap to Pay on iPhone launch announcement (Apple marketing requirements 6.1 and 6.3).

6.1 asks for a launch email to eligible users and 6.3 for an in-app push, both once the feature is
generally available. 6.2, the in-app splash, is in the app.

Apple's Marketing Guide and Toolkit supply the words for both and forbid substituting your own. The
email is the post_office template ``tap_to_pay_launch_email`` (seeded by migration 0501 from the
toolkit's copy deck); the push defaults to the toolkit's "Value proposition" push copy. The command
still refuses to send if someone deletes the template, rather than sending something else.

Usage::

    manage.py tap_to_pay_launch_announcement --dry-run
    manage.py tap_to_pay_launch_announcement

One-shot but safe to re-run: ``PushNotificationSent`` with the category below is the ledger for both
halves, so nobody is emailed or pushed twice.
"""

import logging

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError

from auctions.models import MobileDevice, PushNotificationSent
from auctions.notifications import CATEGORY_TAP_TO_PAY_LAUNCH

logger = logging.getLogger(__name__)

# Its own category so a re-run can't be confused with the nightly promos. Push-only, so an
# undeliverable push isn't emailed as a third message alongside the launch email.
CATEGORY = CATEGORY_TAP_TO_PAY_LAUNCH

EMAIL_TEMPLATE_NAME = "tap_to_pay_launch_email"

MISSING_EMAIL_TEMPLATE = (
    f"No post_office email template named '{EMAIL_TEMPLATE_NAME}' exists.\n"
    "Migration 0501 seeds it with the copy from Apple's Tap to Pay on iPhone Marketing Guide and "
    "Toolkit (6.1 forbids writing your own). Restore it from that migration, then run this again."
)

# The toolkit's "Value proposition" push copy (US copy deck, September 2026), word for word.
PUSH_TITLE = "Accept in-person payments with Tap to Pay on iPhone."
PUSH_BODY = (
    "Accept in-person, contactless payments right on your iPhone, from physical debit and credit cards to "
    "Apple Pay and other digital wallets. Terms apply."
)


class Command(BaseCommand):
    help = "Send the Tap to Pay on iPhone launch email and push to eligible merchants (Apple 6.1 + 6.3)"

    def add_arguments(self, parser):
        parser.add_argument("--push-title", default=PUSH_TITLE, help="Push title, from Apple's toolkit")
        parser.add_argument("--push-body", default=PUSH_BODY, help="Push body, from Apple's toolkit")
        parser.add_argument("--dry-run", action="store_true", help="List recipients without sending")
        parser.add_argument("--email-only", action="store_true", help="Requirement 6.1 only")
        parser.add_argument("--push-only", action="store_true", help="Requirement 6.3 only")

    def handle(self, *args, **options):
        do_email = not options["push_only"]
        do_push = not options["email_only"]
        dry_run = options["dry_run"]

        if do_email:
            self._require_email_template(dry_run)

        recipients = list(self.eligible_users())
        self.stdout.write(f"{len(recipients)} eligible merchant(s).")

        emailed = pushed = 0
        for user in recipients:
            already = PushNotificationSent.objects.filter(user=user, category=CATEGORY).exists()
            if already:
                continue
            if dry_run:
                self.stdout.write(f"[DRY RUN] would announce to {user} <{user.email}>")
                continue
            if do_email and user.email and not user.userdata.has_unsubscribed:
                self._send_email(user)
                emailed += 1
            if do_push:
                self._send_push(user, options["push_title"], options["push_body"])
                pushed += 1
            # The authoritative "we told this person" marker, and what makes a re-run a no-op.
            # send_push_to_user writes per-device rows, but asynchronously and only on acceptance.
            PushNotificationSent.objects.create(user=user, category=CATEGORY)

        logger.info("tap_to_pay_launch_announcement: %s email(s), %s push(es)", emailed, pushed)
        self.stdout.write(self.style.SUCCESS(f"Sent {emailed} email(s) and {pushed} push notification(s)."))

    def _require_email_template(self, dry_run):
        from post_office.models import EmailTemplate

        if EmailTemplate.objects.filter(name=EMAIL_TEMPLATE_NAME).exists():
            return
        if dry_run:
            self.stdout.write(self.style.WARNING(MISSING_EMAIL_TEMPLATE))
            return
        raise CommandError(MISSING_EMAIL_TEMPLATE)

    @staticmethod
    def eligible_users():
        """Merchants who could actually use Tap to Pay on iPhone today.

        Three necessary conditions: they administer an auction or club that could take a payment (the same
        predicate the warm-up endpoint uses), that seller's Square account has the in-person scope, and they
        have an **iPhone** registered -- Tap to Pay on iPhone is iOS-only and Apple's rules don't allow the
        name to be used towards anyone else.
        """
        from auctions.mobile.services.payments import PaymentService

        ios_user_pks = MobileDevice.objects.filter(platform=MobileDevice.PLATFORM_IOS).values_list("user_id", flat=True)
        candidates = (
            User.objects.filter(pk__in=ios_user_pks, is_active=True)
            .exclude(email="")
            .select_related("userdata")
            .distinct()
        )
        for user in candidates:
            if not PaymentService.user_can_take_payments(user):
                continue
            auction = PaymentService._latest_admin_auction(user)
            seller = auction.effective_square_seller if auction else None
            if seller and seller.supports_tap_to_pay:
                yield user

    @staticmethod
    def _send_email(user):
        from post_office import mail

        mail.send(
            user.email,
            template=EMAIL_TEMPLATE_NAME,
            context={
                "name": user.first_name,
                "domain": Site.objects.get_current().domain,
                "unsubscribe": user.userdata.unsubscribe_link,
            },
        )

    @staticmethod
    def _send_push(user, title, body):
        from django.urls import reverse

        from auctions.tasks import send_push_to_user

        send_push_to_user.delay(
            user.pk,
            title=title,
            body=body,
            # The Tap to Pay line of the payments guide: its "Terms apply." holds the disclaimers the
            # push's own "Terms apply." has to lead to, and the Square section with the reconnect
            # button for an account that predates the in-person scope is just above it.
            url=f"https://{Site.objects.get_current().domain}{reverse('help_guide', kwargs={'slug': 'payments'})}#tap-to-pay",
            category=CATEGORY,
        )
