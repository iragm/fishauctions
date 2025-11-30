# Generated manually for Square receipt number field
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0211_update_email_templates_reply_to_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoicepayment",
            name="receipt_number",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Short receipt number (4 chars for Square)",
                max_length=10,
                null=True,
            ),
        ),
    ]
