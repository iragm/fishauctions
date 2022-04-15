# Generated by Django 3.1.7 on 2021-04-15 13:41

from django.db import migrations, models
import django.db.models.deletion
import easy_thumbnails.fields


class Migration(migrations.Migration):

    dependencies = [
        ('auctions', '0082_auto_20210326_0956'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auction',
            name='make_stats_public',
            field=models.BooleanField(default=True, help_text="Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"),
        ),
        migrations.CreateModel(
            name='LotImage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('caption', models.CharField(help_text='Optional', max_length=60)),
                ('image', easy_thumbnails.fields.ThumbnailerImageField(help_text='Select an image to upload', upload_to='images/')),
                ('image_source', models.CharField(blank=True, choices=[('ACTUAL', 'This picture is of the exact item'), ('REPRESENTATIVE', "This is my picture, but it's not of this exact item.  e.x. This is the parents of these fry"), ('RANDOM', 'This picture is from the internet')], max_length=20)),
                ('is_primary', models.BooleanField(blank=True, default=True)),
                ('createdon', models.DateTimeField(auto_now_add=True)),
                ('lot_number', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auctions.lot')),
            ],
        ),
    ]
