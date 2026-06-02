from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0286_club_membership_email_template_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="include_next_auction_in_emails",
            field=models.BooleanField(default=True),
        ),
    ]
