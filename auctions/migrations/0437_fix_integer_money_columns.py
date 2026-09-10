from django.db import migrations

#: What MariaDB calls a column that is holding whole numbers.  A money column that is one of these
#: never went through the DECIMAL conversion in migration 0227.
INTEGER_DATA_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "integer", "bigint"})


def fix_integer_money_columns(apps, schema_editor):
    """Give every ``DecimalField`` a DECIMAL column, on databases where one is still an integer.

    Migration 0227 turned the money columns -- ``Lot.reserve_price``, ``Lot.buy_now_price``,
    ``Lot.winning_price``, ``Auction.minimum_bid``, ``Bid.amount``, ``LotHistory.bid_amount`` and
    ``LotHistory.current_price`` -- from integers into ``DECIMAL(10, 2)``, which is what made bids
    with cents possible.  A database where one of those ``AlterField``s did not take is a database
    Django believes is migrated: the field says DecimalField, the migration is recorded as applied,
    and nothing checks the column.

    Two things go wrong there, and only one of them is visible.  Reading is silent -- mysqlclient
    hands back whatever type the column is, so ``lot.winning_price`` is an ``int``, and every price
    read from that column is one, ``Decimal`` methods and all.  Turning on whole-dollar bids called
    ``.to_integral_value()`` on one and 500'd with ``'int' object has no attribute
    'to_integral_value'``.  Writing is worse for being quiet: MariaDB rounds ``5.50`` to ``6`` on
    the way into an integer column, so an auction with whole-dollar bids turned *off* cannot store
    the cents it just accepted.

    Doing this by rule rather than by naming those seven columns is what keeps it true: any
    DecimalField whose column is an integer type is wrong by definition, whichever migration was
    supposed to have converted it.  On a database built from these migrations there is nothing to
    find and this is a no-op.  The conversion itself is lossless -- every value in an integer
    column is already a whole number of dollars.

    **If this migration fails partway through, run it again.**  It converts by rule, so a rerun
    picks up whatever is still an integer and skips what already converted; there is never a reason
    to fake it.  Faking 0227 is how six columns -- ``auctions_bid.amount`` and everything ordered
    after it -- reached production still holding integers while Django recorded the conversion as
    done, which is the state this migration exists to undo.

    Two things can make it fail, and neither is fixed by skipping it:

    * ``Out of range value for column`` (error 1264) means a row holds more than the DecimalField
      can: an unsigned integer column goes to 4294967295, while ``decimal(10, 2)`` stops at
      99999999.99.  Nothing legitimate is priced there.  Find the rows, correct or delete them, and
      run the migration again::

          docker exec django python3 manage.py shell -c "
          from django.apps import apps
          from django.db import connection
          for model in apps.get_app_config('auctions').get_models():
              table = model._meta.db_table
              for f in model._meta.local_fields:
                  if f.get_internal_type() != 'DecimalField':
                      continue
                  limit = 10 ** (f.max_digits - f.decimal_places) - 1
                  with connection.cursor() as c:
                      c.execute('SELECT id, ' + f.column + ' FROM ' + table
                                + ' WHERE ' + f.column + ' > ' + str(limit))
                      for row in c.fetchall():
                          print(table, f.column, row)
          "

    * A lock wait or a timeout means the table was busy.  Each conversion is an ``ALTER TABLE`` that
      rebuilds the whole table, and ``auctions_bid`` and ``auctions_lothistory`` are the big ones --
      this is almost certainly what stopped 0227 in the first place.  Run it when no auction is
      live, rather than during one.
    """
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
        )
        live_types = {(table, column): data_type.lower() for table, column, data_type in cursor.fetchall()}
    for model in apps.get_models(include_auto_created=True):
        if model._meta.app_label != "auctions":
            continue
        table = model._meta.db_table
        for field in model._meta.local_fields:
            if field.get_internal_type() != "DecimalField":
                continue
            if live_types.get((table, field.column)) not in INTEGER_DATA_TYPES:
                continue
            null = "NULL" if field.null else "NOT NULL"
            definition = f"decimal({field.max_digits}, {field.decimal_places}) {null}"
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{field.column}` {definition}")
            print(f"  converted {table}.{field.column} to {definition}")  # noqa: T201


class Migration(migrations.Migration):
    # DDL on MySQL/MariaDB is non-transactional; don't pretend otherwise.
    atomic = False

    dependencies = [
        ("auctions", "0436_speciessearchcache_scientific_name"),
    ]

    operations = [
        migrations.RunPython(fix_integer_money_columns, migrations.RunPython.noop, elidable=False),
    ]
