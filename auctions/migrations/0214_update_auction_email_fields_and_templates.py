# Generated manually to update auction email system
from django.db import migrations, models


def update_email_templates(apps, schema_editor):
    """Create/update email templates for the new auction email system."""
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")

    templates = [
        {
            "name": "auction_welcome",
            "subject": "{{ subject }}",
            "content": """Hello {{ auction.created_by.first_name }},

{{ subject }}

You can view your auction here: https://{{ domain }}/auctions/{{ auction.slug }}/

{% if enable_help %}You can find help here: https://{{ domain }}/auctions/{{ auction.slug }}/help/{% endif %}

* {% if not auction.promote_this_auction %}Your auction isn't visible to users unless they have a link to it.  {% endif %}Use this link to share your auction: https://{{ domain }}/?{{ auction.slug }}{% if auction.multi_location %}

* It looks like you've added more than one pickup location to your auction.  Here's some more information about how multi-locations auctions work:
https://{{ domain }}/blog/multiple-location-auctions/{% endif %}

Just reply to this email if you have questions!

Best wishes,
{{ domain }}

Unsubscribe: https://{{ domain }}/unsubscribe/{{ unsubscribe }}/""",
            "html_content": """Hello {{ auction.created_by.first_name }},<br><br>

{{ subject }}<br><br>

<a href="https://{{ domain }}/auctions/{{ auction.slug }}/">View your auction here</a><br><br>

{% if enable_help %}<a href="https://{{ domain }}/auctions/{{ auction.slug }}/help/">Get help with your auction</a><br><br>{% endif %}

<ul>
<li>{% if not auction.promote_this_auction %}Your auction isn't visible to users unless they have a link to it.  {% endif %}Use this link to share your auction: <a href="https://{{ domain }}/?{{ auction.slug }}">https://{{ domain }}/?{{ auction.slug }}</a></li>
{% if auction.multi_location %}<li>It looks like you've added more than one pickup location to your auction.  Here's some <a href="https://{{ domain }}/blog/multiple-location-auctions/">more information about how multi-locations auctions work</a>.</li>{% endif %}
</ul>

Just reply to this email if you have questions!<br><br>

Best wishes,<br>{{ domain }}<br><br>

<small><a href="https://{{ domain }}/unsubscribe/{{ unsubscribe }}/">Unsubscribe</a></small>""",
        },
        {
            "name": "auction_thanks",
            "subject": "Thanks for running an auction",
            "content": """Hello {{ auction.created_by.first_name }},

Thanks for choosing {{ domain }} to run your auction!

* Take a look at the stats for {{ auction }}: https://{{ domain }}/auctions/{{ auction.slug }}/stats/

* Remind your users to leave feedback on lots they've bought and sold: https://{{ domain }}/feedback/  It's probably a good idea to thank them for participating in {{ auction }}, and to encourage them to join your club.

* If you haven't already done so, you should mark your invoices paid: https://{{ domain }}/auctions/{{ auction.slug }}/users/

* Make sure to create your next auction by copying the rules used here: https://{{ domain }}/auctions/new/?copy={{ auction.slug }}  Try to always have one upcoming auction promoted for people to join (but not more than one or users can get confused about which one to join).

Once again, thank you very much for using {{ domain }}.  If you have suggestions to help improve the site, just reply to this email!

Unsubscribe: https://{{ domain }}/unsubscribe/{{ unsubscribe }}/""",
            "html_content": """Hello {{ auction.created_by.first_name }},<br><br>

Thanks for choosing {{ domain }} to run your auction!<br><br>

<ul>
<li>Take a look at the <a href="https://{{ domain }}/auctions/{{ auction.slug }}/stats/">stats for {{ auction }}</a>.</li>

<li>Remind your users to <a href="https://{{ domain }}/feedback/">leave feedback</a> on lots they've bought and sold.  It's probably a good idea to thank them for participating in the {{ auction }}, and to encourage them to join your club.</li>

<li>If you haven't already done so, you should <a href="https://{{ domain }}/auctions/{{ auction.slug }}/users/">mark your invoices paid</a>.</li>

<li>Make sure to create your next auction by <a href="https://{{ domain }}/auctions/new/?copy={{ auction.slug }}">copying the rules used here</a>.  Try to always have one upcoming auction promoted for people to join (but not more than one or users can get confused about which one to join).</li>
</ul><br>

Once again, thank you very much for using {{ domain }}.  If you have suggestions to help improve the site, just reply to this email!<br><br>

Best wishes,<br>{{ domain }}<br><br>

<small><a href="https://{{ domain }}/unsubscribe/{{ unsubscribe }}/">Unsubscribe</a></small>""",
        },
    ]

    for template in templates:
        EmailTemplate.objects.update_or_create(
            name=template["name"],
            defaults={
                "subject": template["subject"],
                "content": template["content"],
                "html_content": template["html_content"],
            },
        )


def reverse_email_templates(apps, schema_editor):
    """Revert email templates (optional - templates are not deleted)."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0213_mark_existing_in_person_print_reminders_sent"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        # Remove old email tracking fields
        migrations.RemoveField(
            model_name="auction",
            name="email_first_sent",
        ),
        migrations.RemoveField(
            model_name="auction",
            name="email_second_sent",
        ),
        migrations.RemoveField(
            model_name="auction",
            name="email_third_sent",
        ),
        migrations.RemoveField(
            model_name="auction",
            name="email_fourth_sent",
        ),
        migrations.RemoveField(
            model_name="auction",
            name="email_fifth_sent",
        ),
        # Add new email tracking fields with default=True so existing auctions don't get emails
        migrations.AddField(
            model_name="auction",
            name="welcome_email_sent",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="welcome_email_due",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="invoice_email_sent",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="invoice_email_due",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="followup_email_sent",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="auction",
            name="followup_email_due",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Update email templates
        migrations.RunPython(update_email_templates, reverse_email_templates),
    ]
