# Generated manually to prevent sending print reminder emails to past auctions
# This migration marks print_reminder_email_sent=True for all existing in-person auctions
# so that the new logic (which includes manually added users who added their own lots)
# only applies going forward.

from django.db import migrations


def mark_existing_in_person_print_reminders_sent(apps, schema_editor):
    AuctionTOS = apps.get_model("auctions", "AuctionTOS")
    # Mark all in-person auction TOS entries as having had the print reminder sent
    # This prevents sending emails to past auctions when we expand the criteria
    AuctionTOS.objects.filter(
        auction__is_online=False,
        print_reminder_email_sent=False,
    ).update(print_reminder_email_sent=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0211_update_email_templates_reply_to_note"),
    ]

    operations = [
        migrations.RunPython(
            mark_existing_in_person_print_reminders_sent,
            migrations.RunPython.noop,  # Migration is not reversible
        ),
    ]
