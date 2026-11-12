"""Feature emails (``auctions/feature_emails.py``) and the club outreach queue.

``email_me_about_new_features`` starts off for everyone who has already unsubscribed from everything.
The ``feature_email`` template is one greeting, the kind's one sentence and its button: the wording
lives on each ``Kind``, so the template never needs editing per feature.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

SUBJECT = "{{ subject }}"
TEXT = """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

{{ sentence }}

{{ button }}: {{ url }}

Stop emails about features: https://{{ domain }}/notifications/
{% email_footer_text %}
"""
HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
{{ sentence }}<br>
{% email_button %}<a href="{{ url }}">{{ button }}</a>{% endemail_button %}
<p style="font-size: small;"><a href="https://{{ domain }}/notifications/">Stop emails about features</a></p>
{% endblock %}
"""


def forwards(apps, schema_editor):
    apps.get_model("auctions", "UserData").objects.filter(has_unsubscribed=True).update(
        email_me_about_new_features=False
    )
    apps.get_model("post_office", "EmailTemplate").objects.update_or_create(
        name="feature_email",
        language="",
        defaults={"subject": SUBJECT, "content": TEXT, "html_content": HTML},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0527_auction_next_auction_emails"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.CreateModel(
            name="FeatureEmailPause",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("reason", models.CharField(max_length=300)),
            ],
        ),
        migrations.AddField(
            model_name="club",
            name="other_software",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not known"),
                    ("mygroupauctions", "Possibly MyGroupAuctions"),
                    ("own", "Its own software"),
                    ("other", "Something else"),
                ],
                default="",
                help_text="What the club may run its auctions on instead. Picks the outreach email that doesn't ask it to switch (admin_reports.outreach_queue); a guess is fine.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="userdata",
            name="email_me_about_new_features",
            field=models.BooleanField(
                blank=True,
                default=True,
                help_text="A few times a year at most (auctions.feature_emails)",
                verbose_name="Tell me about site features I'm not using yet",
            ),
        ),
        migrations.CreateModel(
            name="FeatureEmail",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=40)),
                ("sent_on", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("about", models.CharField(blank=True, default="", max_length=100)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feature_emails",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("user", "kind"), name="feature_email_once_per_kind")],
            },
        ),
        # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
