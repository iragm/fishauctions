from collections import defaultdict

from django.db import migrations


def _columns_the_models_describe(apps):
    """Every column name the models claim, keyed by table.

    A union, not a per-model set: proxies, multi-table children and many-to-many through tables
    all mean more than one model can speak for one table, and taking any single model's word for
    it would call the rest of the table's columns unknown.
    """
    known = defaultdict(set)
    for model in apps.get_models(include_auto_created=True):
        known[model._meta.db_table] |= {field.column for field in model._meta.local_fields}
    return known


def drop_orphan_columns(apps, schema_editor):
    """Drop columns no model describes, where their presence stops the table taking rows at all.

    A ``NOT NULL`` column with no database default is fatal to a table whose models have never
    heard of it: Django writes an INSERT naming only its own fields, MariaDB in strict mode
    refuses it with error 1364 (``Field 'x' doesn't have a default value``), and there is no way
    to create a row of that model -- no application code can route around a schema that rejects
    every write.  Live databases have collected several of these from feature branches that were
    migrated against them and then abandoned; the columns survive in no model, no migration and
    no branch.  ``auctions_clubapikey.can_add_species`` broke creating an API key,
    ``auctions_club.enable_event_rsvp`` broke creating a club, and
    ``auctions_clubevent.rsvp_enabled`` broke creating an event.

    Being fatal is what makes this safe to do by rule rather than by a list of names.  A column
    that blocks every insert cannot be one anything is using, and cannot be holding data anything
    has written since it appeared.  Columns that are merely unknown -- nullable, or carrying a
    default -- are left alone: those are inert, and dropping them is where real data loss would
    live.  On a database built from these migrations there is nothing to find and this is a no-op.
    """
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    known = _columns_the_models_describe(apps)
    tables = {
        model._meta.db_table
        for model in apps.get_models(include_auto_created=True)
        if model._meta.app_label == "auctions"
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
            "AND EXTRA NOT LIKE '%%auto_increment%%' AND EXTRA NOT LIKE '%%GENERATED%%'"
        )
        candidates = sorted(cursor.fetchall())
    for table, column in candidates:
        if table not in tables or column in known[table]:
            continue
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")
        print(f"  dropped orphan column {table}.{column}")  # noqa: T201


class Migration(migrations.Migration):
    # DDL on MySQL/MariaDB is non-transactional; don't pretend otherwise.
    atomic = False

    dependencies = [
        ("auctions", "0417_remove_auction_code_to_add_lots"),
    ]

    operations = [
        migrations.RunPython(drop_orphan_columns, migrations.RunPython.noop, elidable=False),
    ]
