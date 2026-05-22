import datetime

from django.db import migrations


def backfill_expiration_date(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    today = datetime.datetime.now(tz=datetime.timezone.utc).date()
    members = ClubMember.objects.filter(
        membership_expiration_date__isnull=True,
        membership_last_paid__isnull=False,
    ).select_related("club")
    to_update = []
    for member in members:
        system = member.club.membership_system
        if system == "january_first":
            member.membership_expiration_date = datetime.date(member.membership_last_paid.year + 1, 1, 1)
        else:
            member.membership_expiration_date = member.membership_last_paid + datetime.timedelta(days=365)
        # Schedule a reminder only if the expiration is in the future and reminders are enabled
        if (
            member.membership_expiration_date > today
            and member.club.send_membership_expiration_reminders
            and member.club.membership_annual_fee
            and member.email
        ):
            reminder_date = member.membership_expiration_date - datetime.timedelta(days=1)
            member.membership_expiration_reminder_due = datetime.datetime.combine(
                reminder_date, datetime.time(hour=12), tzinfo=datetime.timezone.utc
            )
        to_update.append(member)
    ClubMember.objects.bulk_update(to_update, ["membership_expiration_date", "membership_expiration_reminder_due"])


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0260_clubmember_membership_expiration_date"),
    ]

    operations = [
        migrations.RunPython(backfill_expiration_date, migrations.RunPython.noop),
    ]
