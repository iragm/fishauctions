# Generated migration to clean up leftover invoice notification tasks

from django.db import migrations


def cleanup_invoice_notification_tasks(apps, schema_editor):
    """
    Remove any leftover invoice_notification_* PeriodicTask entries from the database.

    These tasks should be automatically cleaned up after execution, but any
    tasks that were created before the cleanup logic was added may still exist.
    """
    try:
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
        # Delete all invoice notification tasks
        PeriodicTask.objects.filter(name__startswith="invoice_notification_").delete()
    except LookupError:
        # django_celery_beat not installed or model not available
        pass


def reverse_noop(apps, schema_editor):
    """
    No-op reverse migration.
    """
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0217_remove_email_invoice_periodic_task"),
    ]

    operations = [
        migrations.RunPython(cleanup_invoice_notification_tasks, reverse_noop),
    ]
