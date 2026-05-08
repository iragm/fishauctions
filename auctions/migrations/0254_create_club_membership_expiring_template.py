# Generated manually for club membership renewal reminders
from django.db import migrations


def create_email_template(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="club_membership_expiring",
        defaults={
            "subject": "Your membership with {{ club.name }} is expiring soon",
            "content": (
                "Hello {{ name }},\n\n"
                "Your membership with {{ club.name }} is expiring soon.\n\n"
                "This club uses {{ navbar_brand }} to manage club memberships.\n"
                "You can renew it here: {{ renew_link }}\n"
            ),
            "html_content": (
                "Hello {{ name }},<br><br>"
                "Your membership with <b>{{ club.name }}</b> is expiring soon.<br><br>"
                "This club uses {{ navbar_brand }} to manage club memberships.<br>"
                "<a href='{{ renew_link }}'>Click here to renew your membership</a><br>"
            ),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0253_auction_add_membership_fee_to_invoices_for_expired_members_and_more"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.RunPython(create_email_template, migrations.RunPython.noop),
    ]
