# Generated by Django 3.1.1 on 2020-11-24 13:57

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0039_auto_20201124_0856'),
    ]

    operations = [
        migrations.AlterField(
            model_name='blogpost',
            name='extra_js',
            field=models.TextField(blank=True, max_length=16000, null=True),
        ),
    ]
