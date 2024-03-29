# Generated by Django 4.2.1 on 2023-07-19 15:05

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auctions', '0118_lot_reference_link_alter_pageview_source'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lot',
            name='reference_link',
            field=models.URLField(blank=True, help_text='A URL with additional information about this lot', null=True),
        ),
        migrations.CreateModel(
            name='SearchHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('search', models.CharField(max_length=600)),
                ('createdon', models.DateTimeField(auto_now_add=True)),
                ('auction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='auctions.auction')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
