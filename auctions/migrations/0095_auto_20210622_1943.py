# Generated by Django 3.2.4 on 2021-06-22 23:43

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0094_auto_20210615_2109'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lot',
            name='auction',
            field=models.ForeignKey(blank=True, help_text="<span class='text-warning' id='last-auction-special'></span>Only auctions that you have <span class='text-warning'>selected a pickup location for</span> will be shown here. This lot must be brought to that location", null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.auction'),
        ),
        migrations.AlterField(
            model_name='lot',
            name='reserve_price',
            field=models.PositiveIntegerField(default=2, help_text='The minimum bid for this lot. Lot will not be sold unless someone bids at least this much', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(2000)]),
        ),
    ]
