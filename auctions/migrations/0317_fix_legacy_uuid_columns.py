from django.db import migrations, models


def fix_legacy_uuid_columns(apps, schema_editor):
    """Convert legacy CHAR-stored UUIDField columns to MariaDB's native uuid type.

    Django < 5.0 stored UUIDField as char(32) (undashed hex).  Django >= 5.0 on
    MariaDB >= 10.7 runs UUIDField in native mode: new columns are created with the
    native uuid type and *values are sent in the dashed 36-char form*.  Against a
    leftover char(32) column that means every INSERT fails with DataError 1406
    ("Data too long") and every lookup silently matches nothing, because the dashed
    query value never equals the undashed stored value.

    MariaDB's uuid type accepts both the dashed and undashed text forms when
    converting, so a single MODIFY per column both changes the type and fixes the
    stored data.  Databases created entirely under Django 5 + MariaDB >= 10.7
    (including test databases) already have native uuid columns and this is a no-op.
    """
    connection = schema_editor.connection
    if connection.vendor != "mysql" or not connection.features.has_native_uuid_field:
        # On servers without the native uuid type, char(32) is still the correct
        # storage format for UUIDField and nothing is broken.
        return
    for model in apps.get_models():
        if model._meta.app_label != "auctions":
            continue
        for field in model._meta.local_fields:
            if not isinstance(field, models.UUIDField):
                continue
            table = model._meta.db_table
            column = field.column
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    [table, column],
                )
                row = cursor.fetchone()
            if not row:
                continue
            column_type, is_nullable = row
            if column_type.lower() not in ("char(32)", "varchar(32)", "char(36)", "varchar(36)"):
                continue
            null_sql = "NULL" if is_nullable == "YES" else "NOT NULL"
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE `{table}` MODIFY `{column}` uuid {null_sql}")


class Migration(migrations.Migration):
    # DDL on MySQL/MariaDB is non-transactional; don't pretend otherwise.
    atomic = False

    dependencies = [
        ("auctions", "0316_auction_alternate_split_mode_and_more"),
    ]

    operations = [
        migrations.RunPython(fix_legacy_uuid_columns, migrations.RunPython.noop, elidable=False),
    ]
