# Generated by Django 3.1.1 on 2020-10-11 12:07

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0019_auto_20201010_2102'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdata',
            name='last_auction_used',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.auction'),
        ),
    ]
