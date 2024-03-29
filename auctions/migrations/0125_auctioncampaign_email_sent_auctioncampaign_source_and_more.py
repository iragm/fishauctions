# Generated by Django 4.2.1 on 2023-11-07 20:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0124_auction_invoice_payment_instructions_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='auctioncampaign',
            name='email_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='auctioncampaign',
            name='source',
            field=models.CharField(blank=True, default='', max_length=200, null=True),
        ),
        migrations.AlterField(
            model_name='userdata',
            name='username_visible',
            field=models.BooleanField(blank=True, default=True, help_text="Uncheck to bid anonymously.  Your username will still be visible on lots you sell, chat messages, and to the people running any auctions you've joined."),
        ),
    ]
