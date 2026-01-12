# Generated manually to remove duplicate ChatSubscription records

from django.db import migrations


def remove_duplicate_subscriptions(apps, schema_editor):
    """Remove duplicate ChatSubscription records, keeping the most recent one"""
    ChatSubscription = apps.get_model("auctions", "ChatSubscription")
    db_alias = schema_editor.connection.alias

    # Get all user-lot combinations that have duplicates
    from django.db.models import Count

    duplicates = (
        ChatSubscription.objects.using(db_alias).values("user", "lot").annotate(count=Count("id")).filter(count__gt=1)
    )

    # For each duplicate set, keep the most recent one and delete the rest
    for dup in duplicates:
        user_id = dup["user"]
        lot_id = dup["lot"]
        subscriptions = (
            ChatSubscription.objects.using(db_alias).filter(user_id=user_id, lot_id=lot_id).order_by("-createdon")
        )

        # Keep the first (most recent) and delete the rest
        subscriptions_to_delete = subscriptions[1:]
        for sub in subscriptions_to_delete:
            sub.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0219_update_privacy_blog_post"),
    ]

    operations = [
        migrations.RunPython(remove_duplicate_subscriptions, reverse_code=migrations.RunPython.noop),
    ]
