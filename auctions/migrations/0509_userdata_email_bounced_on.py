from django.db import migrations, models
from django.utils import timezone


def date_existing_bounces(apps, schema_editor):
    """Undated red envelopes count as bouncing today, so the gate starts with them.

    Not Hide My Email addresses: those are red when the person turned forwarding off, and a verification
    email could never reach them.
    """
    AuctionTOS = apps.get_model("auctions", "AuctionTOS")
    ClubMember = apps.get_model("auctions", "ClubMember")
    UserData = apps.get_model("auctions", "UserData")
    emails = set()
    for model in (AuctionTOS, ClubMember):
        rows = (
            model.objects.filter(email_address_status="BAD")
            .exclude(email=None)
            .exclude(email="")
            .exclude(email__iendswith="@privaterelay.appleid.com")
        )
        emails.update(email.lower() for email in rows.values_list("email", flat=True).order_by().distinct())
    emails = sorted(emails)
    now = timezone.now()
    for start in range(0, len(emails), 500):
        UserData.objects.filter(user__email__in=emails[start : start + 500]).update(email_bounced_on=now)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0508_emails_link_the_help_not_the_blog"),
    ]

    operations = [
        migrations.AddField(
            model_name="userdata",
            name="email_bounced_on",
            field=models.DateTimeField(
                blank=True,
                help_text="When mail to this account's address last bounced. Joining an auction waits for the address to be verified again, which clears it (services.needs_email_reverification).",
                null=True,
            ),
        ),
        migrations.RunPython(date_existing_bounces, migrations.RunPython.noop),
    ]
