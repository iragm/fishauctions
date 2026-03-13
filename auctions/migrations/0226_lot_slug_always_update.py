import autoslug.fields
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0225_update_email_templates_for_url_images"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lot",
            name="slug",
            field=autoslug.fields.AutoSlugField(
                always_update=True, editable=False, populate_from="lot_name", unique=False
            ),
        ),
    ]
