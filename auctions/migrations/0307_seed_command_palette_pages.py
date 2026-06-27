from django.db import migrations
from django.urls import reverse


def _entries():
    """Default phrase -> page mappings.

    Dynamic entries use ``target`` (resolved per-user at request time); static entries
    use ``url`` (a literal path, computed here via reverse so it tracks the URLconf).
    Multiple rows may share a search_term, exactly as the examples in the spec describe
    (e.g. "sell lots" maps to both the set-lot-winners page and the selling page).
    """
    return [
        # Dynamic, resolved against the user's most recent auction / administered club.
        {"search_term": "sell lots", "target": "last_auction:set_winners", "icon": "bi-cash-coin"},
        {"search_term": "set winners", "target": "last_auction:set_winners", "icon": "bi-calendar-check"},
        {"search_term": "set lot winners", "target": "last_auction:set_winners", "icon": "bi-calendar-check"},
        {"search_term": "winners", "target": "last_auction:set_winners", "icon": "bi-calendar-check"},
        {"search_term": "view users", "target": "last_auction:view_users", "icon": "bi-people-fill"},
        {"search_term": "users", "target": "last_auction:view_users", "icon": "bi-people-fill"},
        {"search_term": "manage users", "target": "last_auction:view_users", "icon": "bi-people-fill"},
        {"search_term": "view lots", "target": "last_auction:view_lots", "icon": "bi-grid"},
        {"search_term": "checkout", "target": "last_auction:quick_checkout", "icon": "bi-bag-heart"},
        {"search_term": "quick checkout", "target": "last_auction:quick_checkout", "icon": "bi-bag-heart"},
        {"search_term": "check out", "target": "last_auction:quick_checkout", "icon": "bi-bag-heart"},
        {"search_term": "bap", "target": "last_auction:bap", "icon": "bi-award"},
        {"search_term": "hap", "target": "last_auction:bap", "icon": "bi-award"},
        {"search_term": "breeder award", "target": "last_auction:bap", "icon": "bi-award"},
        {"search_term": "members", "target": "club:members", "icon": "bi-people-fill"},
        {"search_term": "club members", "target": "club:members", "icon": "bi-people-fill"},
        {"search_term": "email", "target": "club:brevo", "icon": "bi-envelope"},
        {"search_term": "brevo", "target": "club:brevo", "icon": "bi-envelope"},
        # Static destinations.
        {"search_term": "sell lots", "url": reverse("selling"), "title": "Selling", "icon": "bi-cash-coin"},
        {"search_term": "selling", "url": reverse("selling"), "title": "Selling", "icon": "bi-cash-coin"},
        {
            "search_term": "address",
            "url": reverse("contact_info"),
            "title": "Contact info",
            "icon": "bi-telephone-fill",
        },
        {
            "search_term": "contact info",
            "url": reverse("contact_info"),
            "title": "Contact info",
            "icon": "bi-telephone-fill",
        },
        {"search_term": "phone", "url": reverse("contact_info"), "title": "Contact info", "icon": "bi-telephone-fill"},
        {"search_term": "add lot", "url": reverse("new_lot"), "title": "Add a lot", "icon": "bi-plus-circle"},
        {"search_term": "new lot", "url": reverse("new_lot"), "title": "Add a lot", "icon": "bi-plus-circle"},
        {"search_term": "lots", "url": reverse("allLots"), "title": "All lots", "icon": "bi-grid"},
        {"search_term": "watched", "url": reverse("watched"), "title": "Watched lots", "icon": "bi-heart-fill"},
        {"search_term": "watched lots", "url": reverse("watched"), "title": "Watched lots", "icon": "bi-heart-fill"},
        {"search_term": "invoices", "url": reverse("my_invoices"), "title": "My invoices", "icon": "bi-bag"},
        {"search_term": "my invoices", "url": reverse("my_invoices"), "title": "My invoices", "icon": "bi-bag"},
        {"search_term": "preferences", "url": reverse("preferences"), "title": "Preferences", "icon": "bi-sliders"},
        {"search_term": "settings", "url": reverse("preferences"), "title": "Preferences", "icon": "bi-sliders"},
        {"search_term": "print labels", "url": reverse("printing"), "title": "Label printing", "icon": "bi-printer"},
        {"search_term": "labels", "url": reverse("printing"), "title": "Label printing", "icon": "bi-printer"},
        {"search_term": "printing", "url": reverse("printing"), "title": "Label printing", "icon": "bi-printer"},
        {"search_term": "find a club", "url": reverse("clubs"), "title": "Find a club", "icon": "bi-people"},
        {"search_term": "clubs", "url": reverse("clubs"), "title": "Find a club", "icon": "bi-people"},
        {"search_term": "faq", "url": reverse("faq"), "title": "FAQ", "icon": "bi-question-circle"},
        {"search_term": "help", "url": reverse("faq"), "title": "FAQ", "icon": "bi-question-circle"},
        {"search_term": "feedback", "url": reverse("feedback"), "title": "Leave feedback", "icon": "bi-chat-heart"},
        {"search_term": "messages", "url": reverse("messages"), "title": "Messages", "icon": "bi-chat"},
    ]


def seed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _entries():
        CommandPalettePage.objects.get_or_create(
            search_term=entry["search_term"],
            target=entry.get("target", ""),
            url=entry.get("url", ""),
            defaults={
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
                "icon": entry.get("icon", ""),
            },
        )


def unseed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _entries():
        CommandPalettePage.objects.filter(
            search_term=entry["search_term"],
            target=entry.get("target", ""),
            url=entry.get("url", ""),
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0306_commandpalettepage_commandpalettesearch"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
