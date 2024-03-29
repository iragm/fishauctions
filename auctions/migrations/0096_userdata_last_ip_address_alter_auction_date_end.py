# Generated by Django 4.0.3 on 2022-04-17 18:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0095_auto_20210622_1943'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdata',
            name='last_ip_address',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AlterField(
            model_name='auction',
            name='date_end',
            field=models.DateTimeField(help_text='Bidding will end on this date.  If last-minute bids are placed, bidding can go up to 1 hour past this time on those lots.'),
        ),
    ]
