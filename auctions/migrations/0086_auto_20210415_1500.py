# Generated by Django 3.1.7 on 2021-04-15 19:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0085_auto_20210415_1114'),
    ]

    operations = [
        migrations.AddField(
            model_name='auction',
            name='clone_complete',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='lot',
            name='clone_complete',
            field=models.BooleanField(default=False),
        ),
    ]
