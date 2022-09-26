# Generated by Django 3.1.7 on 2021-04-19 21:44

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0088_lot_lot_run_duration'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auction',
            name='make_stats_public',
            field=models.BooleanField(default=True, help_text="Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"),
        ),
        migrations.AlterField(
            model_name='lot',
            name='buy_now_price',
            field=models.PositiveIntegerField(blank=True, default=None, help_text='This lot will be sold instantly for this price if someone is willing to pay this much', null=True, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(1000)]),
        ),
        migrations.AlterField(
            model_name='lot',
            name='relist_if_not_sold',
            field=models.BooleanField(default=False, help_text='When this lot ends without being sold, reopen bidding on it.  Lots can be automatically relisted up to 5 times.'),
        ),
        migrations.AlterField(
            model_name='lot',
            name='relist_if_sold',
            field=models.BooleanField(default=False, help_text='When this lot sells, create a new copy of it.  Useful if you have many copies of something but only want to sell one at a time.'),
        ),
    ]