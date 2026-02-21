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
    # else:
    # if instance.lot_submission_end_date > instance.date_start:
    # instance.lot_submission_end_date = instance.date_start
    if instance.lot_submission_start_date > instance.date_start:
        instance.lot_submission_start_date = instance.date_start
    # I don't see a problem submitting lots after the auction has started,
    # or any need to restrict when people add lots to an in-person auction
    # So I am not putting any new validation checks here
    # OK, the above comment was not correct, this caused confusion.  A couple checks have been added.
    # Admins can always override those, and they seem to be adding most of the lots for in person stuff anyway.
    # OK, third time's the charm, leave the lines above commented out

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
        # update the date end for all lots associated with this auction
        # note that we do NOT update the end time if there's a winner!
        # This means you cannot reopen an auction simply by changing the date end
        if instance.date_end:
            if instance.date_end + datetime.timedelta(minutes=60) < timezone.now():
                # if we are at least 60 minutes before the auction end
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
            # don't make the users set the pickup time seperately for simple auctions
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
        # Set initial email due dates
        instance.welcome_email_due = timezone.now() + datetime.timedelta(hours=WELCOME_EMAIL_DELAY_HOURS)
        if instance.is_online:
            if instance.date_end:
                instance.invoice_email_due = instance.date_end + datetime.timedelta(hours=INVOICE_EMAIL_DELAY_HOURS)
            if instance.date_end:
                instance.followup_email_due = instance.date_end + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)
        else:
            # For in-person auctions, skip invoice email and set followup based on date_start
            instance.invoice_email_sent = True  # Mark as sent so it won't be sent
            if instance.date_start:
                instance.followup_email_due = instance.date_start + datetime.timedelta(hours=FOLLOWUP_EMAIL_DELAY_HOURS)
    if not instance.is_online:
        # for in-person auctions, we need to add a single pickup location
        # and create it if the user was dumb enough to delete it
        try:
            from auctions.models import PickupLocation

            in_person_location, created = PickupLocation.objects.get_or_create(
                auction=instance,
                is_default=True,
                defaults={
                    "name": str(instance)[:50],
                    "pickup_time": instance.date_start,
                },
            )
        except Exception:
            pass
            # logger.warning("Somehow there's two pickup locations for this auction -- how is this possible?")


@receiver(pre_save, sender="auctions.UserData")
@receiver(pre_save, sender="auctions.PickupLocation")
@receiver(pre_save, sender="auctions.Club")
def update_user_location(sender, instance, **kwargs):
    """
    GeoDjango does not appear to support MySQL and Point objects well at the moment (2020)
    To get around this, I'm storing the coordinates in a raw latitude and longitude column

    The custom function distance_to is used to annotate queries
    """
    try:
        # if not instance.latitude and not instance.longitude:
        # some things to change here:
        # if sender has coords and they do not equal the instance coords, update instance lat/lng from sender
        # if sender has lat/lng and they do not equal the instance lat/lng, update instance coords
        cutLocation = instance.location_coordinates.split(",")
        instance.latitude = float(cutLocation[0])
        instance.longitude = float(cutLocation[1])
    except:
        pass


@receiver(pre_save, sender="auctions.Lot")
def update_lot_info(sender, instance, **kwargs):
    """
    Fill out the location and address from the user
    Fill out end date from the auction
    """
    if not instance.pk:
        # new lot?  set the default end date to the auction end
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

    # # create an invoice for this seller/winner
    # if instance.auction and instance.auctiontos_seller:
    #     invoice, created = Invoice.objects.get_or_create(
    #         auctiontos_user=instance.auctiontos_seller,
    #         auction=instance.auction,
    #         defaults={},
    #     )
    # if instance.auction and instance.auctiontos_winner:
    #     invoice, created = Invoice.objects.get_or_create(
    #         auctiontos_user=instance.auctiontos_winner,
    #         auction=instance.auction,
    #         defaults={},
    #     )
    if instance.auction and (not instance.reserve_price or instance.reserve_price < instance.auction.minimum_bid):
        instance.reserve_price = instance.auction.minimum_bid


@receiver(user_logged_in)
def user_logged_in_callback(sender, user, request, **kwargs):
    """When a user signs in, check for any AuctionTOS that have this users email but no user, and attach them to the user
    This allows people to view invoices, leave feedback, get contact information for sellers, etc.
    Important to have this be any user, not just new ones so that existing users can be signed up for in-person auctions

    After some thought, the user is also set in auctiontos.save
     -- but this is still important because people may add users who do not yet have an account
    """
    from auctions.models import AuctionTOS

    auctiontoss = AuctionTOS.objects.filter(user__isnull=True, email=user.email)
    for auctiontos in auctiontoss:
        existing = AuctionTOS.objects.filter(user=user, auction=auctiontos.auction).first()
        if existing:
            # Keep the oldest record as canonical
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


@receiver(post_save, sender=User)
def create_user_userdata(sender, instance, created, **kwargs):
    if created:
        from auctions.models import UserData

        UserData.objects.create(user=instance)


@receiver(bounce_received)
def bounce_handler(sender, mail_obj, bounce_obj, raw_message, *args, **kwargs):
    # you can then use the message ID and/or recipient_list(email address) to identify any problematic email messages that you have sent
    # message_id = mail_obj['messageId']
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    from auctions.models import AuctionTOS

    auctiontos = AuctionTOS.objects.filter(email=email)
    for tos in auctiontos:
        tos.email_address_status = "BAD"
        tos.save()


@receiver(complaint_received)
def complaint_handler(sender, mail_obj, complaint_obj, raw_message, *args, **kwargs):
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    user = User.objects.filter(email=email).first()
    if user:
        user.userdata.unsubscribe_from_all
