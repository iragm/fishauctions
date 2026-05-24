"""Signal handlers for the auctions app."""

import datetime
import logging

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django_ses.signals import bounce_received, complaint_received

logger = logging.getLogger(__name__)

# Email timing constants (in hours)
WELCOME_EMAIL_DELAY_HOURS = 24
INVOICE_EMAIL_DELAY_HOURS = 1
FOLLOWUP_EMAIL_DELAY_HOURS = 24


def _associate_auctions_for_member(member):
    """Associate unlinked auctions created by this member's user with the club.

    Only runs when the user has chosen this club in their preferences — prevents
    clubs from claiming auctions by granting a user a permission.
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
    auctions_to_update = Auction.objects.filter(created_by=member.user, club__isnull=True, is_deleted=False)
    for auction in auctions_to_update:
        Auction.objects.filter(pk=auction.pk).update(club=club)
        AuctionHistory.objects.create(
            auction=auction,
            user=None,
            action=f"Automatically associated with club '{club}' because {member.display_name} was granted a club permission.",
            applies_to="RULES",
        )


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
    """Snapshot prev field values so post_save handlers can detect transitions.

    Currently tracks: membership_number_mode (revocation logic), icon name
    (re-push the Wallet class so new logos propagate), and club name
    (refresh member wallet object metadata on rename).
    """
    if instance.pk:
        from .models import Club

        prev = Club.objects.filter(pk=instance.pk).values("membership_number_mode", "icon", "name").first() or {}
        instance._previous_membership_number_mode = prev.get("membership_number_mode")
        instance._previous_icon_name = prev.get("icon") or ""
        instance._previous_name = prev.get("name") or ""
    else:
        instance._previous_membership_number_mode = None
        instance._previous_icon_name = ""
        instance._previous_name = ""


@receiver(post_save, sender="auctions.Club")
def revoke_wallet_passes_on_mode_change(sender, instance, created, **kwargs):
    """When membership_number_mode tightens, expire active Wallet passes.

    Transitions handled:
      * anything → "disabled"   : expire ALL members' Wallet objects
      * anything → "paid_only"  : expire UNPAID members' Wallet objects (only when
                                  the previous mode was not already "paid_only")

    Apple Wallet does not have an equivalent push-revocation API without the
    full Web Service implementation, so we rely on the embedded `expirationDate`
    in the pkpass plus URL gating on re-download.
    """
    if created:
        return
    prev = getattr(instance, "_previous_membership_number_mode", None)
    current = instance.membership_number_mode
    if prev == current:
        return
    from .tasks import expire_google_wallet_objects_for_club

    if current == "disabled":
        transaction.on_commit(lambda: expire_google_wallet_objects_for_club.delay(instance.pk))
    elif current == "paid_only" and prev != "paid_only":
        transaction.on_commit(lambda: expire_google_wallet_objects_for_club.delay(instance.pk, unpaid_only=True))


@receiver(pre_save, sender="auctions.ClubMember")
def stash_previous_clubmember_state(sender, instance, **kwargs):
    """Snapshot per-club auction-permission fields so post_save can detect changes
    and propagate them to linked shadow AuctionTOS records."""
    if instance.pk:
        from .models import ClubMember

        prev = (
            ClubMember.objects.filter(pk=instance.pk)
            .values("bidder_number", "bidding_allowed", "selling_allowed")
            .first()
            or {}
        )
        instance._previous_bidder_number = prev.get("bidder_number")
        instance._previous_bidding_allowed = prev.get("bidding_allowed")
        instance._previous_selling_allowed = prev.get("selling_allowed")
    else:
        instance._previous_bidder_number = None
        instance._previous_bidding_allowed = None
        instance._previous_selling_allowed = None


@receiver(post_save, sender="auctions.ClubMember")
def propagate_clubmember_to_shadow_tos(sender, instance, created, **kwargs):
    """When a ClubMember's bidder_number / bidding_allowed / selling_allowed change,
    push the new values to linked shadow AuctionTOS records for club-managed auctions
    that have not yet been invoiced. Bidder-number collisions are skipped per-row
    (warning logged) rather than letting a unique-constraint violation crash the save.
    """
    if created:
        return
    from .models import AuctionTOS

    prev_bidder = getattr(instance, "_previous_bidder_number", None)
    prev_bidding = getattr(instance, "_previous_bidding_allowed", None)
    prev_selling = getattr(instance, "_previous_selling_allowed", None)

    shadows = AuctionTOS.objects.filter(
        clubmember=instance,
        auction__manage_users_through_club__in=["all", "checkin"],
        auction__invoiced=False,
    )

    if prev_bidding is not None and prev_bidding != instance.bidding_allowed:
        shadows.update(bidding_allowed=instance.bidding_allowed)
    if prev_selling is not None and prev_selling != instance.selling_allowed:
        shadows.update(selling_allowed=instance.selling_allowed)
    if prev_bidder is not None and prev_bidder != instance.bidder_number and instance.bidder_number:
        for shadow in shadows:
            collision = (
                AuctionTOS.objects.filter(auction_id=shadow.auction_id, bidder_number=instance.bidder_number)
                .exclude(pk=shadow.pk)
                .exists()
            )
            if collision:
                logging.getLogger(__name__).warning(
                    "Skipped bidder_number sync for AuctionTOS pk=%s: '%s' already taken in auction pk=%s",
                    shadow.pk,
                    instance.bidder_number,
                    shadow.auction_id,
                )
                continue
            AuctionTOS.objects.filter(pk=shadow.pk).update(bidder_number=instance.bidder_number)


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


@receiver(user_logged_in)
def user_logged_in_callback(sender, user, request, **kwargs):
    """When a user signs in, link unattached AuctionTOS and ClubMember records to their account."""
    from auctions.models import AuctionTOS

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
            canonical.merge_duplicate(duplicate, reason="duplicate detected on login")
        else:
            auctiontos.user = user
            auctiontos.save()

    from auctions.models import ClubMember

    # Bulk update — no ClubHistory here because this is an automatic system action on login
    # and there is no meaningful "who did this" actor to record.
    ClubMember.objects.filter(user__isnull=True, email=user.email, is_deleted=False).update(user=user)


@receiver(post_save, sender=User)
def create_user_userdata(sender, instance, created, **kwargs):
    if created:
        from auctions.models import UserData

        UserData.objects.create(user=instance)


@receiver(post_save, sender="auctions.Club")
def ensure_google_wallet_class(sender, instance, created, **kwargs):
    """Create the Google Wallet GenericClass once per club.

    The class is essentially a static template (per-pass visuals live on the
    GenericObject, not here), so we only need to create it the first time —
    icon / name changes are handled by refresh_google_wallet_objects_for_club.
    Dispatched via transaction.on_commit so a rolled-back Club.save() doesn't leak
    a task that then tries to create a Wallet class for a nonexistent club.
    """
    if instance.google_wallet_class_created:
        return
    from .tasks import create_google_wallet_class_for_club

    transaction.on_commit(lambda: create_google_wallet_class_for_club.delay(instance.pk))


@receiver(post_save, sender="auctions.Club")
def refresh_google_wallet_objects_for_club(sender, instance, created, **kwargs):
    """Patch all member wallet objects when the club's name or icon changes.

    Logo and background color are GenericObject fields (per-pass), not class
    fields, so a club-level visual change requires PATCHing every active member.
    """
    if created:
        return
    prev_name = getattr(instance, "_previous_name", "")
    prev_icon = getattr(instance, "_previous_icon_name", "")
    current_icon = instance.icon.name if instance.icon else ""
    if prev_name == instance.name and prev_icon == current_icon:
        return
    from .tasks import update_google_wallet_objects_for_club

    transaction.on_commit(lambda: update_google_wallet_objects_for_club.delay(instance.pk))


@receiver(bounce_received)
def bounce_handler(sender, mail_obj, bounce_obj, raw_message, *args, **kwargs):
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    from auctions.models import AuctionTOS, ClubMember

    AuctionTOS.objects.filter(email=email).update(email_address_status="BAD")
    # Bulk update — no ClubHistory here because this is an automatic SES bounce notification;
    # the "actor" is the email provider, not a club admin.
    ClubMember.objects.filter(email=email, is_deleted=False).update(email_address_status="BAD")


@receiver(complaint_received)
def complaint_handler(sender, mail_obj, complaint_obj, raw_message, *args, **kwargs):
    from .models import ClubHistory, ClubMember

    recipient_list = mail_obj["destination"]
    email = recipient_list[0]

    user = User.objects.filter(email=email).first()
    if user:
        user.userdata.unsubscribe_from_all

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
