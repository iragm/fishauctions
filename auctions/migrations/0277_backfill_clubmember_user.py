"""
Data migration: link existing ClubMember rows to a User when their email matches.

ClubMember.save() now auto-associates user via email on every save, but historical
records created before that hook are still missing the link. This backfill walks
unlinked active members and points them at the first matching User (case-insensitive
email). The same logic also reruns the BAP eligibility backfill (migration 0273)
since matching is now more permissive.
"""

from django.db import migrations


def backfill_clubmember_user(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    User = apps.get_model("auth", "User")
    unlinked = (
        ClubMember.objects.filter(user__isnull=True, is_deleted=False).exclude(email="").exclude(email__isnull=True)
    )
    updates = []
    for member in unlinked.iterator(chunk_size=500):
        match = User.objects.filter(email__iexact=member.email).order_by("pk").first()
        if match:
            member.user = match
            updates.append(member)
    if updates:
        ClubMember.objects.bulk_update(updates, ["user"], batch_size=500)


def rerun_bap_backfill(apps, schema_editor):
    from django.db.utils import OperationalError  # noqa: PLC0415

    from auctions.models import Lot  # noqa: PLC0415

    lots_to_check = (
        Lot.objects.filter(
            is_deleted=False,
            auction__club__enable_breeder_award_program=True,
        )
        .exclude(bap_award__isnull=False)
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
            reason = lot.sold_lot_no_bap_reason or ""
            if lot.bap_auto_reason != reason:
                lot.bap_auto_reason = reason
                updates.append(lot)
    except OperationalError:
        return
    if updates:
        Lot.objects.bulk_update(updates, ["bap_auto_reason"], batch_size=500)


def forwards(apps, schema_editor):
    backfill_clubmember_user(apps, schema_editor)
    rerun_bap_backfill(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0276_auctiontos_checked_in_auctiontos_door_prize_called"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
