# Generated by Django 3.1.4 on 2021-01-05 22:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0057_auto_20210103_1618'),
    ]

    operations = [
        migrations.AddField(
            model_name='auction',
            name='max_lots_per_user',
            field=models.PositiveIntegerField(blank=True, help_text="User won't be able to add more than this number of lots to this auction", null=True),
        ),
    ]
