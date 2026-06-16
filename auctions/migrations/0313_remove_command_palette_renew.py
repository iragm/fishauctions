"""Remove the membership-renew/pay command palette shortcut.

The club membership *payment* page (``club_membership_pay``) now redirects members whose dues
are current back to their membership card, and it should never be offered directly in the
palette. The palette surfaces the member's UUID membership card instead (searching card /
membership / member / a club name). This deletes the seeded ``clubs:renew`` row.
"""

from django.db import migrations

RENEW_SYNONYMS = "membership, renew membership, pay dues, dues, membership renewal, pay membership"


def remove(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    CommandPalettePage.objects.filter(target="clubs:renew").delete()


def restore(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    CommandPalettePage.objects.get_or_create(
        search_term="renew",
        target="clubs:renew",
        url="",
        defaults={"icon": "bi-arrow-repeat", "synonyms": RENEW_SYNONYMS},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0312_seed_command_palette_api_username"),
    ]

    operations = [
        migrations.RunPython(remove, restore),
    ]
