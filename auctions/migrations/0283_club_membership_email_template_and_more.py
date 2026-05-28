from django.db import migrations, models


def backfill_existing_club_members(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    ClubMember.objects.update(send_welcome_email=False, welcome_email_sent=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0282_recalculate_all_auction_stats"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="membership_email_template",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="club",
            name="send_membership_expiration_reminders_30_days",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="club",
            name="send_membership_renewal_confirmation",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="club",
            name="send_welcome_email_to_new_members",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clubmember",
            name="membership_expiration_reminder_30_days_due",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clubmember",
            name="send_welcome_email",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="clubmember",
            name="welcome_email_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_existing_club_members, migrations.RunPython.noop),
    ]
