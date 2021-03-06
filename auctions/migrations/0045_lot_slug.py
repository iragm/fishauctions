# Generated by Django 3.1.1 on 2020-12-11 14:04

import autoslug.fields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0044_auto_20201128_1407'),
    ]

    operations = [
        migrations.AddField(
            model_name='lot',
            name='slug',
            field=autoslug.fields.AutoSlugField(default='info', editable=False, populate_from='lot_name'),
            preserve_default=False,
        ),
    ]
