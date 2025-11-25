# Generated manually to change email field defaults for new auctions
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    This migration changes the defaults for email sent fields to False.
    The previous migration (0212) added these fields with default=True so that
    existing auctions don't receive these new emails. This migration changes
    the defaults so that newly created auctions will receive emails.
    """

    dependencies = [
        ("auctions", "0212_update_auction_email_fields_and_templates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auction",
            name="welcome_email_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="auction",
            name="invoice_email_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="auction",
            name="followup_email_sent",
            field=models.BooleanField(default=False),
        ),
    ]
