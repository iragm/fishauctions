"""The ``thanks`` club email (``auctions/club_emails.py``), and the monthly-auction email without its
"add a club" link: adding a club makes the person who adds it that club's admin.
"""

from django.db import migrations

THANKS_TEXT = """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

Thanks for using auction.fish! Know of another club that might like to use it? We'd love it if you'd tell them about us.

Stop emails about features: https://{{ domain }}/notifications/
{% email_footer_text %}
"""
THANKS_HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
Thanks for using auction.fish! Know of another club that might like to use it? We'd love it if you'd tell them about us.<br>
<p style="font-size: small;"><a href="https://{{ domain }}/notifications/">Stop emails about features</a></p>
{% endblock %}
"""


def _without_link(text):
    """Every line naming ``add_club_url``, however it was edited since: the sender no longer passes it."""
    return "".join(line for line in text.splitlines(keepends=True) if "add_club_url" not in line)


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="club_email_thanks",
        language="",
        defaults={"subject": "{{ subject }}", "content": THANKS_TEXT, "html_content": THANKS_HTML},
    )
    for template in EmailTemplate.objects.filter(name="club_email_monthly_invite"):
        template.content = _without_link(template.content)
        template.html_content = _without_link(template.html_content)
        template.save(update_fields=["content", "html_content"])


def backwards(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.filter(name="club_email_thanks").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0532_club_emails"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
