# Generated by Django 4.0.3 on 2023-01-28 22:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0104_invoice_calculated_total'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='status',
            field=models.CharField(choices=[('DRAFT', 'Open'), ('UNPAID', 'Waiting for payment'), ('PAID', 'Paid')], default='DRAFT', max_length=20),
        ),
    ]
