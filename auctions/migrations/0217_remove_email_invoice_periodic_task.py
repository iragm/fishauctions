# Generated migration to remove old email_invoice periodic task

from django.db import migrations


def remove_email_invoice_periodic_task(apps, schema_editor):
    """
    Remove the old email_invoice PeriodicTask from the database.

    This task was replaced by the new send_invoice_notification task
    which is scheduled as a one-off task when invoice status changes.
    """
    try:
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
        PeriodicTask.objects.filter(name="email_invoice").delete()
    except LookupError:
        # django_celery_beat not installed or model not available
        pass


def reverse_noop(apps, schema_editor):
    """
    No-op reverse migration.

    We don't recreate the old task since the new implementation
    uses a different approach.
    """
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0216_add_invoice_notification_due"),
    ]

    operations = [
        migrations.RunPython(remove_email_invoice_periodic_task, reverse_noop),
    ]
