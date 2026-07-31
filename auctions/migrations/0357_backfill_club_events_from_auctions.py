"""Seed the new club event list from auctions that already exist.

Without this, every club page would look empty until each of its auctions happened to be saved
again. Mirrors the same rules as club_events.sync_one_auction_event, restated here because a
migration must not import app code that will keep changing.
"""

import datetime

from django.db import migrations

DEFAULT_AUCTION_LENGTH = datetime.timedelta(hours=2)


def backfill_events(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    ClubEvent = apps.get_model("auctions", "ClubEvent")

    auctions = (
        Auction.objects.filter(
            is_deleted=False,
            promote_this_auction=True,
            club__isnull=False,
            date_start__isnull=False,
            club__add_auctions_to_calendar=True,
        )
        .select_related("club")
        .exclude(calendar_events__isnull=False)
    )

    events = []
    for auction in auctions.iterator():
        start = auction.date_start
        if auction.is_online and auction.date_end and auction.date_end > start:
            end = auction.date_end
        else:
            end = start + DEFAULT_AUCTION_LENGTH
        events.append(
            ClubEvent(
                club=auction.club,
                auction=auction,
                source="auction",
                title=auction.title,
                description="Online auction with in-person pickup." if auction.is_online else "In-person auction.",
                location="",
                date_start=start,
                date_end=end,
                # Leave the Google push to the periodic sync task rather than the migration.
                needs_google_sync=True,
            )
        )
        if len(events) >= 500:
            ClubEvent.objects.bulk_create(events, ignore_conflicts=True)
            events = []
    if events:
        ClubEvent.objects.bulk_create(events, ignore_conflicts=True)


def drop_auction_events(apps, schema_editor):
    ClubEvent = apps.get_model("auctions", "ClubEvent")
    ClubEvent.objects.filter(source="auction").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0356_club_add_auctions_to_calendar_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_events, drop_auction_events),
    ]
