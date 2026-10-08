"""The auction rule for welcome letters, and a backfill so no imported member is welcomed by surprise.

The nightly welcome job used to skip every member whose source was "csv". It now goes by
``send_welcome_email`` alone, so an imported row the job hadn't reached yet is marked done here, as
that job would have marked it.
"""

from django.db import migrations, models


def mark_imported_members_welcomed(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    ClubMember.objects.filter(source="csv", welcome_email_sent=False).update(
        welcome_email_sent=True, send_welcome_email=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0477_clubmember_membership_carried_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="send_club_welcome_letter",
            field=models.BooleanField(
                default=True,
                help_text="To people this auction adds to the club. Off, they get it when they first pay dues.",
                verbose_name="Send club welcome letter",
            ),
        ),
        migrations.RunPython(mark_imported_members_welcomed, migrations.RunPython.noop),
    ]
