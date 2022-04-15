# Generated by Django 3.1.4 on 2021-03-21 15:25

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auctions', '0080_remove_adcampaign_start_date'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='adcampaign',
            name='contact_user',
        ),
        migrations.RemoveField(
            model_name='adcampaign',
            name='paid',
        ),
        migrations.AddField(
            model_name='auction',
            name='make_stats_public',
            field=models.BooleanField(default=False, help_text="Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"),
        ),
        migrations.AddField(
            model_name='lot',
            name='buy_now_used',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userdata',
            name='has_bid',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userdata',
            name='has_used_proxy_bidding',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='adcampaign',
            name='bid',
            field=models.FloatField(default=1, help_text="At the moment, this is not actually the cost per click, it's the percent chance of showing this ad.  If the top ad fails, the next one will be selected.  If there are none left, google ads will be loaded.  Expects 0-1"),
        ),
        migrations.AlterField(
            model_name='adcampaign',
            name='title',
            field=models.CharField(default='Click here', max_length=50),
        ),
        migrations.CreateModel(
            name='AdCampaignGroup',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(default='Untitled campaign', max_length=100)),
                ('paid', models.BooleanField(default=False)),
                ('total_cost', models.FloatField(default=0)),
                ('contact_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(
            model_name='adcampaign',
            name='campaign_group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.adcampaigngroup'),
        ),
    ]
