"""Add command palette shortcuts for printing and labels.

Typing "print" or "label" should surface: printing the user's labels for their most recent
auction, the per-auction label setup (what fields print on labels), and the global printing
preferences page (label size, margins, printer setup).
"""

from django.db import migrations
from django.urls import reverse


def _new_entries():
    return [
        {
            "search_term": "print labels",
            "target": "last_auction:print_labels",
            "icon": "bi-printer",
            "synonyms": (
                "print, label, labels, print my labels, print lot labels, "
                "print labels for last auction, print labels for your last auction"
            ),
        },
        {
            "search_term": "label setup",
            "target": "last_auction:label_setup",
            "icon": "bi-tags",
            "synonyms": (
                "label, labels, label fields, what prints on labels, configure labels, "
                "label settings, auction label setup, label preferences for your last auction"
            ),
        },
        {
            "search_term": "printing preferences",
            "url": reverse("printing"),
            "title": "Printing preferences",
            "icon": "bi-sliders",
            "synonyms": (
                "print, label, printing, print preferences, label preferences, label size, "
                "page size, margins, printer setup, print settings, printing settings"
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
        ("auctions", "0309_seed_command_palette_pages_v2"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
