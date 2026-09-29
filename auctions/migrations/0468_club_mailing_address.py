"""The club's donation mailing address becomes its mailing address, required for any email the club sends.

A rename, not a remove-and-add: the addresses clubs typed (or that Mailchimp/Brevo filled in) are kept.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0467_email_greetings_and_signoffs"),
    ]

    operations = [
        migrations.RenameField(
            model_name="club",
            old_name="donation_mailing_address",
            new_name="mailing_address",
        ),
        migrations.AlterField(
            model_name="club",
            name="mailing_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Required before the club can send email from this site: the law wants a postal address "
                    "on it. Also where vendors send physical donations."
                ),
                verbose_name="Mailing address",
            ),
        ),
    ]
