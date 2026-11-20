"""The ``add_icon`` club email (``auctions/club_emails.py``): a club with no icon, once per club."""

from django.db import migrations

ICON_TEXT = """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

{{ club.name }} doesn't have an icon yet; add one here: {{ edit_url }}

Stop emails about features: https://{{ domain }}/notifications/
{% email_footer_text %}
"""
ICON_HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
{{ club.name }} doesn't have an icon yet.<br>
{% email_button %}<a href="{{ edit_url }}">Add an icon</a>{% endemail_button %}
<p style="font-size: small;"><a href="https://{{ domain }}/notifications/">Stop emails about features</a></p>
{% endblock %}
"""


def forwards(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.update_or_create(
        name="club_email_add_icon",
        language="",
        defaults={"subject": "{{ subject }}", "content": ICON_TEXT, "html_content": ICON_HTML},
    )


def backwards(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.filter(name="club_email_add_icon").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0535_blockedemailaddress"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
