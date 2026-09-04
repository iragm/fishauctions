"""Two indexes for queries that were finding their rows and then sorting them by hand.

Both back a `filter(...).order_by(...).first()` that had an index for the filter and nothing for
the order, so MariaDB read every matching row and sorted it to return one.

* **PageView(user, -date_start)** -- every lot list a signed-in person opens asks for the date of
  their most recent lot view, to badge lots as new. `user` alone found all of that person's page
  views (thousands, for anybody who uses the site) and filesorted them. **This one is expensive to
  apply**: PageView is the biggest table here. InnoDB can build it in place, but budget for it and
  run it when the site is quiet.
* **Invoice(auctiontos_user, -date)** -- `AuctionTOS.invoice`, read once per row of the users
  table. A small table; this is cheap.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0422_club_bap_ytd_reset_year_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="pageview",
            index=models.Index(fields=["user", "-date_start"], name="pageview_user_recent_idx"),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["auctiontos_user", "-date"], name="invoice_tos_recent_idx"),
        ),
    ]
