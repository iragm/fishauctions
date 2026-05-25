"""
Data migration: link existing ClubMember rows to a User when their email matches.

ClubMember.save() now auto-associates user via email on every save, but historical
records created before that hook are still missing the link. This backfill walks unlinked active members and points them at the first matching
User (case-insensitive email).
"""

from django.db import migrations


def backfill_clubmember_user(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    User = apps.get_model("auth", "User")
    unlinked = (
        ClubMember.objects.filter(user__isnull=True, is_deleted=False).exclude(email="").exclude(email__isnull=True)
    )
    updates = []
    batch_size = 500
    for member in unlinked.iterator(chunk_size=500):
        match = User.objects.filter(email__iexact=member.email).order_by("pk").first()
        if match:
            member.user = match
            updates.append(member)
            if len(updates) >= batch_size:
                ClubMember.objects.bulk_update(updates, ["user"], batch_size=batch_size)
                updates = []
    if updates:
        ClubMember.objects.bulk_update(updates, ["user"], batch_size=batch_size)


def forwards(apps, schema_editor):
    backfill_clubmember_user(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0276_auctiontos_checked_in_auctiontos_door_prize_called"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
