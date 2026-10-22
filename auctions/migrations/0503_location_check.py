"""Ask people who joined from far away whether their map marker is right, and say how far in the
wrong-location email to the organizer.

Rows that already exist are marked as asked, so only joins from now on get the question. Like 0475, the
wrong-location template is overwritten whole rather than find-and-replaced.
"""

from django.db import migrations, models

LOCATION_CHECK = (
    "Is your location right?",
    "{% load email_tags %}Hey {{ tos.name|first_name|default:\"there\" }},\n\nYou joined {{ tos.auction }} from about {{ tos.distance_traveled_for_user }} away from {{ tos.pickup_location }}; if that's not where you are, fix your map marker here: https://{{ domain }}{% url 'contact_info' %}\n{% email_footer_text %}\n",
    '{% extends "email/base.html" %}{% load email_tags %}\n{% block content %}\nHey {{ tos.name|first_name|default:"there" }},<br>\n<br>\nYou joined {{ tos.auction }} from about {{ tos.distance_traveled_for_user }} away from {{ tos.pickup_location }}; if that\'s not where you are, fix your map marker.<br>\n{% email_button %}<a href="https://{{ domain }}{% url \'contact_info\' %}">Check my location</a>{% endemail_button %}\n{% endblock %}\n',
)

WRONG_LOCATION = (
    "A user may have selected the wrong location in {{ tos.auction }}",
    'Hey {{ tos.auction.created_by.first_name|default:"there" }},\n\n{{ tos.name }} joined {{ tos.auction}} from about {{ tos.distance_traveled_for_organizer }} away.  They may have selected the wrong pickup location -- {{ tos.closest_location_for_this_user}} is approximately {{tos.closer_location_savings}} miles closer to them, but they\'ve selected {{ tos.pickup_location }} instead.\n{% load email_tags %}{% email_footer_text %}\n',
    '{% extends "email/base.html" %}{% load email_tags %}\n{% block content %}\nHey {{ tos.auction.created_by.first_name|default:"there" }},<br>\n<br>\n{{ tos.name }} joined {{ tos.auction}} from about {{ tos.distance_traveled_for_organizer }} away.  They may have selected the wrong pickup location -- {{ tos.closest_location_for_this_user}} is approximately {{tos.closer_location_savings}} miles closer to them, but they\'ve selected {{ tos.pickup_location }} instead.<br>\n<br>\nYou can <a href="https://{{domain}}{{tos.auction.user_admin_link}}?query={{tos}}">change their pickup location for them here</a>, but don\'t make changes without talking with them first!<br>\n<br>\nTheir email is {{ tos.email }}\n{% endblock %}\n',
)


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for name, (subject, content, html_content) in (
        ("location_check", LOCATION_CHECK),
        ("wrong_location_selected", WRONG_LOCATION),
    ):
        values = {"subject": subject, "content": content, "html_content": html_content}
        # Not update_or_create: see 0475.
        if not EmailTemplate.objects.filter(name=name, language="").update(**values):
            EmailTemplate.objects.create(name=name, language="", **values)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0502_watch_is_deleted"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        # Added as True, so every existing row counts as asked without an UPDATE over the whole table,
        # then defaulted to False for new joins.
        migrations.AddField(
            model_name="auctiontos",
            name="location_check_email_sent",
            field=models.BooleanField(blank=True, default=True),
        ),
        migrations.AlterField(
            model_name="auctiontos",
            name="location_check_email_sent",
            field=models.BooleanField(blank=True, default=False),
        ),
        migrations.AddField(
            model_name="userdata",
            name="location_confirmed",
            field=models.BooleanField(blank=True, default=False, verbose_name="Yes, my location is accurate"),
        ),
        # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
