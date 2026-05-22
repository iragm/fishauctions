"""Signal handlers for the auctions app."""

import datetime
import logging

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
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
    """Auto-create the Google Wallet GenericClass for a new club.

    Only fires on create — slug renames must NOT re-trigger this, because Wallet
    class IDs are immutable and we key them off the (stable) club PK.
    """
    if not created:
        return
    from .tasks import create_google_wallet_class_for_club

    create_google_wallet_class_for_club.delay(instance.pk)


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

    members = ClubMember.objects.filter(email=email, is_deleted=False, contact_status="contact")
    for member in members:
        member.contact_status = "non_essential"
        member.save(update_fields=["contact_status"])
        ClubHistory.objects.create(
            club=member.club,
            user=None,
            action=f"{member} has requested to be unsubscribed",
            applies_to="MEMBERS",
        )


@receiver(post_save, sender="auctions.ClubMember")
def on_club_member_saved(sender, instance, **kwargs):
    """When a member gains permission_admin or permission_manage_auctions, auto-associate their auctions."""
    if instance.permission_admin or instance.permission_manage_auctions:
        _associate_auctions_for_member(instance)
