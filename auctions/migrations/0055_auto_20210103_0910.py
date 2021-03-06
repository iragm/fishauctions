# Generated by Django 3.1.4 on 2021-01-03 14:10

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auctions', '0054_auto_20201231_2144'),
    ]

    operations = [
        migrations.AddField(
            model_name='auction',
            name='is_chat_allowed',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='lot',
            name='is_chat_allowed',
            field=models.BooleanField(default=True, help_text='Uncheck to prevent chatting on this lot.  This will not remove any existing chat messages'),
        ),
        migrations.AddField(
            model_name='userdata',
            name='banned_from_chat_until',
            field=models.DateTimeField(blank=True, help_text='After this date, the user can post chats again.  Being banned from chatting does not block bidding', null=True),
        ),
        migrations.AlterField(
            model_name='lot',
            name='auction',
            field=models.ForeignKey(blank=True, help_text="Only auctions that you have <span class='text-warning'>selected a pickup location for</span> will be shown here. This lot must be brought to that location", null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.auction'),
        ),
        migrations.CreateModel(
            name='LotHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message', models.CharField(blank=True, max_length=200, null=True)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('seen', models.BooleanField(default=False)),
                ('changed_price', models.BooleanField(default=False, help_text='Was this a bid that changed the price?')),
                ('lot', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.lot')),
                ('user', models.ForeignKey(blank=True, help_text='The user who posted this message.', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
