# Generated by Django 5.1 on 2024-10-07 15:27

from django.db import migrations, models


def copy_notes(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    for auction in Auction.objects.all():
        auction.summernote_description = auction.notes_rendered[:10000] or ""
        auction.save()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0152_copy_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="summernote_description",
            field=models.CharField(blank=True, default="", max_length=10000, verbose_name="Rules"),
        ),
        migrations.RunPython(copy_notes),
    ]