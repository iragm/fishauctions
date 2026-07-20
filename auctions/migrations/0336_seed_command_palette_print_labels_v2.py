"""Broaden the command palette's "print" / "labels" results.

Typing "print" or "labels" already surfaced the user's own label print, the label-field setup, and
the global printing-preferences page. It missed most of the label-printing pages that the auction
/print/ hub links to, so add:

  * the admin bulk-print page (print/reprint labels for the whole auction),
  * printing only the not-yet-printed labels for your own lots,
  * the club barcode-labels page (membership cards / bidder paddles / barcode stickers).

Every new row carries "print" and "labels" in its synonyms so the two broad searches surface them.
Idempotent get_or_create + synonym refresh, matching 0310/0318.
"""

from django.db import migrations


def _new_entries():
    return [
        {
            "search_term": "print all labels",
            "target": "last_auction:auction_printing",
            "icon": "bi-printer",
            "synonyms": (
                "print, labels, print labels, printing, bulk print, print all labels, "
                "print everyone's labels, auction printing, print labels for the whole auction, "
                "registration desk labels, reprint labels, label printing"
            ),
        },
        {
            "search_term": "print unprinted labels",
            "target": "last_auction:print_unprinted",
            "icon": "bi-printer",
            "synonyms": (
                "print, labels, unprinted labels, print new labels, print remaining labels, "
                "only unprinted, labels not printed yet, print the rest of my labels"
            ),
        },
        {
            "search_term": "print barcodes",
            "target": "clubs:barcode_labels",
            "icon": "bi-upc-scan",
            "synonyms": (
                "print, labels, barcodes, barcode labels, membership cards, member cards, "
                "membership labels, bidder paddles, print membership barcodes, print member cards"
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
        ("auctions", "0335_lotobservation_yaw_deg_lotposition_component"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
