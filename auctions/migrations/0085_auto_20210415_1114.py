# Generated by Django 3.1.7 on 2021-04-15 15:14

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0084_auto_20210415_1000'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lotimage',
            name='caption',
            field=models.CharField(blank=True, help_text='Optional', max_length=60, null=True),
        ),
    ]
