# Generated by Django 3.1.4 on 2021-01-23 16:14

from django.db import migrations, models
import location_field.models.plain


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0070_club_abbreviation'),
    ]

    operations = [
        migrations.CreateModel(
            name='GeneralInterest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
            ],
        ),
        migrations.AddField(
            model_name='club',
            name='active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='club',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='club',
            name='location',
            field=models.CharField(blank=True, help_text='Search Google maps with this address', max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='club',
            name='location_coordinates',
            field=location_field.models.plain.PlainLocationField(blank=True, max_length=63, null=True, verbose_name='Map'),
        ),
        migrations.AddField(
            model_name='club',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='club',
            name='interests',
            field=models.ManyToManyField(blank=True, to='auctions.GeneralInterest'),
        ),
    ]
