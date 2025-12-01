# Generated migration to set default value for next_update_due field

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0217_remove_email_invoice_periodic_task"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auction",
            name="next_update_due",
            field=models.DateTimeField(blank=True, null=True, default=timezone.now),
        ),
    ]
