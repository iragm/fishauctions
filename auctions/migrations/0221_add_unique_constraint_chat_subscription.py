# Generated manually to add unique constraint to ChatSubscription

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0220_remove_duplicate_chat_subscriptions"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="chatsubscription",
            constraint=models.UniqueConstraint(fields=["user", "lot"], name="unique_user_lot_subscription"),
        ),
    ]
