# Generated manually for Square refund protection

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0208_enable_square_encryption'),
    ]

    operations = [
        migrations.AddField(
            model_name='lot',
            name='no_more_refunds_possible',
            field=models.BooleanField(default=False, help_text='Set to True after a Square refund is issued to prevent multiple refunds that would unbalance the books'),
        ),
    ]
