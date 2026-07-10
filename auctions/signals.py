"""Signal handlers for the auctions app."""

import datetime
import logging

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django_ses.signals import bounce_received, complaint_received

from .site_setup import ensure_single_club_membership_for_user

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
    auctions_to_update = list(Auction.objects.filter(created_by=member.user, club__isnull=True, is_deleted=False))
    for auction in auctions_to_update:
        Auction.objects.filter(pk=auction.pk).update(club=club)
        AuctionHistory.objects.create(
            auction=auction,
            user=None,
            action=f"Automatically associated with club '{club}' because {member.display_name} was granted a club permission.",
            applies_to="RULES",
        )
        # The bulk update above bypasses Auction.save(), so book the club ledger for this
        # auction's already-settled invoices the same way save() would on a fresh assignment.
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

    Currently tracks: show_member_barcode (revocation logic), icon name
    (re-push the Wallet class so new logos propagate), and club name
    (refresh member wallet object metadata on rename).
    """
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
    """When barcodes are disabled, expire all active Wallet passes.

    Apple Wallet does not have an equivalent push-revocation API without the
    full Web Service implementation, so we rely on the embedded `expirationDate`
    in the pkpass plus URL gating on re-download.
    """
    if created:
        return
    prev = getattr(instance, "_previous_show_member_barcode", None)
    current = instance.show_member_barcode
    if prev == current:
        return
    from .tasks import expire_google_wallet_objects_for_club

    if not current:
        transaction.on_commit(lambda: expire_google_wallet_objects_for_club.delay(instance.pk))


@receiver(pre_save, sender="auctions.ClubMember")
def stash_previous_clubmember_state(sender, instance, **kwargs):
    """Snapshot per-club auction-permission fields so post_save can detect changes
    and propagate them to linked shadow AuctionTOS records.

    Also snapshots wallet-relevant fields (name, membership_number,
    membership_expiration_date) so update_google_wallet_object_on_member_change
    can detect when a PATCH to the member's Wallet pass is needed.
    """
    if instance.pk:
        from .models import ClubMember

        prev = (
            ClubMember.objects.filter(pk=instance.pk)
            .values(
                "bidder_number",
                "bidding_allowed",
                "selling_allowed",
                "name",
                "membership_number",
                "membership_expiration_date",
                "address",
                "email",
            )
            .first()
            or {}
        )
        instance._previous_bidder_number = prev.get("bidder_number")
        instance._previous_bidding_allowed = prev.get("bidding_allowed")
        instance._previous_selling_allowed = prev.get("selling_allowed")
        instance._previous_name = prev.get("name") or ""
        instance._previous_membership_number = prev.get("membership_number")
        instance._previous_membership_expiration_date = prev.get("membership_expiration_date")
        instance._previous_address = prev.get("address") or ""
        instance._previous_email = prev.get("email") or ""
    else:
        instance._previous_bidder_number = None
        instance._previous_bidding_allowed = None
        instance._previous_selling_allowed = None
        instance._previous_name = ""
        instance._previous_membership_number = None
        instance._previous_membership_expiration_date = None
        instance._previous_address = ""
        instance._previous_email = ""


@receiver(post_save, sender="auctions.ClubMember")
def propagate_clubmember_to_shadow_tos(sender, instance, created, **kwargs):
    """When a ClubMember's bidder_number / bidding_allowed / selling_allowed change,
    push the new values to linked shadow AuctionTOS records for club-managed auctions
    that have not yet been invoiced. Bidder-number collisions are skipped per-row
    (warning logged) rather than letting a unique-constraint violation crash the save.

    When a new member is created, auto-create shadow TOS records in any active
    club-managed auctions that auto-add members ("all" or "checkin" mode).
    """
    from .models import Auction, AuctionTOS, PickupLocation

    if created:
        # Auto-create shadow TOS records in club-managed auctions for new members
        managed_auctions = Auction.objects.filter(
            club=instance.club,
            is_deleted=False,
            invoiced=False,
            manage_users_through_club__in=["all", "checkin"],
        )
        for auction in managed_auctions:
            default_location = PickupLocation.objects.filter(auction=auction).order_by("-is_default", "pk").first()
            if not default_location:
                continue
            already_exists = AuctionTOS.objects.filter(auction=auction, clubmember=instance).exists()
            if already_exists:
                continue
            if not instance.bidder_number:
                instance.generate_bidder_number(save=True)
            bidding = False if auction.manage_users_through_club == "checkin" else instance.bidding_allowed
            AuctionTOS.objects.create(
                user=instance.user,
                auction=auction,
                pickup_location=default_location,
                clubmember=instance,
                bidder_number=instance.bidder_number,
                bidding_allowed=bidding,
                selling_allowed=instance.selling_allowed,
                name=instance.name or "",
                email=instance.email or "",
                phone_number=instance.phone_number or "",
                address=instance.address or "",
                manually_added=True,
            )
        return

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


@receiver(post_save, sender="auctions.ClubMember")
def update_google_wallet_object_on_member_change(sender, instance, created, **kwargs):
    """When wallet-visible member fields change, PATCH the member's Wallet object.

    Watches: name, membership_number, membership_expiration_date.
    New members have no Wallet object yet (they haven't clicked "Add to Wallet"),
    so update_generic_object_for_member silently skips 404s — no harm done.
    """
    if created:
        return
    prev_name = getattr(instance, "_previous_name", "")
    prev_number = getattr(instance, "_previous_membership_number", None)
    prev_expiry = getattr(instance, "_previous_membership_expiration_date", None)
    current_expiry = instance.membership_expiration_date

    if (
        prev_name == (instance.name or "")
        and prev_number == instance.membership_number
        and prev_expiry == current_expiry
    ):
        return

    from .tasks import update_google_wallet_object_for_member

    transaction.on_commit(lambda: update_google_wallet_object_for_member.delay(instance.pk))


@receiver(post_save, sender="auctions.ClubMember")
def geocode_club_member_on_address_change(sender, instance, created, **kwargs):
    """Trigger geocoding when a ClubMember's address is new or has changed.

    Also triggers for new members with no address so the task can attempt
    the UserData coordinate fallback.
    """
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
    """Keep the member's Mailchimp contact in sync after any change.

    Email changes go through a dedicated task so the existing contact is moved instead of
    duplicated. No-op when the club has no Mailchimp connection.
    """
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
    """Move a user's club memberships to their new account email.

    Saving each member triggers the per-member Mailchimp email-change sync above. We never
    touch other clubs' records or memberships whose email differs from the old account email.
    """
    if created:
        return
    old_email = getattr(instance, "_previous_email", "") or ""
    new_email = instance.email or ""
    if not old_email or old_email == new_email:
        return
    from .models import ClubMember

    for member in ClubMember.objects.filter(user=instance, email__iexact=old_email, is_deleted=False):
        member.email = new_email
        member.save(update_fields=["email"])


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
    """Link any AuctionTOS rows that match this user's email but have no user FK yet.

    When the user already has a TOS in the same auction, the two are merged (oldest kept as
    canonical) via AuctionTOS.merge_duplicate; otherwise the orphan row is linked directly.
    Shared by the login signal (user_logged_in_callback) and the relink_auctiontos_users
    management command so both repair paths behave identically.
    """
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
            canonical.merge_duplicate(duplicate, reason=reason)
        else:
            auctiontos.user = user
            auctiontos.save()


@receiver(user_logged_in)
def user_logged_in_callback(sender, user, request, **kwargs):
    """When a user signs in, link unattached AuctionTOS and ClubMember records to their account."""
    link_unattached_tos_for_user(user)

    from auctions.models import ClubMember

    # Bulk update — no ClubHistory here because this is an automatic system action on login
    # and there is no meaningful "who did this" actor to record.
    ClubMember.objects.filter(user__isnull=True, email=user.email, is_deleted=False).update(user=user)
    ensure_single_club_membership_for_user(user)


@receiver(post_save, sender=User)
def create_user_userdata(sender, instance, created, **kwargs):
    if created:
        from auctions.models import UserData

        UserData.objects.create(user=instance)
    ensure_single_club_membership_for_user(instance)


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
def on_cloudflare_image_row_deleted(sender, instance, **kwargs):
    """Queue deletion of the Cloudflare copy of an image when its row is deleted.

    The task itself skips deletion if another row still references the same
    Cloudflare image (copied lots share images).  Local files are not deleted,
    matching how they were treated before Cloudflare Images was added.
    """
    from . import cloudflare_images

    if (
        instance.cloudflare_image_id
        and instance.cloudflare_image_id != cloudflare_images.UPLOAD_FAILED
        and cloudflare_images.enabled()
    ):
        from .tasks import delete_cloudflare_image

        delete_cloudflare_image.delay(instance.cloudflare_image_id)
