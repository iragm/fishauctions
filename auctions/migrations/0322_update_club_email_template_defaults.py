from django.db import migrations, models

# The (old default -> new default) text for each club email template field.
# We only rewrite a club's value when it still exactly matches the old default,
# so any wording a club has customized is left untouched.
OLD_TO_NEW = {
    "welcome_opening": ("Thanks for joining!", "Thanks for joining!\n\nYou can view your membership below:"),
    "welcome_closing": ("Best wishes,", "See you there!\n\nBest wishes,"),
    "renewal_opening": (
        "Your membership has been renewed!",
        "Thanks for being a club member, and we'll see you at our next meeting.",
    ),
    "renewal_closing": ("Best wishes,", "See you there!\n\nBest wishes,"),
    "expiring_soon_opening": (
        "Your membership expires soon",
        "It's time to renew your membership!  You can pay at this link:",
    ),
    "expiring_soon_closing": ("Best wishes,", "See you there!\n\nBest wishes,"),
}


def backfill_defaults(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    for field, (old, new) in OLD_TO_NEW.items():
        Club.objects.filter(**{field: old}).update(**{field: new})


def reverse_backfill(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    for field, (old, new) in OLD_TO_NEW.items():
        Club.objects.filter(**{field: new}).update(**{field: old})


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0321_adcampaign_cloudflare_image_id_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="club",
            name="welcome_opening",
            field=models.TextField(
                blank=True,
                default="Thanks for joining!\n\nYou can view your membership below:",
                verbose_name="Welcome email opening text",
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="welcome_closing",
            field=models.TextField(
                blank=True, default="See you there!\n\nBest wishes,", verbose_name="Welcome email closing text"
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="renewal_opening",
            field=models.TextField(
                blank=True,
                default="Thanks for being a club member, and we'll see you at our next meeting.",
                verbose_name="Renewal email opening text",
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="renewal_closing",
            field=models.TextField(
                blank=True, default="See you there!\n\nBest wishes,", verbose_name="Renewal email closing text"
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="expiring_soon_opening",
            field=models.TextField(
                blank=True,
                default="It's time to renew your membership!  You can pay at this link:",
                verbose_name="Expiring soon email opening text",
            ),
        ),
        migrations.AlterField(
            model_name="club",
            name="expiring_soon_closing",
            field=models.TextField(
                blank=True, default="See you there!\n\nBest wishes,", verbose_name="Expiring soon email closing text"
            ),
        ),
        migrations.RunPython(backfill_defaults, reverse_backfill),
    ]
