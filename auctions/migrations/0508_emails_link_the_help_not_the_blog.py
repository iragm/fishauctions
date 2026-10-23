"""Seeded emails link the help, not the retired blog.

Each old post's link becomes the help section it moved to (``help_guides.MOVED_BLOG_POSTS``, copied here
so a later edit there can't change what this migration did). The invoices-ready email's PayPal import
bullet goes altogether: the payments help covers it. Only the bullet exactly as seeded is removed; one an
admin reworded keeps its words and gets the help link, rather than guessing where it starts and ends.
Nothing is restored on the way back.
"""

from django.db import migrations

#: ``/blog/<slug>/`` -> where it lives now.
MOVED = {
    "/blog/how-much-should-you-bid/": "/help/online-auctions/#how-much",
    "/blog/online-payments-suck/": "/help/payments/#paypal",
    "/blog/transporting-fish/": "/help/bagging-fish/",
    "/blog/optimal-lot-submission-time/": "/help/online-auctions/#when",
    "/blog/category-popularity/": "/help/online-auctions/#what-sells",
    "/blog/whats-a-picture-worth/": "/help/online-auctions/#photos",
    "/blog/multiple-location-auctions/": "/help/online-auctions/#locations",
    "/blog/encouraging-participation/": "/help/run-an-online-auction/#invite",
    "/blog/privacy/": "/privacy/",
}

#: The invoices-ready email's PayPal bullet, as 0475 seeded it, by field.
DROPPED = {
    "content": (
        "\n\n* We make it easy for you to import the automatically generated invoices into PayPal: "
        "https://{{ domain }}/blog/online-payments-suck/."
    ),
    "html_content": (
        '\n\n<li> We make it easy for you to <a href="https://{{ domain }}/blog/online-payments-suck/">import the '
        "automatically generated invoices into PayPal</a>.</li>"
    ),
}


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.all():
        changed = []
        for field in ("content", "html_content"):
            body = getattr(template, field) or ""
            new = body
            if template.name == "auction_invoices":
                new = new.replace(DROPPED[field], "")
            for old, moved_to in MOVED.items():
                new = new.replace(old, moved_to)
            if new != body:
                setattr(template, field, new)
                changed.append(field)
        if changed:
            template.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0507_help_links_point_at_the_help"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
