"""One greeting, "Hey <first name>,", and no sign-off: the footer already says who sent it.

Every seeded template, both parts. A full name (``tos.name``, the invoice's ``name``) is cut to its
first word with the ``first_name`` filter, and a missing name reads "Hey there,". The unsubscribe
links that some templates carried above the footer go too, since the footer carries the same link
whenever the send site passes a token. The two notes to organizers had no name at all and now greet
the auction's creator.

``auction_first`` keeps the unsubscribe link in its own sentence; only the standalone ones go.

A template edited in the admin keeps whatever part of this no longer matches it.
"""

from django.db import migrations

CREATOR = 'Hey {{ auction.created_by.first_name|default:"there" }},'
TOS_CREATOR = 'Hey {{ tos.auction.created_by.first_name|default:"there" }},'
TOS = 'Hey {{ tos.name|first_name|default:"there" }},'
#: The text part loads email_tags only at its end, for the footer, and a filter must be loaded before use.
TOS_TEXT = "{% load email_tags %}" + TOS
INVOICE = 'Hey {{ name|first_name|default:"there" }},'
NAME = 'Hey {{ name|default:"there" }},'

HTML_UNSUBSCRIBE = (
    '<br><br>\n<small><a href="https://{{ domain }}/unsubscribe/{{unsubscribe}}/">Unsubscribe</a></small>'
)
HTML_UNSUBSCRIBE_SPACED = (
    '<br><br>\n\n<small><a href="https://{{ domain }}/unsubscribe/{{ unsubscribe }}/">Unsubscribe</a></small>'
)

#: ``name: [(field, old, new)]``, where field is ``html_content`` or ``content``.
CHANGES = {
    "auction_first": [
        ("html_content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("html_content", "questions!<br><br>\nBest wishes,<br>{{domain}}", "questions!"),
        ("content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("content", "questions!\n\nBest wishes,\n{{domain}}", "questions!"),
    ],
    "auction_invoices": [
        ("html_content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("html_content", "</ul>\n<br>\nBest wishes,<br>{{domain}}" + HTML_UNSUBSCRIBE, "</ul>"),
        ("content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("content", "their lots.\n\nhttps://{{ domain }}/unsubscribe/{{unsubscribe}}/", "their lots."),
    ],
    "auction_print_reminder": [
        ("html_content", "Hello {{ tos.name }},", TOS),
        ("content", "Hello {{ tos.name }},", TOS_TEXT),
    ],
    "auction_promo_email": [
        ("html_content", "Hello {{ name }},", NAME),
        ("content", "Hello {{ name }},", NAME),
    ],
    "auction_second": [
        ("html_content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("html_content", "</ul>\n<br>\nBest wishes,<br>{{domain}}" + HTML_UNSUBSCRIBE, "</ul>"),
        ("content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("content", "{% endif %}\n\nUnsubscribe: https://{{ domain }}/unsubscribe/{{unsubscribe}}/", "{% endif %}"),
    ],
    "auction_thanks": [
        ("html_content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("html_content", "email!<br><br>\n\nBest wishes,<br>{{ domain }}" + HTML_UNSUBSCRIBE_SPACED, "email!"),
        ("content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("content", "email!\n\nUnsubscribe: https://{{ domain }}/unsubscribe/{{ unsubscribe }}/", "email!"),
    ],
    "auction_welcome": [
        ("html_content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        ("html_content", "questions!<br><br>\n\nBest wishes,<br>{{ domain }}" + HTML_UNSUBSCRIBE_SPACED, "questions!"),
        ("content", "Hello {{ auction.created_by.first_name }},", CREATOR),
        (
            "content",
            "questions!\n\nBest wishes,\n{{ domain }}\n\nUnsubscribe: https://{{ domain }}/unsubscribe/{{ unsubscribe }}/",
            "questions!",
        ),
        # The help link is a bullet, as it is in the HTML part.
        (
            "content",
            "{% if enable_help %}You can find help here: https://{{ domain }}/auctions/{{ auction.slug }}/help/"
            "{% endif %}\n\n* {% if not",
            "{% if enable_help %}* You can find help here: https://{{ domain }}/auctions/{{ auction.slug }}/help/\n\n"
            "{% endif %}* {% if not",
        ),
    ],
    "club_membership_expiring": [
        ("html_content", "Hello {{ name }},", NAME),
        ("content", "Hello {{ name }},", NAME),
    ],
    "in_person_auction_welcome": [
        ("html_content", "Hello {{ tos.name }},", TOS),
        ("content", "Hello {{ tos.name }},", TOS_TEXT),
    ],
    "invoice_ready": [
        ("html_content", "Hello {{ name }},", INVOICE),
        (
            "html_content",
            '{{ invoice.auction.invoice_payment_instructions | default:""}}<br>\nBest wishes,<br>\n{{domain}}',
            '{{ invoice.auction.invoice_payment_instructions | default:""}}',
        ),
        ("content", "Hello {{ name }},", "{% load email_tags %}" + INVOICE),
        (
            "content",
            '{{ invoice.auction.invoice_payment_instructions | default:""}}\n\nBest wishes,\n{{domain}}',
            '{{ invoice.auction.invoice_payment_instructions | default:""}}',
        ),
    ],
    "join_auction_reminder": [
        ("html_content", "Hey {{ user.first_name }},", 'Hey {{ user.first_name|default:"there" }},'),
        ("content", "Hey {{ user.first_name }},", 'Hey {{ user.first_name|default:"there" }},'),
    ],
    "lot_ended_relist": [
        ("html_content", "Hi {{lot.user.first_name}},", 'Hey {{ lot.user.first_name|default:"there" }},'),
        ("html_content", "{% endemail_button %}\n\nBest wishes,<br>\n{{domain}}", "{% endemail_button %}"),
        ("content", "Hi {{lot.user.first_name}},", 'Hey {{ lot.user.first_name|default:"there" }},'),
        ("content", "https://{{ domain }}//lots/new/", "https://{{ domain }}/lots/new/"),
        ("content", "{{ lot.lot_number }}\n\nBest wishes,\n{{domain}}", "{{ lot.lot_number }}"),
    ],
    "non_auction_lot_seller": [
        ("html_content", "Hi {{lot.user.first_name}},", 'Hey {{ lot.user.first_name|default:"there" }},'),
        ("html_content", "exchange.<br><br>\n\nBest wishes,<br>\n{{domain}}", "exchange."),
        ("content", "Hi {{lot.user.first_name}},", 'Hey {{ lot.user.first_name|default:"there" }},'),
        ("content", "exchange.\n\nBest wishes,\n{{domain}}", "exchange."),
    ],
    "non_auction_lot_winner": [
        ("html_content", "Hi {{lot.winner.first_name}},", 'Hey {{ lot.winner.first_name|default:"there" }},'),
        ("html_content", "exchange.<br><br>\n\nBest wishes,<br>\n{{domain}}", "exchange."),
        ("content", "Hi {{lot.winner.first_name}},", 'Hey {{ lot.winner.first_name|default:"there" }},'),
        ("content", "exchange.\n\nBest wishes,\n{{domain}}", "exchange."),
    ],
    "online_auction_welcome": [
        ("html_content", "Hello {{ tos.name }},", TOS),
        ("content", "Hello {{ tos.name }},", TOS_TEXT),
    ],
    "outbid_notification": [
        ("html_content", "Hello {{ name }},", NAME),
        ("content", "Hello {{ name }},", NAME),
    ],
    "reprint_reminder": [
        ("html_content", "Hello {{ tos.name }},", TOS),
        ("content", "Hello {{ tos.name }},", TOS_TEXT),
    ],
    "unread_chat_messages": [
        # The greeting had no break after it, and each section opened with its own; one break, after the greeting.
        ("html_content", "Hi {{name}},", NAME + "<br><br>"),
        ("html_content", "\n<br><br>People have been chatting", "\nPeople have been chatting"),
        ("html_content", "\n<br><br>Messages on lots", "\nMessages on lots"),
        (
            "html_content",
            '<a href="https://{{ domain }}/messages/">Manage messages</a> or '
            '<a href="https://{{ domain }}/unsubscribe/{{unsubscribe}}/">unsubscribe</a>',
            '<a href="https://{{ domain }}/messages/">Manage messages</a>',
        ),
        ("content", "Hi {{name}},", NAME),
        (
            "content",
            "/messages/\n\nUnsubscribe: https://{{ domain }}/unsubscribe/{{unsubscribe}}/",
            "/messages/",
        ),
    ],
    "user_joined_auction_despite_ban": [
        ("html_content", "{% block content %}\nHello,<br>", "{% block content %}\n" + TOS_CREATOR + "<br>"),
        ("content", "{{ tos.name }} joined", TOS_CREATOR + "\n\n{{ tos.name }} joined"),
    ],
    "watched_items_ending": [
        ("html_content", "{% block content %}\nMake sure", "{% block content %}\n" + NAME + "<br><br>\n\nMake sure"),
        ("content", "Make sure to bid", NAME + "\n\nMake sure to bid"),
    ],
    "weekly_promo_email": [
        ("html_content", "Hello {{ name }},", NAME),
        ("content", "Hello {{ name }},", NAME),
    ],
    "wrong_location_selected": [
        ("html_content", "{% block content %}\nHello,<br>", "{% block content %}\n" + TOS_CREATOR + "<br>"),
        ("content", "{{ tos.name }} joined", TOS_CREATOR + "\n\n{{ tos.name }} joined"),
    ],
}


def _apply(apps, forward):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name__in=CHANGES):
        changed = set()
        for field, old, new in CHANGES[template.name]:
            if not forward:
                old, new = new, old
            body = getattr(template, field) or ""
            # Exactly once, both ways: backwards puts a sign-off back after a short anchor like "</ul>".
            if body.count(old) == 1:
                setattr(template, field, body.replace(old, new, 1))
                changed.add(field)
        if changed:
            template.save(update_fields=sorted(changed))


def forwards(apps, schema_editor):
    _apply(apps, forward=True)


def backwards(apps, schema_editor):
    _apply(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0466_email_templates_on_a_layout"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
