# Generated by Django 4.2.1 on 2023-11-07 23:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0125_auctioncampaign_email_sent_auctioncampaign_source_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pageview',
            name='platform',
            field=models.CharField(blank=True, default='', max_length=200, null=True),
        ),
    ]
