"""The two club emails (``auctions/club_emails.py``): ``Club.auction_cadence``, ``ClubEmail`` and their
templates. Each template's wording lives here; the links and the feature list come from the sender.
"""

import django.db.models.deletion
from django.db import migrations, models

SUBJECT = "{{ subject }}"
STOP_TEXT = """
Stop emails about features: https://{{ domain }}/notifications/
{% email_footer_text %}
"""
STOP_HTML = """<p style="font-size: small;"><a href="https://{{ domain }}/notifications/">Stop emails about features</a></p>
{% endblock %}
"""
MONTHLY_TEXT = (
    """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

Your club's monthly meeting auctions can run here too, the same way {{ auction.title }} did.

{% for button in buttons %}{{ button.label }}: {{ button.url }}
{% endfor %}"""
    + STOP_TEXT
)
MONTHLY_HTML = (
    """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
Your club's monthly meeting auctions can run here too, the same way {{ auction.title }} did.<br>
{% for button in buttons %}{% email_button %}<a href="{{ button.url }}">{{ button.label }}</a>{% endemail_button %}{% endfor %}
"""
    + STOP_HTML
)
TOOLS_TEXT = (
    """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

{{ club.name }} runs its auctions here, and the club side of the site can take more off your hands:

{% for feature in features %}{{ feature.name }}: {{ feature.does }}
  How it works: {{ feature.help_url }}
  Set it up: {{ feature.setup_url }}

{% endfor %}"""
    + STOP_TEXT
)
TOOLS_HTML = (
    """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
{{ club.name }} runs its auctions here, and the club side of the site can take more off your hands:<br>
{% for feature in features %}<p><strong>{{ feature.name }}</strong><br>{{ feature.does }}<br><a href="{{ feature.help_url }}">How it works</a></p>
{% email_button %}<a href="{{ feature.setup_url }}">Set it up</a>{% endemail_button %}{% endfor %}
"""
    + STOP_HTML
)
TEMPLATES = {
    "club_email_monthly_invite": (MONTHLY_TEXT, MONTHLY_HTML),
    "club_email_club_tools": (TOOLS_TEXT, TOOLS_HTML),
}


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for name, (text, html) in TEMPLATES.items():
        EmailTemplate.objects.update_or_create(
            name=name, language="", defaults={"subject": SUBJECT, "content": text, "html_content": html}
        )


def backwards(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.filter(name__in=TEMPLATES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0530_speaker_visibility_signed_in"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="auction_cadence",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not asked"),
                    ("monthly", "Runs its monthly auctions here"),
                    ("no_monthly", "Has no monthly auctions"),
                    ("not_interested", "Doesn't want software for its monthly auctions"),
                ],
                default="",
                help_text="The club's own answer to the monthly-auction email (auctions/club_emails.py); any answer stops that email.",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="ClubEmail",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=40)),
                ("sent_on", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="club_emails", to="auctions.club"
                    ),
                ),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("club", "kind"), name="club_email_once_per_kind")],
            },
        ),
        migrations.RunPython(forwards, backwards),
    ]
