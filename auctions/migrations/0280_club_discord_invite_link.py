from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0279_club_auction_email_member_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="discord_invite_link",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
