from django.db import migrations, models
from django.utils import timezone


def forwards(apps, schema_editor):
    Lot = apps.get_model("auctions", "Lot")
    LotQueueEntry = apps.get_model("auctions", "LotQueueEntry")
    # The cooldown starts now, so a lot already announced isn't announced again the moment this lands.
    Lot.objects.filter(selling_push_notification_sent=True).update(selling_push_sent_at=timezone.now())
    LotQueueEntry.objects.filter(lot__selling_push_notification_sent=True).update(announced=True)


def backwards(apps, schema_editor):
    Lot = apps.get_model("auctions", "Lot")
    Lot.objects.filter(selling_push_sent_at__isnull=False).update(selling_push_notification_sent=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0469_lot_label_first_printed_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="lot",
            name="selling_push_sent_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "When this lot's watchers last got the 'about to be sold' push (it came up in the in-person "
                    "queue or was pulled up on the set-winners screen). Another is sent only if the lot comes up "
                    "again after SELLING_PUSH_COOLDOWN, which is what a mistyped lot number looks like; shares a "
                    "notification tag with the 'coming up soon' push so it overwrites it."
                ),
            ),
        ),
        migrations.AddField(
            model_name="lotqueueentry",
            name="passed_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When the room moved past this lot. Unset while it's still to come or on the block.",
            ),
        ),
        migrations.AddField(
            model_name="lotqueueentry",
            name="announced",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "The 'about to be sold' pass has run for this lot's turn on the block. Cleared whenever another "
                    "lot is on the block, so coming back to it announces it again (subject to the lot's cooldown)."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="lot",
            name="coming_up_push_sent",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Set once this lot's watchers got the 'coming up soon -- N lots away' push while it sat in the "
                    "top 10 of the in-person queue. Deduped so that push fires at most once per lot; the later "
                    "'about to be sold' push (selling_push_sent_at) overwrites it on the device."
                ),
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="lot", name="selling_push_notification_sent"),
    ]
