from django.db import migrations
from django.utils import timezone


def schedule_all_auction_stats(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    Auction.objects.filter(is_deleted=False, date_start__isnull=False).update(next_update_due=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0281_club_discord_invite_link"),
    ]

    operations = [
        migrations.RunPython(schedule_all_auction_stats, migrations.RunPython.noop),
    ]
