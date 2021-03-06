# Generated by Django 3.1.4 on 2021-01-01 02:44

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auctions', '0053_auto_20201231_0710'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='seller',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='seller', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='lot',
            name='local_pickup',
            field=models.BooleanField(default=False, help_text="Check if you'll meet people in person to exchange this lot"),
        ),
        migrations.AlterField(
            model_name='lot',
            name='shipping_locations',
            field=models.ManyToManyField(blank=True, help_text="Check all locations you're willing to ship to", to='auctions.Location', verbose_name='I will ship to'),
        ),
        migrations.AlterField(
            model_name='lot',
            name='species_category',
            field=models.ForeignKey(blank=True, help_text="If you don't want to pick a species/product, pick the category this lot belongs to", null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.category', verbose_name='Category'),
        ),
    ]
