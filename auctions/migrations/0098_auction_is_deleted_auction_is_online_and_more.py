# Generated by Django 4.0.3 on 2022-05-21 14:57

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0097_auctiontos_confirm_email_sent_auctiontos_is_admin_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='auction',
            name='is_deleted',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='auction',
            name='is_online',
            field=models.BooleanField(default=True, help_text='Is this is an online auction with in-person pickup at one or more locations?'),
        ),
        migrations.AddField(
            model_name='auction',
            name='use_categories',
            field=models.BooleanField(default=True, help_text='Check to use categories like Cichlids, Livebearers, etc.', verbose_name='This is a fish auction'),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='address',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='bidder_number',
            field=models.CharField(blank=True, default='', max_length=4),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='bidding_allowed',
            field=models.BooleanField(blank=True, default=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='email',
            field=models.EmailField(blank=True, max_length=254, null=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='name',
            field=models.CharField(blank=True, max_length=181, null=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='phone_number',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='print_reminder_email_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='second_confirm_email_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='selling_allowed',
            field=models.BooleanField(blank=True, default=True),
        ),
        migrations.AddField(
            model_name='auctiontos',
            name='time_spent_reading_rules',
            field=models.PositiveIntegerField(blank=True, default=0, validators=[django.core.validators.MinValueValidator(0)]),
        ),
        migrations.AddField(
            model_name='faq',
            name='include_in_auctiontos_confirm_email',
            field=models.BooleanField(blank=True, default=False),
        ),
        migrations.AddField(
            model_name='invoice',
            name='auctiontos_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='auctiontos', to='auctions.auctiontos'),
        ),
        migrations.AddField(
            model_name='lot',
            name='auctiontos_seller',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='auctiontos_seller', to='auctions.auctiontos'),
        ),
        migrations.AddField(
            model_name='lot',
            name='auctiontos_winner',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='auctiontos_winner', to='auctions.auctiontos'),
        ),
        migrations.AddField(
            model_name='lot',
            name='is_deleted',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='pickuplocation',
            name='allow_bidding_by_default',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='pickuplocation',
            name='allow_selling_by_default',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='pickuplocation',
            name='is_default',
            field=models.BooleanField(default=False, help_text='This was a default location added for an in-person auction.'),
        ),
        migrations.AddField(
            model_name='pickuplocation',
            name='pickup_by_mail',
            field=models.BooleanField(default=False, help_text='Special pickup location without an actual location'),
        ),
        migrations.AddField(
            model_name='userban',
            name='createdon',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='userdata',
            name='preferred_bidder_number',
            field=models.CharField(blank=True, default='', max_length=4),
        ),
        migrations.AddField(
            model_name='userdata',
            name='timezone',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='userignorecategory',
            name='createdon',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='watch',
            name='createdon',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='auction',
            name='date_end',
            field=models.DateTimeField(blank=True, help_text='Bidding will end on this date.  If last-minute bids are placed, bidding can go up to 1 hour past this time on those lots.', null=True),
        ),
        migrations.AlterField(
            model_name='userdata',
            name='paypal_email_address',
            field=models.CharField(blank=True, help_text='If different from your email address', max_length=200, null=True, verbose_name='Paypal Address'),
        ),
    ]
