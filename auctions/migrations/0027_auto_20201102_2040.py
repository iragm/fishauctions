# Generated by Django 3.1.1 on 2020-11-03 01:40

from django.db import migrations
import markdownfield.models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0026_auto_20201102_2030'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lot',
            name='description',
            field=markdownfield.models.MarkdownField(blank=True, null=True, rendered_field='description_rendered'),
        ),
    ]
