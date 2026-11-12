"""Two emails to an auction's creator, sent by ``auctions/next_auction.py``: ``next_auction`` ("ready for
your next one?", or after a test auction, "ready to run it for real?") and ``auction_anniversary`` (a copy
prompt ten weeks before a yearly auction comes round again). The invoice email also gains one line linking
the same club's next promoted auction (``{% email_next_auction %}``).

``next_auction_email_sent`` is added as True and then defaulted to False, so every auction that already
exists counts as asked: only auctions made from now on get the email.
"""

from django.db import migrations, models

NEXT_SUBJECT = "{% if is_test %}Ready to run a real auction?{% else %}Ready for your next auction?{% endif %}"
NEXT_TEXT = """{% load email_tags %}Hey {{ auction.created_by.first_name|default:"there" }},

Create a new auction, or if things aren't working as you want, let us know: features are added quickly and feedback improves the site for everyone.

Create a new auction: https://{{ domain }}/auctions/new/?copy={{ auction.slug }}
Let us know: https://{{ domain }}/requests/
{% email_footer_text %}
"""
NEXT_HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ auction.created_by.first_name|default:"there" }},<br>
<br>
Create a new auction, or if things aren't working as you want, <a href="https://{{ domain }}/requests/">let us know</a>: features are added quickly and feedback improves the site for everyone.<br>
{% email_button %}<a href="https://{{ domain }}/auctions/new/?copy={{ auction.slug }}">Create a new auction</a>{% endemail_button %}
{% endblock %}
"""

ANNIVERSARY_SUBJECT = "{{ auction }} was almost a year ago"
ANNIVERSARY_TEXT = """{% load email_tags %}Hey {{ auction.created_by.first_name|default:"there" }},

Copy {{ auction }} to set up this year's: https://{{ domain }}/auctions/new/?copy={{ auction.slug }}
{% email_footer_text %}
"""
ANNIVERSARY_HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
Hey {{ auction.created_by.first_name|default:"there" }},<br>
<br>
Copy {{ auction }} to set up this year's.<br>
{% email_button %}<a href="https://{{ domain }}/auctions/new/?copy={{ auction.slug }}">Copy {{ auction }}</a>{% endemail_button %}
{% endblock %}
"""

#: ``[(field, old, new)]`` for invoice_ready: the club's next promoted auction, after the survey question. A
#: template edited in the admin so it no longer matches is left alone, as 0476 did.
INVOICE_CHANGES = [
    (
        "html_content",
        '\n{% email_survey "invoice" %}\n{% endblock %}\n',
        '\n{% email_survey "invoice" %}\n{% email_next_auction %}\n{% endblock %}\n',
    ),
    (
        "content",
        '{% email_survey_text "invoice" %}{% email_footer_text %}',
        '{% email_survey_text "invoice" %}{% email_next_auction_text %}{% email_footer_text %}',
    ),
]

TEMPLATES = {
    "next_auction": (NEXT_SUBJECT, NEXT_TEXT, NEXT_HTML),
    "auction_anniversary": (ANNIVERSARY_SUBJECT, ANNIVERSARY_TEXT, ANNIVERSARY_HTML),
}


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for name, (subject, text, html) in TEMPLATES.items():
        EmailTemplate.objects.update_or_create(
            name=name,
            language="",
            defaults={"subject": subject, "content": text, "html_content": html},
        )
    for template in EmailTemplate.objects.filter(name="invoice_ready"):
        changed = []
        for field, old, new in INVOICE_CHANGES:
            body = getattr(template, field) or ""
            if body.count(old) == 1:
                setattr(template, field, body.replace(old, new, 1))
                changed.append(field)
        if changed:
            template.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0526_mcp_result_diagnostics"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="next_auction_email_sent",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="auction",
            name="next_auction_email_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="auction",
            name="anniversary_email_sent",
            field=models.BooleanField(default=False),
        ),
        # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
