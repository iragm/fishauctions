"""Account deletion -- what "delete my account" means here, and the machinery that does it.

Required by App Store Review Guideline 5.1.1(v) and Google Play's data-deletion policy; served by
:class:`auctions.views.AccountDeleteView` at /preferences/. Two steps: :func:`request_deletion`
starts a :data:`GRACE_PERIOD_DAYS` window that a plain sign-in cancels (:func:`cancel_deletion`);
:func:`delete_account` runs once the grace period expires (daily via Celery beat).

Not a ``User.delete()``: other people's records (bids, invoices, sold lots, club/auction history)
must keep adding up, so the User row survives with everything personal stripped and un-signable-in,
while rows that point at it keep pointing at it. A club- or auction-owned record (admin-created or
admin-edited) keeps its contents and only loses the account link; a self-created one is deleted with
the account. Everything genuinely personal (profile, devices, history, sign-in identities, a linked
Apple grant) is deleted outright.
"""

import logging
import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Long enough to undo an accidental click, short enough to be a real deletion.
GRACE_PERIOD_DAYS = 30

# Written over the name on records kept for their auction/club, so a blank isn't mistaken for a bug.
DELETED_NAME = "Deleted user"

# Written over an email address quoted in free-text history that the auction or club keeps.
REDACTED_EMAIL = "[deleted]"


def deletion_due_date(userdata):
    """When *userdata*'s pending deletion runs, or None if there isn't one."""
    if not userdata or not userdata.account_deletion_requested:
        return None
    return userdata.account_deletion_requested + timezone.timedelta(days=GRACE_PERIOD_DAYS)


def blacklist_refresh_tokens(user):
    """Retire the app's long-lived tokens, so a signed-in phone stops being signed in."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    except ImportError:  # pragma: no cover - blacklist app is installed in this project
        return
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def request_deletion(user):
    """Schedule *user*'s account for deletion and return the date it will happen.

    Idempotent: asking twice doesn't restart the clock. App refresh tokens are blacklisted here
    (they outlive the web session by months) so a desktop-initiated deletion doesn't leave a
    working app for the whole grace period. Signing in again cancels the deletion and mints new ones.
    """
    userdata = user.userdata
    if not userdata.account_deletion_requested:
        userdata.account_deletion_requested = timezone.now()
        userdata.save(update_fields=["account_deletion_requested"])
        blacklist_refresh_tokens(user)
        logger.info("Account deletion requested for user %s", user.pk)
    return deletion_due_date(userdata)


def cancel_deletion(user):
    """Cancel a pending deletion. Returns True if there was one."""
    userdata = getattr(user, "userdata", None)
    if not userdata or not userdata.account_deletion_requested:
        return False
    userdata.account_deletion_requested = None
    userdata.save(update_fields=["account_deletion_requested"])
    logger.info("Account deletion cancelled for user %s", user.pk)
    return True


def deletion_summary(user):
    """Counts for the confirmation page, split the way deletion treats them: kept vs. deleted."""
    from auctions.models import Auction, AuctionTOS, ClubMember, Lot, MobileDevice

    memberships = ClubMember.objects.filter(user=user, is_deleted=False)
    auction_records = AuctionTOS.objects.filter(user=user)
    return {
        "auctions": auction_records.count(),
        "auctions_added_by_admins": auction_records.filter(manually_added=True).count(),
        "lots": Lot.objects.filter(user=user, is_deleted=False).count(),
        "club_memberships_kept": memberships.filter(admin_edited=True).count(),
        "club_memberships_deleted": memberships.filter(admin_edited=False).count(),
        "devices": MobileDevice.objects.filter(user=user).count(),
        # Not deleted, but a sole admin should hear about it before confirming.
        "auctions_created": Auction.objects.filter(created_by=user, is_deleted=False).count(),
        "clubs_administered": memberships.filter(permission_admin=True).count(),
    }


def _marketing_contacts(user):
    """(club_pk, email) for every mailing list this deletion removes the person from.

    Only from the person's own (non ``admin_edited``) member records -- a club-owned record keeps
    its place on the club's list, since the club collected that address.
    """
    from auctions.models import ClubMember

    contacts = []
    for member in ClubMember.objects.filter(user=user, admin_edited=False).select_related("club"):
        if member.email and (member.club.mailchimp_connected or member.club.brevo_connected):
            contacts.append((member.club_id, member.email))
    return contacts


def _personal_emails(user):
    """Every address this person's records are keyed on -- collected before anything is blanked."""
    from allauth.account.models import EmailAddress

    from auctions.models import AuctionTOS, ClubMember

    emails = {user.email} if user.email else set()
    for queryset in (
        EmailAddress.objects.filter(user=user).values_list("email", flat=True),
        AuctionTOS.objects.filter(user=user).values_list("email", flat=True),
        ClubMember.objects.filter(user=user).values_list("email", flat=True),
    ):
        emails.update(email for email in queryset if email)
    return emails


def _delete_sign_in_identities(user):
    """Drop every way back into this account: password, email records, social logins, JWTs."""
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount, SocialToken

    from auctions.apple_signin import revoke_all_for_user

    # Apple requires the grant revoked when the account goes. Must happen before the token rows
    # below are dropped -- they're the only way to reach Apple. Best effort: Apple being
    # unreachable must not block the deletion.
    try:
        revoke_all_for_user(user)
    except Exception:
        logger.exception("Failed to revoke Apple sign-in grants for user %s", user.pk)

    SocialToken.objects.filter(account__user=user).delete()
    SocialAccount.objects.filter(user=user).delete()
    EmailAddress.objects.filter(user=user).delete()
    # Repeated here (also done on request): a token can be issued between the two, since signing
    # in cancels the deletion.
    blacklist_refresh_tokens(user)


def _delete_personal_rows(user):
    """Rows that are only ever about this person and nobody else's record."""
    from webpush.models import PushInformation, SubscriptionInfo

    from auctions.models import (
        AdCampaignResponse,
        AuctionCampaign,
        AuctionIgnore,
        ChatSubscription,
        CheckinNudge,
        CommandPaletteSearch,
        ContentReport,
        CopyrightNotice,
        LotObservation,
        MobileDevice,
        MobileOfflineOp,
        ObservedPrinter,
        PayPalSeller,
        PushNotificationSent,
        SearchHistory,
        SquareSeller,
        UserBan,
        UserIgnoreCategory,
        UserInterestCategory,
        UserLabelPrefs,
        Watch,
    )

    # Push subscriptions: FCM tokens and any browser subscription (its endpoint/keys are the
    # browser's address for this person).
    subscription_pks = list(PushInformation.objects.filter(user=user).values_list("subscription_id", flat=True))
    PushInformation.objects.filter(user=user).delete()
    SubscriptionInfo.objects.filter(pk__in=subscription_pks).delete()
    MobileDevice.objects.filter(user=user).delete()
    MobileOfflineOp.objects.filter(user=user).delete()
    PushNotificationSent.objects.filter(user=user).delete()
    CheckinNudge.objects.filter(user=user).delete()
    # Their own Bluetooth hardware; the printer profiles it taught us are separate rows, not this
    # person's data.
    ObservedPrinter.objects.filter(user=user).delete()
    # Camera sightings from their phone; the lot map is solved out of these and the buffer is
    # pruned constantly, so nothing depends on keeping them.
    LotObservation.objects.filter(user=user).delete()

    PayPalSeller.objects.filter(user=user).delete()
    SquareSeller.objects.filter(user=user).delete()

    Watch.objects.filter(user=user).delete()
    ChatSubscription.objects.filter(user=user).delete()
    SearchHistory.objects.filter(user=user).delete()
    CommandPaletteSearch.objects.filter(user=user).delete()
    UserInterestCategory.objects.filter(user=user).delete()
    UserIgnoreCategory.objects.filter(user=user).delete()
    AuctionIgnore.objects.filter(user=user).delete()
    # Promo-email campaigns carry the address they were sent to, so they go rather than unlink.
    AuctionCampaign.objects.filter(user=user).delete()
    UserLabelPrefs.objects.filter(user=user).delete()
    # Who this person refused to sell to is their own list. Bans *of* them stay: that list belongs
    # to whoever wrote it.
    UserBan.objects.filter(user=user).delete()
    # An ad response is the campaign owner's statistic; keep the row, lose the person.
    AdCampaignResponse.objects.filter(user=user).update(user=None, session="")
    # A content report and a copyright notice are someone else's record of a decision to be made;
    # unlink, don't delete. Copyright strikes *against* them stay untouched -- this site's own
    # repeat-infringer record, required by 17 U.S.C. 512(i).
    ContentReport.objects.filter(reported_by=user).update(reported_by=None, reporter_email="")
    CopyrightNotice.objects.filter(submitted_by=user).update(submitted_by=None)


def _anonymize_page_views(user):
    """Keep the counts an auction's stats are built on; drop who and from where."""
    from auctions.models import PageView

    PageView.objects.filter(user=user).update(
        user=None, ip_address=None, session_id=None, user_agent=None, latitude=0, longitude=0
    )


def _anonymize_club_memberships(user):
    """Unlink every membership; scrub the ones the club doesn't own.

    ``admin_edited`` is the line: a club-owned record keeps its contents (roster, dues, bidder
    number, mailing list) and only stops pointing at the account; a self-signed-up, untouched
    record is emptied and deactivated.
    """
    from auctions.models import ClubHistory, ClubMember

    for member in ClubMember.objects.filter(user=user).select_related("club"):
        # Queryset update, not member.save(): save() re-links a user-less record to whichever
        # account matches its email, and fires the mailing-list sync -- neither belongs here.
        if member.admin_edited:
            # contact_status untouched: do-not-contact would archive/delete the club's mailing
            # list contact, which is exactly the club-owned data this branch leaves alone.
            ClubMember.objects.filter(pk=member.pk).update(user=None)
            # member.name, not str(member) (which falls back to email) -- this line is kept forever.
            who = member.name or f"Member #{member.pk}"
            action = f"{who} deleted their site account; the club's member record was kept"
        else:
            ClubMember.objects.filter(pk=member.pk).update(
                user=None,
                name=DELETED_NAME,
                email=None,
                phone_number=None,
                address="",
                memo="",
                discord_id=None,
                discord_username=None,
                is_deleted=True,
            )
            action = "A member who signed themselves up deleted their site account and their member record"
        ClubHistory.objects.create(club=member.club, user=None, action=action, applies_to="MEMBERS")


def _anonymize_auction_records(user):
    """Auctions keep their books; the person's identity comes off them.

    ``manually_added`` decides how much: an admin-typed record keeps its contents and only loses
    the account link; a self-joined record has name/email/phone/address removed too. Bidder number
    and invoice amounts are kept either way.
    """
    from auctions.models import AuctionHistory, AuctionTOS, Lot

    # Queryset updates: AuctionTOS.save() re-attaches by email and triggers invoice recalculation,
    # welcome mail and duplicate merging -- none of which should run for someone leaving.
    for tos in AuctionTOS.objects.filter(user=user):
        who = f"Bidder {tos.bidder_number}" if tos.bidder_number else f"Participant #{tos.pk}"
        if tos.manually_added:
            AuctionTOS.objects.filter(pk=tos.pk).update(user=None)
            action = (
                f"{who} deleted their site account.  An admin added this record, so it was kept "
                "as-is and only the link to the account was removed."
            )
        else:
            AuctionTOS.objects.filter(pk=tos.pk).update(
                user=None, name=DELETED_NAME, email=None, phone_number=None, address=None
            )
            action = (
                f"{who} deleted their site account.  Their name and contact details were removed; "
                "the bidder number and every invoice amount were kept."
            )
        AuctionHistory.objects.create(auction_id=tos.auction_id, user=None, action=action, applies_to="USERS")

    # Lots stay (part of an auction's results); a standalone lot has nobody left to sell it, so
    # it's deactivated rather than left listed.
    standalone_pks = list(
        Lot.objects.filter(user=user, auction__isnull=True, is_deleted=False).values_list("pk", flat=True)
    )
    Lot.objects.filter(user=user).update(user=None)
    Lot.objects.filter(winner=user).update(winner=None)
    if standalone_pks:
        Lot.objects.filter(pk__in=standalone_pks).update(deactivated=True)


def _redact_emails_from_history(emails, auction_pks, member_owned_club_pks):
    """Rewrite the person's addresses to ``[deleted]`` in the histories that are kept.

    Names are left alone; only addresses, since the history has to stay readable. Scoped to the
    auctions this person took part in and clubs whose record of them was their own -- an unscoped
    ``action__icontains`` would be a leading-wildcard LIKE with no index, over every account in the
    daily batch. Club history that the club kept is left alone entirely: the club's own changelog
    should agree with its own roster.
    """
    from auctions.models import AuctionHistory, ClubHistory

    for model, queryset in (
        (AuctionHistory, AuctionHistory.objects.filter(auction_id__in=auction_pks)),
        (ClubHistory, ClubHistory.objects.filter(club_id__in=member_owned_club_pks)),
    ):
        max_length = model._meta.get_field("action").max_length
        for email in emails:
            pattern = re.compile(re.escape(email), re.IGNORECASE)
            for pk, action in queryset.filter(action__icontains=email).values_list("pk", "action"):
                model.objects.filter(pk=pk).update(action=pattern.sub(REDACTED_EMAIL, action)[:max_length])


def _scrub_profile(user):
    """Empty the account itself. Leaves an inactive shell so other people's rows still resolve."""
    userdata = user.userdata
    userdata.phone_number = None
    userdata.address = None
    userdata.location_coordinates = None
    userdata.latitude = 0
    userdata.longitude = 0
    userdata.last_ip_address = None
    userdata.timezone = None
    userdata.paypal_email_address = None
    userdata.preferred_bidder_number = ""
    userdata.email_visible = False
    userdata.username_visible = False
    userdata.has_unsubscribed = True
    userdata.push_notifications_instead_of_email = False
    userdata.push_notifications_when_lots_sell = False
    userdata.account_deletion_requested = None
    userdata.save()

    user.username = f"deleted-user-{user.pk}"
    user.first_name = ""
    user.last_name = ""
    user.email = ""
    user.is_active = False
    user.is_staff = False
    user.is_superuser = False
    user.set_unusable_password()
    user.save()


def delete_account(user):
    """Delete *user*'s personal data for good. Not reversible; see the module docstring.

    Returns the (now anonymous) User row, which stays so that bids, invoices and sold lots keep
    resolving. Safe to call twice -- every step is idempotent.
    """
    from auctions.models import AuctionTOS, ClubMember
    from auctions.tasks import delete_marketing_contact

    # Everything that reads the person's own records has to happen before anything blanks them.
    contacts = _marketing_contacts(user)
    emails = _personal_emails(user)
    auction_pks = list(AuctionTOS.objects.filter(user=user).values_list("auction_id", flat=True))
    member_owned_club_pks = list(
        ClubMember.objects.filter(user=user, admin_edited=False).values_list("club_id", flat=True)
    )
    with transaction.atomic():
        _delete_sign_in_identities(user)
        _delete_personal_rows(user)
        _anonymize_page_views(user)
        _anonymize_club_memberships(user)
        _anonymize_auction_records(user)
        # After the two above, so it also covers the history lines they just wrote.
        _redact_emails_from_history(emails, auction_pks, member_owned_club_pks)
        _scrub_profile(user)
    # Marketing lists are someone else's API and can fail; keep them out of the transaction.
    for club_pk, email in contacts:
        transaction.on_commit(lambda club_pk=club_pk, email=email: delete_marketing_contact.delay(club_pk, email))
    logger.info("Account deleted for user %s", user.pk)
    return user


def process_due_deletions(now=None):
    """Run every deletion whose grace period has expired. Returns how many ran."""
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=GRACE_PERIOD_DAYS)
    user_model = get_user_model()
    # No is_active filter: _scrub_profile clears account_deletion_requested, which is what stops
    # reprocessing -- filtering on is_active too would strand anyone deactivated for other reasons.
    due = user_model.objects.filter(
        userdata__account_deletion_requested__isnull=False,
        userdata__account_deletion_requested__lte=cutoff,
    )
    count = 0
    for user in due:
        try:
            delete_account(user)
            count += 1
        except Exception:
            logger.exception("Failed to delete account for user %s", user.pk)
    return count
