"""``UserData.merge_suggested_on`` and the ``account_merge_suggestion`` email, sent once to each account that
``auctions/duplicate_accounts.py`` thinks shares an owner with another one."""

from django.db import migrations, models

SUBJECT = "More than one account?"
TEXT = """{% load email_tags %}Hey {{ user.first_name|default:"there" }},

If you have more than one account, you can merge them: https://{{ domain }}/account/merge/
{% email_footer_text %}
"""
HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ user.first_name|default:"there" }},<br>
<br>
If you have more than one account, you can merge them.<br>
{% email_button %}<a href="https://{{ domain }}/account/merge/">Merge accounts</a>{% endemail_button %}
{% endblock %}
"""


def forwards(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.update_or_create(
        name="account_merge_suggestion",
        language="",
        defaults={"subject": SUBJECT, "content": TEXT, "html_content": HTML},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0527_auction_next_auction_emails"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="userdata",
            name="merge_suggested_on",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When this account was sent the one-time email saying accounts can be merged "
                    "(auctions.duplicate_accounts). Set, it's never sent again."
                ),
                null=True,
            ),
        ),
        # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
