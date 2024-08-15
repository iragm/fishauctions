# Generated by Django 5.0.8 on 2024-08-15 00:24

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0138_auction_only_approved_bidders'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auction',
            name='buy_now',
            field=models.CharField(choices=[('disable', "Don't allow"), ('allow', 'Allow'), ('required', 'Required for all lots'), ('forced', 'Required, and disable bidding')], default='allow', help_text='Allow lots to be sold without bidding, for a user-specified price', max_length=20),
        ),
        migrations.AlterField(
            model_name='auction',
            name='max_lots_per_user',
            field=models.PositiveIntegerField(blank=True, help_text="A user won't be able to add more than this many lots to this auction", null=True, validators=[django.core.validators.MaxValueValidator(100)]),
        ),
    ]
