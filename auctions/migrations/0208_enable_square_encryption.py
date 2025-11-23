# Generated migration for enabling encryption on SquareSeller OAuth tokens

from django.db import migrations
import encrypted_model_fields.fields


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0207_add_square_oauth_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='squareseller',
            name='access_token',
            field=encrypted_model_fields.fields.EncryptedCharField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name='squareseller',
            name='refresh_token',
            field=encrypted_model_fields.fields.EncryptedCharField(blank=True, max_length=500, null=True),
        ),
    ]
