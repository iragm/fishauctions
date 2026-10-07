"""Put printed bidder paddles in the palette, and take "bidder paddles" off the barcode page's row.

Typing "paddles" used to find only the club's Print Barcodes page, whose paddle option is a sticker for a
paddle you already own. Now the printed kind has a row of its own, and the barcode row says stickers.

Idempotent get_or_create + synonym refresh, matching 0336/0345.
"""

from django.db import migrations


def _entries():
    return [
        {
            "search_term": "print paddles",
            "target": "last_auction:auction_paddles",
            "icon": "bi-123",
            "synonyms": (
                "print, printing, paddles, paddle, bidder paddles, print bidder paddles, bidder cards, "
                "print bidder cards, bidder number cards, reprint a paddle"
            ),
        },
        {
            "search_term": "print barcodes",
            "target": "clubs:barcode_labels",
            "icon": "bi-upc-scan",
            "synonyms": (
                "print, labels, barcodes, barcode labels, membership cards, member cards, "
                "membership labels, paddle stickers, print membership barcodes, print member cards"
            ),
        },
    ]


def seed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _entries():
        obj, _ = CommandPalettePage.objects.get_or_create(
            search_term=entry["search_term"],
            target=entry["target"],
            url="",
            defaults={"icon": entry["icon"], "synonyms": entry["synonyms"]},
        )
        obj.synonyms = entry["synonyms"]
        obj.icon = entry["icon"]
        obj.save()


def unseed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    CommandPalettePage.objects.filter(search_term="print paddles", target="last_auction:auction_paddles").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0486_auctiontos_paddle_printed"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
