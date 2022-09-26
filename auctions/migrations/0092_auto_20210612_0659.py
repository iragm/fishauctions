# Generated by Django 3.2.4 on 2021-06-12 10:59

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auctions', '0091_auto_20210423_1416'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auction',
            name='date_end',
            field=models.DateTimeField(help_text='Bidding will end on this date'),
        ),
        migrations.AlterField(
            model_name='auction',
            name='date_start',
            field=models.DateTimeField(help_text='Bidding will be open on this date'),
        ),
        migrations.AlterField(
            model_name='userdata',
            name='can_submit_standalone_lots',
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name='AuctionIgnore',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('createdon', models.DateTimeField(auto_now_add=True)),
                ('auction', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auctions.auction')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'User ignoring auction',
                'verbose_name_plural': 'User ignoring auction',
            },
        ),
    ]