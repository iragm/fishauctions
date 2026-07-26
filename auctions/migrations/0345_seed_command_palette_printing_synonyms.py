"""Make the palette's print/label shortcuts reachable from every wording.

The three "print for your last auction" shortcuts should each surface for all four of the
broad searches a user is likely to type — ``print``, ``label``, ``labels`` and ``printing``:

  * ``print my labels``      -> your own lot labels (shown while the auction isn't pretty-much-over;
                                the page itself redirects to the auction rules page when printing
                                isn't allowed yet, e.g. an online auction that hasn't ended),
  * ``/print/``              -> the whole-auction printing hub (admins only),
  * ``/print-label-setup/``  -> what fields print on the labels (admins only).

Substring matching already covered most of these, but ``printing`` didn't match the label-setup
row (its synonyms carried no "print" token). Refresh all three rows with synonyms that explicitly
contain print / printing / label / labels so none of the four searches can miss any of them.

Idempotent synonym refresh keyed on (search_term, target), matching 0310/0318/0336.
"""

from django.db import migrations


def _entries():
    return [
        {
            "search_term": "print labels",
            "target": "last_auction:print_labels",
            "icon": "bi-printer",
            "synonyms": (
                "print, printing, label, labels, print my labels, print lot labels, "
                "print labels for last auction, print labels for your last auction"
            ),
        },
        {
            "search_term": "print all labels",
            "target": "last_auction:auction_printing",
            "icon": "bi-printer",
            "synonyms": (
                "print, printing, label, labels, print labels, bulk print, print all labels, "
                "print everyone's labels, auction printing, print labels for the whole auction, "
                "registration desk labels, reprint labels, label printing"
            ),
        },
        {
            "search_term": "label setup",
            "target": "last_auction:label_setup",
            "icon": "bi-tags",
            "synonyms": (
                "print, printing, label, labels, print label setup, label print setup, "
                "label fields, what prints on labels, configure labels, label settings, "
                "auction label setup, label preferences for your last auction"
            ),
        },
    ]


def seed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _entries():
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
        obj.synonyms = entry.get("synonyms", obj.synonyms)
        if entry.get("icon"):
            obj.icon = entry["icon"]
        obj.save()


def unseed(apps, schema_editor):
    # Non-destructive: these rows predate this migration (seeded in 0310/0336), so leave them.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0344_club_paypal_webhook_id_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
