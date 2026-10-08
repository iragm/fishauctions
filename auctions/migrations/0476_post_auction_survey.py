"""The post-auction survey (``auctions.auction_survey``): the rule, the answer on each participant, the
``auction_survey`` email, and the question added to the invoice email.

The invoice email's question is a tag that renders nothing unless the auction asks that way, so it is
added to every ``invoice_ready`` template. A passage edited in the admin so that it no longer matches is
left alone, as 0472 did.
"""

from django.db import migrations, models

SURVEY_SUBJECT = "How was {{ auction }}?"
SURVEY_TEXT = """{% load email_tags %}Hey {{ name|first_name|default:"there" }},

Thanks for being part of {{ auction }}.

{% email_survey_text "separate" %}{% email_footer_text %}
"""
SURVEY_HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block header %}{% email_club_header %}{% endblock %}
{% block content %}
Hey {{ name|first_name|default:"there" }},<br><br>

Thanks for being part of {{ auction }}.
{% email_survey "separate" %}
{% endblock %}
"""

#: ``[(field, old, new)]`` for the invoice_ready template.
INVOICE_CHANGES = [
    ("html_content", "\n{% endblock %}\n", '\n{% email_survey "invoice" %}\n{% endblock %}\n'),
    (
        "content",
        "{% load email_tags %}{% email_footer_text %}",
        '{% load email_tags %}{% email_survey_text "invoice" %}{% email_footer_text %}',
    ),
]


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="auction_survey",
        language="",
        defaults={"subject": SURVEY_SUBJECT, "content": SURVEY_TEXT, "html_content": SURVEY_HTML},
    )
    _change_invoice_template(EmailTemplate, forward=True)


def backwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    _change_invoice_template(EmailTemplate, forward=False)
    # Not deleted: post_office.Email cascades from its template, so that would take the sent log with it.


def _change_invoice_template(EmailTemplate, forward):
    for template in EmailTemplate.objects.filter(name="invoice_ready"):
        changed = set()
        for field, old, new in INVOICE_CHANGES:
            if not forward:
                old, new = new, old
            body = getattr(template, field) or ""
            if body.count(old) == 1:
                setattr(template, field, body.replace(old, new, 1))
                changed.add(field)
        if changed:
            template.save(update_fields=sorted(changed))


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0475_email_templates_match_the_migrations"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="post_auction_survey",
            field=models.CharField(
                choices=[
                    ("none", "No feedback"),
                    ("invoice", "Feedback included in invoice email"),
                    ("separate", "Feedback as separate email"),
                ],
                default="invoice",
                help_text="Ask people how the auction went",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="auction",
            name="survey_emails_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="auctiontos",
            name="survey_answer",
            field=models.CharField(
                blank=True, choices=[("great", "Great!"), ("not_fun", "Not so fun")], default="", max_length=10
            ),
        ),
        migrations.AddField(
            model_name="auctiontos",
            name="survey_answered_on",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="auctiontos",
            name="survey_comments",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(forwards, backwards),
    ]
