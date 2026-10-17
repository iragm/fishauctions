"""The ``android_testers_invite`` email, sent once by ``manage.py android_testers_invite``."""

from django.db import migrations

SUBJECT = "Help test the auction.fish Android app"
TEXT = """{% load email_tags %}Hey {{ name|first_name|default:"there" }},

The Android app needs a few testers before Google will let it into the Play Store.

1. Open the testers group and click "Join group": {{ group_url }}
2. Then opt in and install the app on Google Play: {{ testing_url }}

More information about the mobile app: https://{{ domain }}{{ help_path }}

You're getting this email because you viewed a page from an Android phone recently. An iPhone version of this app is scheduled for release about 3 months ago (totally normal software development timeline). But soon, hopefully.

{% email_footer_text %}
"""
HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ name|first_name|default:"there" }},<br><br>

The Android app needs a few testers before Google will let it into the Play Store.<br><br>

1. Open the testers group and click <b>Join group</b>.
{% email_button %}<a href="{{ group_url }}">Open the testers group</a>{% endemail_button %}
2. Then opt in and install the app on Google Play.
{% email_button %}<a href="{{ testing_url }}">Get it on Google Play</a>{% endemail_button %}
<a href="https://{{ domain }}{{ help_path }}">More information about the mobile app</a><br><br>

You're getting this email because you viewed a page from an Android phone recently. An iPhone version of this app is scheduled for release about 3 months ago (totally normal software development timeline). But soon, hopefully.
{% endblock %}
"""


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="android_testers_invite",
        language="",
        defaults={"subject": SUBJECT, "content": TEXT, "html_content": HTML},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0499_auctiontos_leave_fields"),
        ("post_office", "0011_models_help_text"),
    ]

    # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
