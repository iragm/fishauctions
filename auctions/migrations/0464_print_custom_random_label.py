"""Put the custom random field on every auction's labels, leaving the rest of each choice as it was.

It prints nothing until an auction switches the field on, so being on everywhere costs nothing.
"""

from django.db import migrations

TOKEN = "custom_random_label"


def add_token(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    auctions = []
    for auction in Auction.objects.exclude(label_print_fields__isnull=True).only("pk", "label_print_fields"):
        fields = [field for field in auction.label_print_fields.split(",") if field]
        if TOKEN not in fields:
            auction.label_print_fields = ",".join([*fields, TOKEN])
            auctions.append(auction)
    Auction.objects.bulk_update(auctions, ["label_print_fields"], batch_size=500)


def remove_token(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    auctions = []
    for auction in Auction.objects.filter(label_print_fields__contains=TOKEN).only("pk", "label_print_fields"):
        auction.label_print_fields = ",".join(
            field for field in auction.label_print_fields.split(",") if field != TOKEN
        )
        auctions.append(auction)
    Auction.objects.bulk_update(auctions, ["label_print_fields"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0463_custom_random"),
    ]

    operations = [
        migrations.RunPython(add_token, remove_token),
    ]
