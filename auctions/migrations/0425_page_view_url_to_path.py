"""Store ``PageView.url`` as a site-relative path, and rewrite the rows that are not.

The beacon in ``base_page_view.html`` posted ``window.location.href``, so every row written from a
browser holds ``https://auction.fish/lots/123`` -- while every *reader* of the column wants a path.
``AdminUserFlow.URL_SECTIONS`` anchors each pattern at ``^/`` and so classified all of them as
"Other"; ``url__startswith="/account/"`` is what makes "how many people opened preferences" a
query rather than a broken one.  The write side is fixed in ``views.ajax.page_view_path``; this
brings the rows already on disk into the same shape so nothing has to tolerate both.

Only *this deployment's own* hosts are stripped, taken from ``ALLOWED_HOSTS`` -- the same list that
decides what ``request.get_host()`` is allowed to return, which is what the write side compares
against.  A URL on any other host is left whole, because the old endpoint stored whatever it was
POSTed and turning ``https://example.com/x`` into ``/x`` would file someone else's page as one of
ours, under a path indistinguishable from a real one.  The cost is that rows carrying a hostname
this deployment does not answer on -- a database copied down from production, a domain the site
used to have -- keep it; they are already not this site's traffic, and each deployment fixes its
own rows when it runs this.

The fragment goes too, for the same reason the query string always did: ``/lots/1`` and
``/lots/1#chat`` are one page, and ``PageView.duplicates`` matches on the column exactly.

A value that is left neither a path nor an ``http(s)`` URL is blanked.  Nothing that writes this
column can produce one -- but the old endpoint stored whatever it was POSTed, it is ``AllowAny``,
and the admin traffic dashboard renders this column as ``<a href="...">``, so a ``javascript:`` URL
sitting in an old row is a link waiting on an admin's page.  ``views.ajax.page_view_path`` refuses
them going forward; this is the same rule applied backwards.

Batched by primary key because this is the biggest table on the site: each statement is one bounded
index range rather than a lock over the whole table.  The reverse is a no-op -- the host that was
stripped is not recoverable from the row, and nothing wants it back.

Cached user-flow results (``user_flow_*``, set with no timeout) still hold pre-migration numbers.
They are not invalidated here: the flow page prints when its numbers were computed and carries the
button that recomputes them.
"""

from django.conf import settings
from django.db import migrations

BATCH = 100_000


def our_hosts():
    """The hostnames this deployment answers on, as a regex alternation.

    ``ALLOWED_HOSTS`` carries blanks (every unset ``ALLOWED_HOST_n``) and may carry the wildcards
    Django allows; neither names a host, so neither is stripped. An empty result means there is
    nothing this migration can safely call ours, and it leaves the rows alone.
    """
    hosts = set()
    for host in settings.ALLOWED_HOSTS:
        host = (host or "").strip().lstrip(".").lower()
        if host and "*" not in host:
            hosts.add(host.replace(".", r"\."))
    return "|".join(sorted(hosts))


def strip_origin_from_urls(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    table = apps.get_model("auctions", "PageView")._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT MIN(id), MAX(id) FROM `{table}`")
        low, high = cursor.fetchone()
    if low is None:
        return
    hosts = our_hosts()
    origin = f"^[hH][tT][tT][pP][sS]?://({hosts})(:[0-9]+)?" if hosts else None
    changed = 0
    while low <= high:
        window = [low, low + BATCH - 1]
        with connection.cursor() as cursor:
            if origin:
                cursor.execute(
                    f"UPDATE `{table}` "
                    "SET url = COALESCE(NULLIF(REGEXP_REPLACE("
                    "    REGEXP_REPLACE(url, %s, ''), '#.*$', ''"
                    "), ''), '/') "
                    "WHERE id BETWEEN %s AND %s AND (url LIKE '%%://%%' OR url LIKE '%%#%%')",
                    [origin, *window],
                )
                changed += cursor.rowcount
            cursor.execute(
                f"UPDATE `{table}` SET url = '' "
                "WHERE id BETWEEN %s AND %s AND url <> '' AND url IS NOT NULL "
                "AND url NOT LIKE '/%%' AND url NOT LIKE 'http://%%' AND url NOT LIKE 'https://%%'",
                window,
            )
            changed += cursor.rowcount
        low += BATCH
    if changed:
        print(f"  rewrote {changed} PageView urls to paths")  # noqa: T201


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0424_alter_lot_image_source_alter_lotimage_image_source_and_more"),
    ]

    operations = [
        migrations.RunPython(strip_origin_from_urls, migrations.RunPython.noop),
    ]
