"""The ``online_pickup_tomorrow`` email: sent by ``auctiontos_notifications`` the day before an online
auction's first pickup time, saying which of the pickup times this person needs (``AuctionTOS.pickup_plan``).
Also links the logistics page from ``auction_invoices``, the organizer's "mark invoices ready" email; a
template edited in the admin so the anchor sentence is gone is left alone.
"""

from django.db import migrations

SUBJECT = "Pickup tomorrow for {{ tos.auction }}"
TEXT = """{% load email_tags %}Hey {{ tos.name|first_name|default:"there" }},

{% if plan == "second" %}Your invoice is paid and your lots are coming to the second pickup, so you don't need to come on {{ tos.pickup_time_as_localized_string }}.  See you at {{ tos.pickup_location }} on {{ tos.second_pickup_time_as_localized_string }}.
{% elif plan == "first" %}See you at {{ tos.pickup_location }} on {{ tos.pickup_time_as_localized_string }}.
{% else %}Pickup for {{ tos.auction }} is at {{ tos.pickup_location }} on {{ tos.pickup_time_as_localized_string }}.
* This is a multi-location auction, and the club will coordinate transport between locations.  Lots from other locations{% if tos.holding_for_second_pickup %}, and the lots you're holding,{% endif %} will be ready when you return on {{ tos.second_pickup_time_as_localized_string }}.
{% endif %}{% if tos.pickup_location.directions_link %}
Directions: {{ tos.pickup_location.directions_link }}
{% endif %}
Questions?  Just reply and we'll help!
{% email_footer_text %}
"""
HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block header %}{% email_club_header %}{% endblock %}
{% block content %}
Hey {{ tos.name|first_name|default:"there" }},<br>
<br>
{% if plan == "second" %}Your invoice is paid and your lots are coming to the second pickup, so you don't need to come on {{ tos.pickup_time_as_localized_string }}.  See you at {{ tos.pickup_location }} on <b>{{ tos.second_pickup_time_as_localized_string }}</b>.
{% elif plan == "first" %}See you at {{ tos.pickup_location }} on <b>{{ tos.pickup_time_as_localized_string }}</b>.
{% else %}Pickup for {{ tos.auction }} is at {{ tos.pickup_location }} on <b>{{ tos.pickup_time_as_localized_string }}</b>.<ul>
<li>This is a multi-location auction, and the club will coordinate transport between locations.  Lots from other locations{% if tos.holding_for_second_pickup %}, and the lots you're holding,{% endif %} will be ready when you return on <b>{{ tos.second_pickup_time_as_localized_string }}</b>.</li></ul>
{% endif %}
{% if tos.pickup_location.directions_link %}{% email_button %}<a href="{{ tos.pickup_location.directions_link }}">Get directions</a>{% endemail_button %}{% endif %}
Questions?  Just reply and we'll help!
{% endblock %}
"""


#: ``(anchor, added before it)`` in auction_invoices' text and HTML parts.
INVOICES_TEXT = (
    "* Remember to print out a copy of each invoice",
    "* On pickup day, cross people off on the logistics page: https://{{ domain }}/auctions/{{ auction.slug }}/logistics/\n\n",
)
INVOICES_HTML = (
    "<li> Remember to print out a copy of each invoice",
    '<li> On pickup day, cross people off on the <a href="https://{{ domain }}/auctions/{{ auction.slug }}/logistics/">logistics page</a>.</li>\n\n',
)


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name="auction_invoices"):
        for field, (anchor, added) in (("content", INVOICES_TEXT), ("html_content", INVOICES_HTML)):
            text = getattr(template, field) or ""
            if anchor in text and "/logistics/" not in text:
                setattr(template, field, text.replace(anchor, added + anchor, 1))
        template.save()
    EmailTemplate.objects.update_or_create(
        name="online_pickup_tomorrow",
        language="",
        defaults={"subject": SUBJECT, "content": TEXT, "html_content": HTML},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0510_auctiontos_pickup_logistics"),
        ("post_office", "0011_models_help_text"),
    ]

    # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
