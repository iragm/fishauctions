# Generated by Django 4.2.1 on 2023-07-20 22:35

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0120_pageview_duplicate_check_completed_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pageview',
            name='duplicate_check_completed',
            field=models.BooleanField(default=False),
        ),
    ]
