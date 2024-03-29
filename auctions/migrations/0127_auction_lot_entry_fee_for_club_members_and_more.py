# Generated by Django 4.2.1 on 2023-11-08 00:26

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0126_alter_pageview_platform'),
    ]

    operations = [
        migrations.AddField(
            model_name='auction',
            name='lot_entry_fee_for_club_members',
            field=models.PositiveIntegerField(default=0, help_text='Used instead of the standard entry fee, when you designate someone as a club member', validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10)]),
        ),
        migrations.AddField(
            model_name='auction',
            name='winning_bid_percent_to_club_for_club_members',
            field=models.PositiveIntegerField(default=0, help_text='Used instead of the standard split, when you designate someone as a club member', validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)]),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='is_club_member',
            field=models.BooleanField(blank=True, default=False, help_text='Check to use the alternative split for this auction'),
        ),
    ]
