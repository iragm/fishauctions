# Generated migration to remove add_people_from_auction_to_club field

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0290_alter_club_expiring_soon_closing_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="auction",
            name="add_people_from_auction_to_club",
        ),
    ]
