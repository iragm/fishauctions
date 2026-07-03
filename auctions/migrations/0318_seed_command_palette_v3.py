"""Third-pass command palette seed.

Adds the shortcuts requested for the palette refresh:
  * help / tutorials / video -> your latest admin auction's in-auction help (alongside the FAQ)
  * stats -> your latest admin auction's stats, and your club's stats
  * map / set location -> set the location of your latest admin auction (alongside the member map)
  * create auction / new auction -> start a new auction
  * create lot -> add a lot (synonym enrichment for the existing new-lot rows)

Follows the idempotent get_or_create + synonym-refresh pattern of 0309.
"""

from django.db import migrations
from django.urls import reverse


def _new_entries():
    return [
        # help / tutorials / video -> in-auction help for your latest admin auction.
        {
            "search_term": "help",
            "target": "last_auction:help",
            "icon": "bi-life-preserver",
            "synonyms": "tutorials, tutorial, video, videos, how to, how do i, guide, walkthrough, auction help",
        },
        {
            "search_term": "tutorials",
            "target": "last_auction:help",
            "icon": "bi-life-preserver",
            "synonyms": "tutorial, video, videos, help, how to, guide, walkthrough",
        },
        {
            "search_term": "video",
            "target": "last_auction:help",
            "icon": "bi-life-preserver",
            "synonyms": "videos, tutorial, tutorials, help, how to, walkthrough",
        },
        # stats -> auction stats and club stats.
        {
            "search_term": "stats",
            "target": "last_auction:stats",
            "icon": "bi-graph-up",
            "synonyms": "statistics, analytics, auction stats, charts, numbers, reports, results",
        },
        {
            "search_term": "stats",
            "target": "clubs:stats",
            "icon": "bi-graph-up",
            "synonyms": "statistics, analytics, club stats, membership stats, charts, trends",
        },
        # map / set location -> set the location of your latest admin auction.
        {
            "search_term": "set location",
            "target": "last_auction:set_location",
            "icon": "bi-geo-alt",
            "synonyms": "map, auction location, set map, pin location, where is the auction, venue, address",
        },
        {
            "search_term": "map",
            "target": "last_auction:set_location",
            "icon": "bi-geo-alt",
            "synonyms": "set location, auction location, set map, venue, pin location",
        },
        # create auction / new auction.
        {
            "search_term": "create auction",
            "url": reverse("create_auction"),
            "title": "Create an auction",
            "icon": "bi-hammer",
            "synonyms": "new auction, start auction, host auction, run an auction, make auction, add auction",
        },
        {
            "search_term": "new auction",
            "url": reverse("create_auction"),
            "title": "Create an auction",
            "icon": "bi-hammer",
            "synonyms": "create auction, start auction, host auction, run an auction, make auction, add auction",
        },
        # create lot -> add a lot (the new-lot page).
        {
            "search_term": "create lot",
            "url": reverse("new_lot"),
            "title": "Add a lot",
            "icon": "bi-plus-circle",
            "synonyms": "new lot, add lot, sell a lot, sell single lot, submit lot, enter lot",
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
        ("auctions", "0317_fix_legacy_uuid_columns"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
