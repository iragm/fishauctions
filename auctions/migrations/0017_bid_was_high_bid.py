# Generated by Django 3.1.1 on 2020-10-09 18:23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0016_auto_20201009_1255'),
    ]

    operations = [
        migrations.AddField(
            model_name='bid',
            name='was_high_bid',
            field=models.BooleanField(default=False),
        ),
    ]
