# Generated by Django 3.1.1 on 2020-10-05 00:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0009_auto_20201005_0027'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdata',
            name='number_total_lots',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userdata',
            name='number_total_spent',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userdata',
            name='number_unique_species',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]