"""Add command palette shortcuts for the club API keys page, changing your username, and user preferences.

- "api" surfaces the palette club's API keys page (a dynamic ``clubs:api`` target).
- "username" surfaces the standalone change-username page (the field-level matcher separately
  surfaces "username visible" on the user preferences page).
- "preferences" surfaces the user preferences page directly.
"""

from django.db import migrations
from django.urls import reverse


def _new_entries():
    return [
        {
            "search_term": "api",
            "target": "clubs:api",
            "icon": "bi-key",
            "synonyms": "api, api key, api keys, club api, integration, integrations, webhook, rest api, developer",
        },
        {
            "search_term": "username",
            "url": reverse("change_username"),
            "title": "Change username",
            "icon": "bi-person-badge",
            "synonyms": "change username, rename, change my username, update username, handle, display name",
        },
        {
            "search_term": "preferences",
            "url": reverse("preferences"),
            "title": "User preferences",
            "icon": "bi-sliders",
            "synonyms": (
                "settings, my preferences, user settings, account settings, notification settings, "
                "privacy, privacy settings, email preferences"
            ),
        },
    ]


def seed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _new_entries():
        obj, _ = CommandPalettePage.objects.get_or_create(
            search_term=entry["search_term"],
            target=entry.get("target", ""),
            url=entry.get("url", ""),
            defaults={
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
                "icon": entry.get("icon", ""),
                "synonyms": entry.get("synonyms", ""),
            },
        )
        # Keep synonyms/icon fresh on idempotent re-runs.
        obj.synonyms = entry.get("synonyms", obj.synonyms)
        if entry.get("icon"):
            obj.icon = entry["icon"]
        obj.save()


def unseed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _new_entries():
        CommandPalettePage.objects.filter(
            search_term=entry["search_term"],
            target=entry.get("target", ""),
            url=entry.get("url", ""),
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0311_userdata_last_club_used"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
