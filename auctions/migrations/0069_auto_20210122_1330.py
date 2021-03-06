# Generated by Django 3.1.4 on 2021-01-22 18:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0068_auto_20210122_1322'),
    ]

    operations = [
        migrations.AddField(
            model_name='club',
            name='facebook_page',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='club',
            name='notes',
            field=models.CharField(blank=True, help_text='Only visible in the admin site, never made public', max_length=300, null=True),
        ),
    ]
