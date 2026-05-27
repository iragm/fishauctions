from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0280_remove_reply_to_from_email_templates"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="discord_invite_link",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
