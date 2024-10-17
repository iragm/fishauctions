# Generated by Django 5.1 on 2024-10-17 16:27

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0156_category_name_on_label_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userlabelprefs",
            name="print_border",
            field=models.BooleanField(
                default=True,
                help_text="Uncheck if you plant to use peel and stick labels.  Has no effect if you select thermal labels.",
            ),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="font_size",
            field=models.FloatField(
                default=8,
                validators=[django.core.validators.MinValueValidator(5), django.core.validators.MaxValueValidator(14)],
            ),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="label_margin_right",
            field=models.FloatField(
                default=0.2,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(5.0),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="page_margin_bottom",
            field=models.FloatField(default=0.45, validators=[django.core.validators.MinValueValidator(0.0)]),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="page_margin_left",
            field=models.FloatField(default=0.18, validators=[django.core.validators.MinValueValidator(0.0)]),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="page_margin_right",
            field=models.FloatField(default=0.18, validators=[django.core.validators.MinValueValidator(0.0)]),
        ),
        migrations.AlterField(
            model_name="userlabelprefs",
            name="page_margin_top",
            field=models.FloatField(default=0.55, validators=[django.core.validators.MinValueValidator(0.0)]),
        ),
    ]