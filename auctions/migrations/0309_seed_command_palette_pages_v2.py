"""Second-pass command palette seed: rename renamed targets, enrich synonyms, add new shortcuts.

The first pass (0307) used single-club target keys (``club:members`` / ``club:brevo``); those were
renamed to the plural multi-club keys (``clubs:members`` / ``clubs:brevo``). This migration fixes the
existing rows, adds rich synonyms so the palette works well from day one, and seeds the many new
shortcuts (single-lot/bulk add, auction settings/custom fields, club map/discord/mailchimp/payments,
membership renewal, PayPal/Square, change email, Google sign-in, etc.).
"""

from django.db import migrations
from django.urls import reverse

# target key -> synonyms applied to every existing row with that target.
SYNONYMS_BY_TARGET = {
    "last_auction:set_winners": "sell lots, set lot winners, winners, mark sold, who won, sold, sell",
    "last_auction:view_users": "users, manage users, bidders, participants, attendees, buyers, sellers",
    "last_auction:view_lots": "lots, browse lots, all lots",
    "last_auction:quick_checkout": "checkout, quick checkout, check out, cash out, take payment, register, pay",
    "last_auction:bap": "bap, hap, breeder award, breeder award program, points",
    "clubs:members": "members, club members, manage members, membership list, roster",
    "clubs:brevo": "email, newsletter, mailing list, brevo, email blast",
}

TARGET_RENAMES = {"club:members": "clubs:members", "club:brevo": "clubs:brevo"}


def _new_entries():
    return [
        # --- last auction ---
        {
            "search_term": "auction",
            "target": "last_auction:self",
            "icon": "bi-hammer",
            "synonyms": "my auction, current auction, last auction, this auction",
        },
        {
            "search_term": "add lot",
            "target": "last_auction:add_lot",
            "icon": "bi-plus-circle",
            "synonyms": "new lot, sell a lot, sell single lot, single lot, submit lot, enter lot, add a lot",
        },
        {
            "search_term": "bulk add lots",
            "target": "last_auction:bulk_add_lots",
            "icon": "bi-card-list",
            "synonyms": "bulk lots, add many lots, import lots, bulk add, multiple lots, sell in bulk, sell many lots",
        },
        {
            "search_term": "auction rules",
            "target": "last_auction:edit",
            "icon": "bi-gear",
            "synonyms": (
                "rules, edit auction, auction settings, settings, fees, tax, lot fee, unsold fee, "
                "pickup, pickup location, dates, end date, start date, currency, terms, reserve, buy now"
            ),
        },
        {
            "search_term": "custom fields",
            "target": "last_auction:custom_fields",
            "icon": "bi-input-cursor-text",
            "synonyms": "custom field, dropdown, extra fields, lot fields",
        },
        {
            "search_term": "invoice",
            "target": "last_auction:invoice",
            "icon": "bi-bag",
            "synonyms": "invoices, my invoice, receipt, bill, what i owe, what i'm owed, paypal, square, get paid, payout",
        },
        # --- clubs (multi) ---
        {
            "search_term": "members",
            "target": "clubs:members",
            "icon": "bi-people-fill",
            "synonyms": "club members, manage members, membership list, roster",
        },
        {
            "search_term": "map",
            "target": "clubs:map",
            "icon": "bi-map",
            "synonyms": "member map, club map, where are members, member locations",
        },
        {
            "search_term": "email",
            "target": "clubs:brevo",
            "icon": "bi-envelope",
            "synonyms": "newsletter, mailing list, brevo, email blast, email members",
        },
        {
            "search_term": "mailchimp",
            "target": "clubs:mailchimp",
            "icon": "bi-envelope",
            "synonyms": "email, newsletter, mailing list, email members",
        },
        {
            "search_term": "discord",
            "target": "clubs:discord",
            "icon": "bi-discord",
            "synonyms": "discord setup, discord roles, chat",
        },
        {
            "search_term": "club payments",
            "target": "clubs:payments",
            "icon": "bi-credit-card",
            "synonyms": "club paypal, club square, payment setup, accept payment, take payment online, dues setup",
        },
        {
            "search_term": "renew",
            "target": "clubs:renew",
            "icon": "bi-arrow-repeat",
            "synonyms": "membership, renew membership, pay dues, dues, membership renewal, pay membership",
        },
        # --- account / payments ---
        {
            "search_term": "paypal",
            "target": "user:paypal",
            "icon": "bi-paypal",
            "synonyms": "pay pal, get paid, payout, connect paypal",
        },
        {
            "search_term": "square",
            "target": "user:square",
            "icon": "bi-credit-card",
            "synonyms": "connect square, card reader, get paid",
        },
        {
            "search_term": "change email",
            "target": "account:email",
            "icon": "bi-envelope-at",
            "synonyms": "email address, update email, my email",
        },
        {
            "search_term": "google",
            "target": "account:google",
            "icon": "bi-google",
            "synonyms": "sign in with google, connect google, login with google, google login",
        },
        {
            "search_term": "password",
            "target": "account:password",
            "icon": "bi-person-fill-lock",
            "synonyms": "change password, reset password, new password",
        },
        # --- extra static pages ---
        {
            "search_term": "won lots",
            "url": reverse("won_lots"),
            "title": "Won lots",
            "icon": "bi-calendar-check",
            "synonyms": "won, purchases, bought, what i won",
        },
        {
            "search_term": "bids",
            "url": reverse("my_bids"),
            "title": "My bids",
            "icon": "bi-coin",
            "synonyms": "my bids, bidding",
        },
        {
            "search_term": "messages",
            "url": reverse("messages"),
            "title": "Messages",
            "icon": "bi-chat",
            "synonyms": "chat, my messages, lot messages, notifications",
        },
        {
            "search_term": "account",
            "url": reverse("account"),
            "title": "My account",
            "icon": "bi-person",
            "synonyms": "my account, profile, account information",
        },
        {
            "search_term": "ignore categories",
            "url": reverse("ignore_categories"),
            "title": "Ignore categories",
            "icon": "bi-ban",
            "synonyms": "blocked categories, hide categories, mute categories",
        },
    ]


def seed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")

    for old, new in TARGET_RENAMES.items():
        CommandPalettePage.objects.filter(target=old).update(target=new)

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
        # Always keep synonyms/icon fresh (idempotent re-runs and enrichment of pre-existing rows).
        obj.synonyms = entry.get("synonyms", obj.synonyms)
        if entry.get("icon"):
            obj.icon = entry["icon"]
        obj.save()

    # Enrich synonyms on every row pointing at the well-known targets (covers 0307 rows too).
    for target, synonyms in SYNONYMS_BY_TARGET.items():
        CommandPalettePage.objects.filter(target=target).update(synonyms=synonyms)


def unseed(apps, schema_editor):
    CommandPalettePage = apps.get_model("auctions", "CommandPalettePage")
    for entry in _new_entries():
        CommandPalettePage.objects.filter(
            search_term=entry["search_term"],
            target=entry.get("target", ""),
            url=entry.get("url", ""),
        ).delete()
    for old, new in TARGET_RENAMES.items():
        CommandPalettePage.objects.filter(target=new).update(target=old)
    CommandPalettePage.objects.update(synonyms="")


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0308_commandpalettepage_synonyms_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
