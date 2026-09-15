"""Signal handlers for the auctions app."""

import datetime
import logging

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.contrib.sites.models import Site
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django_ses.signals import bounce_received, complaint_received

from .services import (
    SHARED_MEMBER_FIELDS,
    clear_bidder_number_in,
    club_managed_auctions_for,
    club_managed_shadows_for,
    set_member_bidder_number,
    shared_member_values,
    sync_member_to_shadows,
)
from .site_setup import ensure_single_club_membership_for_user

logger = logging.getLogger(__name__)

# Email timing constants (in hours)
WELCOME_EMAIL_DELAY_HOURS = 24
INVOICE_EMAIL_DELAY_HOURS = 1
FOLLOWUP_EMAIL_DELAY_HOURS = 24


def _associate_auctions_for_member(member):
    """Link this member's user's unlinked auctions to the club, only if they chose this club themselves,
    so a granted permission can't claim auctions.
    """
    if not member.user:
        return
    try:
        if member.user.userdata.club != member.club:
            return
    except AttributeError:
        return
    from .models import Auction, AuctionHistory

    club = member.club
    auctions_to_update = list(Auction.objects.filter(created_by=member.user, club__isnull=True, is_deleted=False))
    for auction in auctions_to_update:
        Auction.objects.filter(pk=auction.pk).update(club=club)
        AuctionHistory.objects.create(
            auction=auction,
            user=None,
            action=f"Automatically associated with club '{club}' because {member.display_name} was granted a club permission.",
            applies_to="RULES",
        )
        # The bulk update skips Auction.save(), so book the ledger by hand.
        auction.backfill_club_money()


@receiver(pre_save, sender="auctions.Auction")
def on_save_auction(sender, instance, **kwargs):
    """This is run when an auction is saved"""
    if instance.date_end and instance.date_start:
        # if the user entered an end time that's after the start time
        if instance.date_end < instance.date_start:
            new_start = instance.date_end
            instance.date_end = instance.date_start
            instance.date_start = new_start
    if not instance.date_end:
        if instance.is_online:
            instance.date_end = instance.date_start + datetime.timedelta(days=7)
    if not instance.lot_submission_end_date:
        if instance.is_online:
            instance.lot_submission_end_date = instance.date_end
        else:
            instance.lot_submission_end_date = instance.date_start
    if not instance.lot_submission_start_date:
        if instance.is_online:
            instance.lot_submission_start_date = instance.date_start
        else:
            instance.lot_submission_start_date = instance.date_start - datetime.timedelta(days=7)
    # if the lot submission end date is badly set, fix it
    if instance.is_online:
        if instance.lot_submission_end_date > instance.date_end:
            instance.lot_submission_end_date = instance.date_end
    if instance.lot_submission_start_date > instance.date_start:
        instance.lot_submission_start_date = instance.date_start

    # Some validation for online bidding with in-person auctions for #189
    if not instance.is_online and instance.online_bidding != "disable":
        if not instance.date_online_bidding_ends:
            instance.date_online_bidding_ends = instance.date_start
        if not instance.date_online_bidding_starts:
            instance.date_online_bidding_starts = instance.date_start - datetime.timedelta(days=7)
        if instance.date_online_bidding_ends < instance.date_online_bidding_starts:
            new_start = instance.date_online_bidding_ends
            instance.date_online_bidding_ends = instance.date_online_bidding_starts
            instance.date_online_bidding_starts = new_start

    # if this is an existing auction
    if instance.pk:
        logger.info("updating date end on lots because this is an existing auction")
        if instance.date_end:
            if instance.date_end + datetime.timedelta(minutes=60) < timezone.now():
                from auctions.models import Lot

                lots = Lot.objects.exclude(is_deleted=True).filter(
                    auction=instance.pk,
                    winner__isnull=True,
                    auctiontos_winner__isnull=True,
                    active=True,
                )
                for lot in lots:
                    lot.date_end = instance.date_end
                    lot.save()
        if not instance.is_online and instance.number_of_locations == 1:
            location = instance.location_qs.first()
            location.pickup_time = instance.date_start
            location.save()

        # Update email due dates when auction dates change (only if not already sent)
        if not instance.invoice_email_sent:
            if instance.is_online and instance.date_end:
                instance.invoice_email_due = instance.date_end + datetime.timedelta(hours=INVOICE_EMAIL_DELAY_HOURS)
        if not instance.followup_email_sent:
            if instance.is_online and instance.date_end:
                instance.followup_email_due = instance.date_end + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)
            elif not instance.is_online and instance.date_start:
                instance.followup_email_due = instance.date_start + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)

    else:
        # logic for new auctions goes here
        instance.welcome_email_due = timezone.now() + datetime.timedelta(hours=WELCOME_EMAIL_DELAY_HOURS)
        if instance.is_online:
            if instance.date_end:
                instance.invoice_email_due = instance.date_end + datetime.timedelta(hours=INVOICE_EMAIL_DELAY_HOURS)
            if instance.date_end:
                instance.followup_email_due = instance.date_end + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)
        else:
            instance.invoice_email_sent = True
            if instance.date_start:
                instance.followup_email_due = instance.date_start + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)
    if not instance.is_online:
        try:
            from auctions.models import PickupLocation

            PickupLocation.objects.get_or_create(
                auction=instance,
                is_default=True,
                defaults={
                    "name": str(instance)[:50],
                    "pickup_time": instance.date_start,
                },
            )
        except Exception:
            pass


@receiver(pre_save, sender="auctions.UserData")
@receiver(pre_save, sender="auctions.PickupLocation")
@receiver(pre_save, sender="auctions.Club")
@receiver(pre_save, sender="auctions.Speaker")
def update_user_location(sender, instance, **kwargs):
    """Store lat/lng from location_coordinates field."""
    try:
        cutLocation = instance.location_coordinates.split(",")
        instance.latitude = float(cutLocation[0])
        instance.longitude = float(cutLocation[1])
    except:
        pass


@receiver(pre_save, sender="auctions.Club")
def stash_previous_club_state(sender, instance, **kwargs):
    """Snapshot barcode flag, icon and name so post_save handlers can detect changes."""
    if instance.pk:
        from .models import Club

        prev = Club.objects.filter(pk=instance.pk).values("show_member_barcode", "icon", "name").first() or {}
        instance._previous_show_member_barcode = prev.get("show_member_barcode")
        instance._previous_icon_name = prev.get("icon") or ""
        instance._previous_name = prev.get("name") or ""
    else:
        instance._previous_show_member_barcode = None
        instance._previous_icon_name = ""
        instance._previous_name = ""


@receiver(post_save, sender="auctions.Club")
def revoke_wallet_passes_on_mode_change(sender, instance, created, **kwargs):
    """When barcodes are toggled, update Wallet passes. Google expires objects on disable; Apple pushes
    void/unvoid either way.
    """
    if created:
        return
    prev = getattr(instance, "_previous_show_member_barcode", None)
    current = instance.show_member_barcode
    if prev == current:
        return
    from .tasks import expire_google_wallet_objects_for_club, notify_apple_wallet_devices_for_club

    if not current:
        transaction.on_commit(lambda: expire_google_wallet_objects_for_club.delay(instance.pk))
    transaction.on_commit(lambda: notify_apple_wallet_devices_for_club.delay(instance.pk))


@receiver(post_save, sender="auctions.Auction")
def mirror_auction_to_club_calendar(sender, instance, **kwargs):
    """Mirror the auction onto its club's calendar in the same transaction. Pushing to Google and Discord
    is the periodic sync's job.
    """
    if not instance.club_id:
        return
    from auctions import club_events

    try:
        club_events.sync_one_auction_event(instance)
        club_events.sync_pickup_events(instance)
    except Exception:
        # Never let a calendar problem be the reason an auction can't be saved.
        logger.exception("Could not mirror auction %s onto its club calendar", instance.pk)


@receiver(post_save, sender="auctions.PickupLocation")
def refresh_calendar_pickups(sender, instance, **kwargs):
    """Re-mirror when a pickup location or its times change; they're usually saved after the auction."""
    if not instance.auction_id:
        return
    from auctions import club_events

    try:
        club_events.sync_one_auction_event(instance.auction)
        club_events.sync_pickup_events(instance.auction)
    except Exception:
        logger.exception("Could not refresh calendar pickups for auction %s", instance.auction_id)


# What Discord shows about an auction. Any of these changing makes its scheduled event stale.
DISCORD_AUCTION_FIELDS = (
    "title",
    "slug",
    "date_start",
    "date_end",
    "is_online",
    "promote_this_auction",
    "is_deleted",
)


@receiver(pre_save, sender="auctions.Auction")
def stash_previous_auction_discord_state(sender, instance, **kwargs):
    """Snapshot what Discord shows for this auction. Only auctions with an event pay for the query."""
    instance._previous_discord_state = None
    if not instance.pk or not instance.discord_event_id:
        return
    from .models import Auction

    instance._previous_discord_state = Auction.objects.filter(pk=instance.pk).values(*DISCORD_AUCTION_FIELDS).first()


@receiver(post_save, sender="auctions.Auction")
def flag_stale_auction_discord_event(sender, instance, **kwargs):
    """Flag an auction's Discord event for update when it moves, is renamed or is unpromoted."""
    if not instance.discord_event_id:
        return
    previous = getattr(instance, "_previous_discord_state", None)
    if not previous or all(previous[field] == getattr(instance, field) for field in DISCORD_AUCTION_FIELDS):
        return
    from .models import Auction

    # A queryset update, not instance.save(): saving here would re-enter this signal.
    Auction.objects.filter(pk=instance.pk).update(discord_event_needs_update=True)


@receiver(pre_delete, sender="auctions.Auction")
def remove_auction_discord_event(sender, instance, **kwargs):
    """Remove an auction's own Discord event, which isn't tracked on a ClubEvent."""
    if not instance.club_id or not instance.club.discord_server_id:
        return
    from auctions import discord_events
    from auctions.models import Auction

    # Re-read: the cascade may already have taken this down via the event's own handler.
    event_id = Auction.objects.filter(pk=instance.pk).values_list("discord_event_id", flat=True).first()
    if not event_id:
        return
    try:
        discord_events.cancel_scheduled_event(instance.club.discord_server_id, event_id)
    except Exception:
        logger.exception("Could not remove the Discord event for deleted auction %s", instance.pk)


@receiver(pre_delete, sender="auctions.ClubEvent")
def remove_event_from_calendars(sender, instance, **kwargs):
    """Take an event off Google and Discord before its row is gone. Catches every cascade."""
    from auctions import club_events

    try:
        club_events._remove_remote(instance)
    except Exception:
        logger.exception("Could not remove calendar event %s from Google and Discord", instance.pk)


@receiver(pre_save, sender="auctions.ClubMember")
def stash_previous_clubmember_state(sender, instance, **kwargs):
    """Snapshot auction-permission and wallet-relevant fields for the post_save handlers."""
    if instance.pk:
        from .models import ClubMember

        prev = (
            ClubMember.objects.filter(pk=instance.pk)
            .values(
                "bidder_number",
                "bidding_allowed",
                "selling_allowed",
                "name",
                "phone_number",
                "membership_number",
                "membership_expiration_date",
                "membership_last_paid",
                "address",
                "email",
                "is_deleted",
            )
            .first()
            or {}
        )
        instance._previous_bidder_number = prev.get("bidder_number")
        instance._previous_bidding_allowed = prev.get("bidding_allowed")
        instance._previous_selling_allowed = prev.get("selling_allowed")
        instance._previous_name = prev.get("name") or ""
        instance._previous_phone_number = prev.get("phone_number") or ""
        instance._previous_membership_number = prev.get("membership_number")
        instance._previous_membership_expiration_date = prev.get("membership_expiration_date")
        instance._previous_membership_last_paid = prev.get("membership_last_paid")
        instance._previous_address = prev.get("address") or ""
        instance._previous_email = prev.get("email") or ""
        instance._previous_is_deleted = prev.get("is_deleted")
    else:
        instance._previous_bidder_number = None
        instance._previous_bidding_allowed = None
        instance._previous_selling_allowed = None
        instance._previous_name = ""
        instance._previous_phone_number = ""
        instance._previous_membership_number = None
        instance._previous_membership_expiration_date = None
        instance._previous_membership_last_paid = None
        instance._previous_address = ""
        instance._previous_email = ""
        instance._previous_is_deleted = None


@receiver(post_save, sender="auctions.ClubMember")
def propagate_clubmember_to_shadow_tos(sender, instance, created, **kwargs):
    """Push a changed ClubMember onto every participant row that is the same person.

    Shared details and the bidder number go everywhere, finished auctions included. A bidder number
    first displaces whoever holds it (``services.clear_bidder_number_in``), with history, rather than
    being silently skipped.

    A new member gets shadow rows in club-managed auctions that auto-add ("all" or "checkin"), with the
    number checked against both club and auction.
    """
    from .models import AuctionTOS, PickupLocation

    if created:
        # Finished auctions too: the member is findable there, and attendance counts checked_in.
        for auction in club_managed_auctions_for(instance.club):
            default_location = PickupLocation.objects.filter(auction=auction).order_by("-is_default", "pk").first()
            if not default_location:
                continue
            already_exists = AuctionTOS.objects.filter(auction=auction, clubmember=instance).exists()
            if already_exists:
                continue
            if not instance.bidder_number:
                instance.generate_bidder_number(save=True)
            bidding = False if auction.manage_users_through_club == "checkin" else instance.bidding_allowed
            clear_bidder_number_in(auction, instance.bidder_number)
            AuctionTOS.objects.create(
                user=instance.user,
                auction=auction,
                pickup_location=default_location,
                clubmember=instance,
                bidder_number=instance.bidder_number,
                bidding_allowed=bidding,
                selling_allowed=instance.selling_allowed,
                manually_added=True,
                **shared_member_values(instance),
            )
        return

    prev_bidder = getattr(instance, "_previous_bidder_number", None)
    prev_bidding = getattr(instance, "_previous_bidding_allowed", None)
    prev_selling = getattr(instance, "_previous_selling_allowed", None)

    shadows = club_managed_shadows_for(instance)

    if prev_bidding is not None and prev_bidding != instance.bidding_allowed:
        if instance.bidding_allowed:
            # Check-in mode: no bidding until checked in, whatever the club record says.
            shadows.exclude(
                auction__manage_users_through_club="checkin",
                auction__club__isnull=False,
                checked_in__isnull=True,
            ).update(bidding_allowed=True)
        else:
            shadows.update(bidding_allowed=False)
    if prev_selling is not None and prev_selling != instance.selling_allowed:
        shadows.update(selling_allowed=instance.selling_allowed)
    sync_member_to_shadows(instance)
    if prev_bidder is not None and prev_bidder != instance.bidder_number and instance.bidder_number:
        set_member_bidder_number(instance, instance.bidder_number)


@receiver(post_save, sender="auctions.ClubMember")
def update_wallet_passes_on_member_change(sender, instance, created, **kwargs):
    """Refresh a member's Wallet passes when wallet-visible fields change.

    Watches name, membership_number, membership_expiration_date, membership_last_paid (expiry falls back
    to it) and is_deleted (Apple voids the pass). New members have no pass; both tasks no-op.
    """
    if created:
        return
    prev_name = getattr(instance, "_previous_name", "")
    prev_number = getattr(instance, "_previous_membership_number", None)
    prev_expiry = getattr(instance, "_previous_membership_expiration_date", None)
    prev_last_paid = getattr(instance, "_previous_membership_last_paid", None)
    prev_is_deleted = getattr(instance, "_previous_is_deleted", None)

    if (
        prev_name == (instance.name or "")
        and prev_number == instance.membership_number
        and prev_expiry == instance.membership_expiration_date
        and prev_last_paid == instance.membership_last_paid
        and prev_is_deleted == instance.is_deleted
    ):
        return

    from .tasks import notify_apple_wallet_devices_for_member, update_google_wallet_object_for_member

    transaction.on_commit(lambda: update_google_wallet_object_for_member.delay(instance.pk))
    transaction.on_commit(lambda: notify_apple_wallet_devices_for_member.delay(instance.pk))


@receiver(post_save, sender="auctions.ClubMember")
def geocode_club_member_on_address_change(sender, instance, created, **kwargs):
    """Geocode when a member's address is new or changed, or for a new member with none (UserData fallback)."""
    from .tasks import geocode_club_member

    prev_address = getattr(instance, "_previous_address", "")
    current_address = instance.address or ""
    address_changed = created or (current_address != prev_address)
    if address_changed:
        transaction.on_commit(lambda: geocode_club_member.delay(instance.pk))


def _club_member_mailchimp_connected(member_id):
    """Cheap check (plaintext columns only) that a member's club has Mailchimp connected."""
    from .models import ClubMember

    return (
        ClubMember.objects.filter(pk=member_id)
        .exclude(club__mailchimp_audience_id="")
        .exclude(club__mailchimp_server_prefix="")
        .exists()
    )


def _club_member_brevo_connected(member_id):
    """Cheap check (plaintext columns only) that a member's club has Brevo connected."""
    from .models import ClubMember

    return ClubMember.objects.filter(pk=member_id).exclude(club__brevo_list_id="").exists()


@receiver(post_save, sender="auctions.ClubMember")
def sync_clubmember_to_mailchimp(sender, instance, created, **kwargs):
    """Keep the member's Mailchimp contact in sync; an email change moves the contact instead of duplicating."""
    club = instance.club
    if not club or not club.mailchimp_connected:
        return
    from .tasks import sync_club_member_email_change, sync_club_member_to_mailchimp

    pk = instance.pk
    prev_email = getattr(instance, "_previous_email", "") or ""
    current_email = instance.email or ""
    if not created and prev_email and prev_email != current_email:
        transaction.on_commit(lambda old=prev_email: sync_club_member_email_change.delay(pk, old))
    else:
        transaction.on_commit(lambda: sync_club_member_to_mailchimp.delay(pk))


@receiver(post_save, sender="auctions.ClubMember")
def sync_clubmember_to_brevo(sender, instance, created, **kwargs):
    """Brevo equivalent of sync_clubmember_to_mailchimp (one-way per-member sync on save)."""
    club = instance.club
    if not club or not club.brevo_connected:
        return
    from .tasks import sync_club_member_email_change_brevo, sync_club_member_to_brevo

    pk = instance.pk
    prev_email = getattr(instance, "_previous_email", "") or ""
    current_email = instance.email or ""
    if not created and prev_email and prev_email != current_email:
        transaction.on_commit(lambda old=prev_email: sync_club_member_email_change_brevo.delay(pk, old))
    else:
        transaction.on_commit(lambda: sync_club_member_to_brevo.delay(pk))


#: Placeholders ``AuctionTOS.save()`` writes into blank fields; never carried up to the member.
BLANK_MARKERS = {"name": {"", "Unknown"}, "bidder_number": {"", "ERROR"}}


def _worth_carrying_up(field, value):
    value = (value or "").strip()
    return bool(value) and value not in BLANK_MARKERS.get(field, set())


@receiver(post_save, sender="auctions.AuctionTOS")
def sync_auctiontos_up_to_clubmember(sender, instance, **kwargs):
    """Carry an auction-side edit up to the club member, which then flows to every other auction.

    Only when something differs. ``update_fields`` is honoured on purpose: the club member form saves
    a participant row it loaded before the member edit, and carrying its stale fields up would undo it.
    """
    from .models import ClubMember

    member = instance.clubmember
    if member is None or not instance.auction.is_club_managed:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not set(update_fields) & {*SHARED_MEMBER_FIELDS, "bidder_number"}:
        return
    changed = [
        field
        for field in SHARED_MEMBER_FIELDS
        if _worth_carrying_up(field, getattr(instance, field, None))
        and (getattr(instance, field, None) or "") != (getattr(member, field, None) or "")
    ]
    number = (instance.bidder_number or "").strip()
    number_changed = _worth_carrying_up("bidder_number", number) and number != (member.bidder_number or "").strip()
    if not changed and not number_changed:
        return
    if changed:
        for field in changed:
            setattr(member, field, getattr(instance, field) or "")
        # update(): the member's post_save would push these back down.
        ClubMember.objects.filter(pk=member.pk).update(**{field: getattr(member, field) for field in changed})
        sync_member_to_shadows(member)
    if number_changed:
        set_member_bidder_number(member, number)


@receiver(post_save, sender="auctions.AuctionTOS")
def sync_clubmember_to_mailchimp_on_auctiontos(sender, instance, **kwargs):
    """Auction join / check-in changes the linked member's tags (e.g. auction-checkin)."""
    member_id = instance.clubmember_id
    if not member_id or not _club_member_mailchimp_connected(member_id):
        return
    from .tasks import sync_club_member_to_mailchimp

    transaction.on_commit(lambda: sync_club_member_to_mailchimp.delay(member_id))


@receiver(post_save, sender="auctions.AuctionTOS")
def sync_clubmember_to_brevo_on_auctiontos(sender, instance, **kwargs):
    """Brevo equivalent: auction join / check-in changes the linked member's tags."""
    member_id = instance.clubmember_id
    if not member_id or not _club_member_brevo_connected(member_id):
        return
    from .tasks import sync_club_member_to_brevo

    transaction.on_commit(lambda: sync_club_member_to_brevo.delay(member_id))


@receiver(pre_save, sender="auctions.Invoice")
def stash_previous_invoice_status(sender, instance, **kwargs):
    if instance.pk:
        from .models import Invoice

        instance._previous_status = Invoice.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    else:
        instance._previous_status = None


@receiver(post_save, sender="auctions.Invoice")
def sync_clubmember_to_mailchimp_on_invoice_paid(sender, instance, created, **kwargs):
    """When an invoice becomes PAID, refresh the linked member (totals -> power-buyer/seller)."""
    if instance.status != "PAID" or getattr(instance, "_previous_status", None) == "PAID":
        return
    tos = instance.auctiontos_user
    member_id = getattr(tos, "clubmember_id", None) if tos else None
    if member_id and _club_member_mailchimp_connected(member_id):
        from .tasks import sync_club_member_to_mailchimp

        transaction.on_commit(lambda: sync_club_member_to_mailchimp.delay(member_id))
    if member_id and _club_member_brevo_connected(member_id):
        from .tasks import sync_club_member_to_brevo

        transaction.on_commit(lambda: sync_club_member_to_brevo.delay(member_id))


@receiver(pre_save, sender=User)
def stash_previous_user_email(sender, instance, **kwargs):
    if instance.pk:
        instance._previous_email = User.objects.filter(pk=instance.pk).values_list("email", flat=True).first() or ""
    else:
        instance._previous_email = ""


@receiver(post_save, sender=User)
def propagate_user_email_change_to_members(sender, instance, created, **kwargs):
    """Move a user's club memberships (with the old email only) to their new account email."""
    if created:
        return
    old_email = getattr(instance, "_previous_email", "") or ""
    new_email = instance.email or ""
    if not old_email or old_email == new_email:
        return
    from .models import ClubHistory, ClubMember

    for member in ClubMember.objects.filter(user=instance, email__iexact=old_email, is_deleted=False):
        member.email = new_email
        member.save(update_fields=["email"])
        # The club never sees the account change, so record it.
        ClubHistory.objects.create(
            club=member.club,
            user=instance,
            action=f"{member} changed their account email from {old_email} to {new_email}",
            applies_to="MEMBERS",
        )


@receiver(pre_save, sender="auctions.Lot")
def update_lot_info(sender, instance, **kwargs):
    """Fill out the location and address from the user; set end date from auction."""
    if not instance.pk:
        if instance.auction:
            instance.date_end = instance.auction.date_end
    if instance.user:
        from auctions.models import UserData

        userData, created = UserData.objects.get_or_create(
            user=instance.user,
            defaults={},
        )
        instance.latitude = userData.latitude
        instance.longitude = userData.longitude
        instance.address = userData.address

    if instance.auction and (not instance.reserve_price or instance.reserve_price < instance.auction.minimum_bid):
        instance.reserve_price = instance.auction.minimum_bid


def link_unattached_tos_for_user(user, reason="duplicate detected on login"):
    """Link AuctionTOS rows matching this user's email with no user, merging with an existing row in the
    same auction. Shared by the login signal and ``relink_auctiontos_users``. Also claims lots sold
    while unlinked, which have ``user=None``.
    """
    from auctions.models import AuctionTOS, Lot

    linked_tos_pks = []
    auctiontoss = AuctionTOS.objects.filter(user__isnull=True, email=user.email)
    for auctiontos in auctiontoss:
        existing = AuctionTOS.objects.filter(user=user, auction=auctiontos.auction).first()
        if existing:
            if auctiontos.createdon and existing.createdon and auctiontos.createdon < existing.createdon:
                canonical, duplicate = auctiontos, existing
                canonical.user = user
                AuctionTOS.objects.filter(pk=canonical.pk).update(user=user)
            else:
                canonical, duplicate = existing, auctiontos
            canonical.merge_duplicate(duplicate, reason=reason)
            linked_tos_pks.append(canonical.pk)
        else:
            auctiontos.user = user
            auctiontos.save()
            linked_tos_pks.append(auctiontos.pk)
    if linked_tos_pks:
        Lot.objects.filter(auctiontos_seller__pk__in=linked_tos_pks, user__isnull=True).update(user=user)


@receiver(user_logged_in)
def user_logged_in_callback(sender, user, request, **kwargs):
    """On sign-in: cancel a pending account deletion, link unattached AuctionTOS and ClubMember rows."""
    # Signing in is how a pending deletion is called off.
    from auctions.account_deletion import cancel_deletion

    if cancel_deletion(user) and request is not None and hasattr(request, "_messages"):
        # Not every sign-in has message middleware (JWT, WebView handoff); the email says it too.
        from django.contrib import messages

        messages.info(
            request,
            "Welcome back!  Your account was scheduled to be deleted, and signing in has cancelled that.",
        )

    link_unattached_tos_for_user(user)
    record_sign_in_stitch(user, request)

    from auctions.models import ClubMember

    # No ClubHistory: automatic, no actor.
    ClubMember.objects.filter(user__isnull=True, email=user.email, is_deleted=False).update(user=user)
    ensure_single_club_membership_for_user(user)


def record_sign_in_stitch(user, request):
    """Remember which anonymous session this person held when they signed in (see ``SignInStitch``).

    Read from the cookie, not the session: ``login()`` cycles the key before this signal. Silent with
    no session cookie (JWT, handoff, commands).
    """
    from django.conf import settings

    from auctions.models import SignInStitch

    if request is None:
        return
    key = (request.COOKIES or {}).get(settings.SESSION_COOKIE_NAME)
    if not key:
        return
    # Same browser again is the same stitch.
    SignInStitch.objects.get_or_create(user=user, session_id=key[:600])


@receiver(post_save, sender=User)
def create_user_userdata(sender, instance, created, **kwargs):
    if created:
        from auctions.models import UserData

        UserData.objects.create(user=instance)
    ensure_single_club_membership_for_user(instance)


@receiver(post_save, sender="auctions.Club")
def ensure_google_wallet_class(sender, instance, created, **kwargs):
    """Create the Google Wallet class once per club (visuals are per object), on commit."""
    if instance.google_wallet_class_created:
        return
    from .tasks import create_google_wallet_class_for_club

    transaction.on_commit(lambda: create_google_wallet_class_for_club.delay(instance.pk))


@receiver(post_save, sender="auctions.Club")
def refresh_wallet_passes_on_club_change(sender, instance, created, **kwargs):
    """Refresh member passes when the club's name or icon changes: Google keeps visuals on each object,
    Apple bakes them into each pass.
    """
    if created:
        return
    prev_name = getattr(instance, "_previous_name", "")
    prev_icon = getattr(instance, "_previous_icon_name", "")
    current_icon = instance.icon.name if instance.icon else ""
    if prev_name == instance.name and prev_icon == current_icon:
        return
    from .tasks import notify_apple_wallet_devices_for_club, update_google_wallet_objects_for_club

    transaction.on_commit(lambda: update_google_wallet_objects_for_club.delay(instance.pk))
    transaction.on_commit(lambda: notify_apple_wallet_devices_for_club.delay(instance.pk))


@receiver(bounce_received)
def bounce_handler(sender, mail_obj, bounce_obj, raw_message, *args, **kwargs):
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    from auctions.models import AuctionTOS, ClubMember

    AuctionTOS.objects.filter(email=email).update(email_address_status="BAD")
    # No ClubHistory: the actor is the email provider.
    ClubMember.objects.filter(email=email, is_deleted=False).update(email_address_status="BAD")


@receiver(complaint_received)
def complaint_handler(sender, mail_obj, complaint_obj, raw_message, *args, **kwargs):
    from .models import ClubHistory, ClubMember

    recipient_list = mail_obj["destination"]
    email = recipient_list[0]

    user = User.objects.filter(email=email).first()
    if user:
        # Unsubscribe user from all emails without touching club members
        userdata = user.userdata
        userdata.email_me_about_new_auctions = False
        userdata.email_me_about_new_local_lots = False
        userdata.email_me_about_new_lots_ship_to_location = False
        userdata.email_me_when_people_comment_on_my_lots = False
        userdata.email_me_about_new_chat_replies = False
        userdata.send_reminder_emails_about_joining_auctions = False
        userdata.email_me_about_new_in_person_auctions = False
        userdata.has_unsubscribed = True
        userdata.last_activity = timezone.now()
        userdata.save()

    members = ClubMember.objects.filter(email=email, is_deleted=False).exclude(contact_status="do_not_contact")
    for member in members:
        member.contact_status = "do_not_contact"
        member.save(update_fields=["contact_status"])
        ClubHistory.objects.create(
            club=member.club,
            user=None,
            action=f"{member} marked do not contact after SES complaint",
            applies_to="MEMBERS",
        )


@receiver(post_save, sender="auctions.ClubMember")
def on_club_member_saved(sender, instance, **kwargs):
    """When a member gains permission_admin or permission_manage_auctions, auto-associate their auctions."""
    if instance.permission_admin or instance.permission_manage_auctions:
        _associate_auctions_for_member(instance)


@receiver(post_delete, sender="auctions.LotImage")
@receiver(post_delete, sender="auctions.Club")
@receiver(post_delete, sender="auctions.AdCampaign")
@receiver(post_delete, sender="auctions.Speaker")
def on_cloudflare_image_row_deleted(sender, instance, **kwargs):
    """Queue deletion of the Cloudflare copy when an image row is deleted. The task skips it if another
    row still uses the image. Local files aren't touched.
    """
    from . import cloudflare_images

    if (
        instance.cloudflare_image_id
        and instance.cloudflare_image_id != cloudflare_images.UPLOAD_FAILED
        and cloudflare_images.enabled()
    ):
        from .tasks import delete_cloudflare_image

        # on_commit: post_delete is inside the delete transaction, and a rollback would leave the row
        # pointing at a deleted Cloudflare image.
        transaction.on_commit(lambda image_id=instance.cloudflare_image_id: delete_cloudflare_image.delay(image_id))


@receiver(post_delete, sender="auctions.LotImage")
@receiver(post_delete, sender="auctions.Lot")
@receiver(post_delete, sender="auctions.Club")
@receiver(post_delete, sender="auctions.AdCampaign")
@receiver(post_delete, sender="auctions.Speaker")
def on_uploaded_image_deleted(sender, instance, **kwargs):
    """Delete the uploaded file, its thumbnails, and the edge-cached copy.

    ``/media/`` is unauthenticated, so a "deleted" image stayed at the URL a takedown quoted, which is
    not the expeditious removal 17 U.S.C. 512(c)(1)(C) asks for. The edge purge is queued
    (:func:`auctions.tasks.purge_edge_cache`); Cloudflare Images is handled above.

    **A file two rows share is left alone**: ``clone_lot_images`` reuses the file.
    """
    from easy_thumbnails.files import get_thumbnailer

    field_name = getattr(instance, "IMAGE_FIELD_NAME", "image")
    field_file = getattr(instance, field_name, None)
    if not field_file or not field_file.name:
        return
    name = field_file.name
    if sender.objects.filter(**{field_name: name}).exists():
        return

    urls = []
    thumbnailer = get_thumbnailer(field_file)
    try:
        urls.append(field_file.url)
        for thumbnail in thumbnailer.get_thumbnails():
            urls.append(thumbnail.url)
    except Exception:
        # The file still goes; only the purge list is shorter.
        logger.exception("Could not list files to purge for %s %s", sender.__name__, name)
    try:
        thumbnailer.delete_thumbnails()
        field_file.delete(save=False)
    except Exception:
        logger.exception("Could not delete the file for %s %s", sender.__name__, name)
        return

    absolute = []
    try:
        domain = Site.objects.get_current().domain
    except Exception:
        domain = ""
    for url in urls:
        absolute.append(f"https://{domain}{url}" if domain and url.startswith("/") else url)
    if absolute:
        from .tasks import purge_edge_cache

        # on_commit, as above.
        transaction.on_commit(lambda purge=absolute: purge_edge_cache.delay(purge))


@receiver(post_save, sender="auctions.ThermalPrinterProfile")
def notify_users_their_printer_is_supported(sender, instance, **kwargs):
    """Push to users whose hand-identified printer this profile now matches, so they reconnect. Only
    ``manual`` or unmatched observations.
    """
    from auctions.models import ObservedPrinter
    from auctions.printer_drafts import profile_matches_observation

    if not instance.enabled:
        return

    candidates = ObservedPrinter.objects.filter(support_notified=False).filter(
        models.Q(matched_by="manual") | models.Q(profile_slug="")
    )
    notified_users = set()
    for observation in candidates.select_related("user"):
        if not profile_matches_observation(instance, observation):
            continue
        if observation.user_id in notified_users or _push_printer_supported(observation, instance):
            notified_users.add(observation.user_id)
            ObservedPrinter.objects.filter(pk=observation.pk).update(support_notified=True)


def _push_printer_supported(observation, profile):
    """Enqueue the push for one observation; False leaves it unnotified so it arrives if push is enabled later."""
    from auctions import notifications
    from auctions.tasks import send_push_to_user

    if not observation.user_id or not notifications.user_prefers_push(observation.user):
        return False
    printer = observation.model or observation.ble_name or "Your Bluetooth printer"

    def enqueue():
        try:
            send_push_to_user.delay(
                observation.user_id,
                title="Your printer is supported now",
                body=f"{printer} works with label printing now. Open the printing page and reconnect it.",
                url="/printing/",
                category=notifications.CATEGORY_PRINTER,
                collapse_key=f"printer-supported-{profile.slug}",
            )
        except Exception:
            # A notification must never block enabling a profile.
            logger.exception("Could not enqueue the printer-supported push for user %s", observation.user_id)

    transaction.on_commit(enqueue)
    return True


@receiver(pre_save, sender="auctions.Speaker")
def stash_previous_speaker_location(sender, instance, **kwargs):
    """Snapshot the location text so post_save can tell whether it needs re-geocoding."""
    if instance.pk:
        from .models import Speaker

        prev = Speaker.objects.filter(pk=instance.pk).values("location").first() or {}
        instance._previous_location = prev.get("location") or ""
    else:
        instance._previous_location = ""


@receiver(post_save, sender="auctions.Speaker")
def geocode_speaker_on_location_change(sender, instance, created, **kwargs):
    """Geocode a speaker whose location text changed, unless this save already placed coordinates by hand."""
    from .tasks import geocode_speaker

    current_location = instance.location or ""
    if not current_location:
        return
    location_changed = created or (current_location != getattr(instance, "_previous_location", ""))
    if location_changed and not instance.location_coordinates:
        transaction.on_commit(lambda: geocode_speaker.delay(instance.pk))
