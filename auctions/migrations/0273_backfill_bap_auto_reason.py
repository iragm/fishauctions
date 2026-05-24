"""
Data migration: backfill bap_auto_reason for historical sold lots.

Lots sold before the BAP feature was introduced have bap_auto_reason="" (the
field default) regardless of eligibility.  This migration computes the reason
for every sold lot in a BAP-enabled club auction that hasn't been processed yet
(bap_auto_reason still blank, no BapAward attached) and writes the correct
value so the pending-BAP table and selling-dashboard filter can display them.

Only bap_auto_reason is written.  No BapAward records are created and
bap_points_awarded is left untouched; awarding historical points is a separate
admin action.
"""

from django.db import migrations


def backfill_bap_reasons(apps, schema_editor):
    # Import the live model so we can call its property methods (sold_lot_no_bap_reason).
    # This is intentional: the migration is a one-time backfill and the logic
    # lives on the model, so we accept the coupling to current model code.
    from django.db.utils import OperationalError  # noqa: PLC0415

    from auctions.models import Lot  # noqa: PLC0415

    lots_to_check = (
        Lot.objects.filter(
            is_deleted=False,
            bap_auto_reason="",  # not yet processed
            auction__club__enable_breeder_award_program=True,
        )
        .exclude(bap_award__isnull=False)  # already has an award — reason is implicitly "eligible"
        .select_related(
            "auction__club",
            "auctiontos_seller__user",
            "species_category",
            "winner",
            "auctiontos_winner",
        )
    )

    updates = []
    try:
        for lot in lots_to_check.iterator(chunk_size=500):
            reason = lot.sold_lot_no_bap_reason  # None = eligible; non-None = ineligible key
            if reason:  # only update ineligible lots; eligible ones are already correct ("")
                lot.bap_auto_reason = reason
                updates.append(lot)
    except OperationalError:
        # The live model may reference columns added by a later migration that aren't yet
        # present in the DB during a fresh build. This is a best-effort backfill — fresh
        # databases have no historical data to migrate, so skipping is safe.
        return

    if updates:
        Lot.objects.bulk_update(updates, ["bap_auto_reason"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0272_club_create_events_auction_discord_event_created"),
    ]

    operations = [
        migrations.RunPython(backfill_bap_reasons, migrations.RunPython.noop),
    ]
