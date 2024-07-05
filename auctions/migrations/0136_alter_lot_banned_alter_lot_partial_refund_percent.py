# Generated by Django 5.0.6 on 2024-06-29 22:53

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0135_lot_partial_refund_percent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lot',
            name='banned',
            field=models.BooleanField(blank=True, default=False, help_text="This lot will be hidden from views, and users won't be able to bid on it.  Removed lots are not charged in invoices.", verbose_name='Removed'),
        ),
        migrations.AlterField(
            model_name='lot',
            name='partial_refund_percent',
            field=models.IntegerField(blank=True, default=0, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)]),
        ),
    ]