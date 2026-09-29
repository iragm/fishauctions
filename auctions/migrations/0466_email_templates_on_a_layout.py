"""Put every seeded HTML email on ``email/base.html``, with buttons for their main links.

Each body keeps its words; it moves into ``{% block content %}`` of a layout that carries the
typography, the footer (now with the site logo) and, for mail about a club's auction, the club's logo
above it. A template edited in the admin since it was seeded still gets the layout, and just keeps the
link its editor wrote where a button would have gone.

``auction_first``, ``auction_second``, ``club_membership_expiring`` and ``weekly_promo_email`` are not
sent, but get the layout anyway so that no seeded template is left looking different.
"""

import re

from django.db import migrations

FOOTER = "{% load email_tags %}{% email_footer %}"
LAYOUT = '{% extends "email/base.html" %}{% load email_tags %}\n'
CLUB_HEADER = "{% block header %}{% email_club_header %}{% endblock %}\n"

#: Sent to people taking part in a club's auction, so they carry that club's logo.
ABOUT_A_CLUB = {
    "auction_print_reminder",
    "auction_promo_email",
    "club_membership_expiring",
    "in_person_auction_welcome",
    "invoice_ready",
    "join_auction_reminder",
    "online_auction_welcome",
    "outbid_notification",
    "reprint_reminder",
}

#: To the organizer, or from the site itself.
FROM_THE_SITE = {
    "auction_first",
    "auction_invoices",
    "auction_second",
    "auction_thanks",
    "auction_welcome",
    "lot_ended_relist",
    "non_auction_lot_seller",
    "non_auction_lot_winner",
    "unread_chat_messages",
    "user_joined_auction_despite_ban",
    "watched_items_ending",
    "weekly_promo_email",
    "wrong_location_selected",
}


def _button(link):
    return "{% email_button %}" + link + "{% endemail_button %}"


#: ``name: [(old, new)]``, the one link in each email that is what the email is for.
BUTTONS = {
    "auction_invoices": [
        (
            'Please review the <a href="https://{{domain}}/auctions/{{auction.slug}}/users/">invoices for '
            "{{auction}} and mark them ready for payment</a><br><br>",
            "Please review the invoices for {{auction}} and mark them ready for payment.\n"
            + _button('<a href="https://{{domain}}/auctions/{{auction.slug}}/users/">Review invoices</a>'),
        ),
    ],
    "auction_print_reminder": [
        (
            'please <a href="https://{{domain}}{{tos.auction.label_print_link}}">print your labels from here</a>.'
            "<br>{% if not tos.auction.is_online%}If you want to add more lots, add them before printing your "
            "labels.{%endif%}",
            "please print your labels.{% if not tos.auction.is_online%}  If you want to add more lots, add them "
            "before printing your labels.{%endif%}\n"
            + _button('<a href="https://{{domain}}{{tos.auction.label_print_link}}">Print your labels</a>'),
        ),
    ],
    "auction_promo_email": [
        (
            '<a href="{{ auction_url }}&uid={{ unsubscribe }}">Read the rules and join</a><br><br>',
            _button('<a href="{{ auction_url }}&uid={{ unsubscribe }}">Read the rules and join</a>'),
        ),
    ],
    "auction_welcome": [
        (
            '<a href="https://{{ domain }}/auctions/{{ auction.slug }}/">View your auction here</a><br><br>',
            _button('<a href="https://{{ domain }}/auctions/{{ auction.slug }}/">View your auction</a>'),
        ),
        # The help link joins the list under the button rather than sitting between them on its own.
        (
            '{% if enable_help %}<a href="https://{{ domain }}/auctions/{{ auction.slug }}/help/">Get help with your '
            "auction</a><br><br>{% endif %}\n\n<ul>\n",
            '<ul>\n{% if enable_help %}<li><a href="https://{{ domain }}/auctions/{{ auction.slug }}/help/">Get help '
            "with your auction</a></li>{% endif %}\n",
        ),
    ],
    "club_membership_expiring": [
        (
            "<a href='{{ renew_link }}'>Click here to renew your membership</a><br>",
            _button("<a href='{{ renew_link }}'>Renew your membership</a>"),
        ),
    ],
    "invoice_ready": [
        (
            "<a href='https://{{domain}}/invoices/{{invoice.no_login_link}}/?src=notification'>Click here to view "
            "your invoice</a><br><br>\n<br>",
            _button(
                "<a href='https://{{domain}}/invoices/{{invoice.no_login_link}}/?src=notification'>View your invoice</a>"
            ),
        ),
    ],
    "join_auction_reminder": [
        (
            "Don't forget to join:  <a href='https://{{ domain }}/auctions/{{auction.slug}}?src={{uuid}}'>Read the "
            "rules and click the green button at the bottom of this page</a>\n<br><br>",
            "Don't forget to join: read the rules and click the green button at the bottom of the page.\n"
            + _button(
                "<a href='https://{{ domain }}/auctions/{{auction.slug}}?src={{uuid}}'>Read the rules and join</a>"
            ),
        ),
    ],
    "lot_ended_relist": [
        (
            "If you want to relist it, <a href='https://{{ domain }}//lots/new/?copy={{ lot.lot_number }}'>click "
            "here</a><br><br>",
            "If you want to relist it:\n"
            + _button("<a href='https://{{ domain }}/lots/new/?copy={{ lot.lot_number }}'>Relist this lot</a>"),
        ),
    ],
    "outbid_notification": [
        (
            "<a href='https://{{ lot.full_lot_link }}?src=outbid'>Click here to increase your bid</a><br><br>",
            _button("<a href='https://{{ lot.full_lot_link }}?src=outbid'>Increase your bid</a>"),
        ),
    ],
    "watched_items_ending": [
        (
            '<a href="https://{{domain}}/lots/watched/?src=email">Click here to view your watched lots</a>',
            _button('<a href="https://{{domain}}/lots/watched/?src=email">View your watched lots</a>'),
        ),
    ],
    "weekly_promo_email": [
        (
            'There\'s lots more, too!  <a href="https://{{ domain }}/?src=weekly_email&uid={{unsubscribe}}">Click '
            "here to see everything</a><br><br>",
            "There's lots more, too!\n"
            + _button('<a href="https://{{ domain }}/?src=weekly_email&uid={{unsubscribe}}">See everything</a>'),
        ),
    ],
}

#: The layout's footer has its own spacing, so line breaks left above the old footer only add a gap.
TRAILING_BREAKS = re.compile(r"(\s*<br\s*/?>)+\s*$")


def wrap(name, body):
    # Wherever it is: the layout adds its own, and one left inside the content would print twice.
    body = body.replace(FOOTER, "").rstrip()
    body = TRAILING_BREAKS.sub("", body).rstrip()
    for old, new in BUTTONS.get(name, []):
        body = body.replace(old, new)
    header = CLUB_HEADER if name in ABOUT_A_CLUB else ""
    return f"{LAYOUT}{header}{{% block content %}}\n{body}\n{{% endblock %}}\n"


def unwrap(name, body):
    body = body.replace(LAYOUT, "").replace(CLUB_HEADER, "")
    body = body.removeprefix("{% block content %}\n").rstrip()
    body = body.removesuffix("{% endblock %}").rstrip()
    for old, new in BUTTONS.get(name, []):
        body = body.replace(new, old)
    return f"{body}<br><br>\n{FOOTER}\n"


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name__in=ABOUT_A_CLUB | FROM_THE_SITE):
        if template.html_content.strip() and not template.html_content.startswith(LAYOUT):
            template.html_content = wrap(template.name, template.html_content)
            template.save(update_fields=["html_content"])


def backwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name__in=ABOUT_A_CLUB | FROM_THE_SITE):
        if template.html_content.startswith(LAYOUT):
            template.html_content = unwrap(template.name, template.html_content)
            template.save(update_fields=["html_content"])


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0465_auction_promos"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
