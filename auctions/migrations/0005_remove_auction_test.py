# Generated by Django 3.1.1 on 2020-10-04 15:45

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0004_auction_test'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='auction',
            name='test',
        ),
    ]
