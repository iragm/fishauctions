from django.db import migrations
from django.db.models import Q

# Clubs created before these fields existed can have blank (empty-string or NULL) email
# templates that 0322 didn't touch — it only rewrote values equal to the *old* defaults.
# Populate any still-blank field with the current default so every club has usable text.
FIELD_DEFAULTS = {
    "welcome_opening": "Thanks for joining!\n\nYou can view your membership below:",
    "welcome_closing": "See you there!\n\nBest wishes,",
    "renewal_opening": "Thanks for being a club member, and we'll see you at our next meeting.",
    "renewal_closing": "See you there!\n\nBest wishes,",
    "expiring_soon_opening": "It's time to renew your membership!  You can pay at this link:",
    "expiring_soon_closing": "See you there!\n\nBest wishes,",
}


def backfill_blank(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    for field, value in FIELD_DEFAULTS.items():
        Club.objects.filter(Q(**{field: ""}) | Q(**{f"{field}__isnull": True})).update(**{field: value})


def reverse_noop(apps, schema_editor):
    # Populating blanks is not reversible without knowing which were intentionally blank.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0322_update_club_email_template_defaults"),
    ]

    operations = [
        migrations.RunPython(backfill_blank, reverse_noop),
    ]
