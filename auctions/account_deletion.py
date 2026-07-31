"""Account deletion — what "delete my account" means here, and the machinery that does it.

Any app that lets people create an account has to let them delete it from inside the app (App Store
Review Guideline 5.1.1(v); Google Play's data-deletion policy asks for the same and accepts a web
URL). That's :class:`auctions.views.AccountDeleteView` — one web page, reachable from /preferences/,
so the app follows a link like any other page and needs no release of its own.

Deletion happens in two steps:

1. :func:`request_deletion` records the request and signs the user out. Nothing is destroyed yet.
   The account keeps working for :data:`GRACE_PERIOD_DAYS`, and simply signing in again cancels it
   (:func:`cancel_deletion`, wired to the login signal in ``auctions.signals``) — the alternative is
   an irreversible mistake at 2am.
2. :func:`delete_account` runs when the grace period is up (the ``delete_pending_accounts`` command,
   daily via Celery beat) and is the irreversible part.

What deletion means, and why it isn't a ``User.delete()``:

* **Other people's records stay.** A bid, an invoice, a sold lot and a payout are also the seller's,
  the buyer's and the club's records; a club's past auction has to keep adding up after someone
  leaves. So the User row survives, stripped of everything personal (username, email, password, name,
  and it can never be signed into again), and the rows that point at it keep pointing at it.
* **The club's own records stay.** A ClubMember row an admin created or has edited belongs to the
  club — it loses the account link and nothing else (``ClubMember.admin_edited``). A row that exists
  only because the member signed themselves up and no admin ever touched it is theirs, and goes.
* **Everything personal actually goes**: the site profile and its address/coordinates, devices and
  their push tokens, browsing history, watched lots, saved payment-processor connections, the
  sign-in identities (password, email address records, linked Google account), and the marketing
  contacts held by clubs' Mailchimp/Brevo accounts.

The page says all of this in plain language before asking for confirmation — a deletion page that
quietly does less than it claims is the one thing Apple actually rejects.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# How long a deletion request can be undone by signing in again. Long enough to cover "I meant to do
# that but my club needs one more thing from me", short enough to be a real deletion.
GRACE_PERIOD_DAYS = 30

# Written over the name on records that are kept for their auction/club, so a person reading them
# sees why there's no name rather than a suspicious blank.
DELETED_NAME = "Deleted user"


def deletion_due_date(userdata):
    """When *userdata*'s pending deletion runs, or None if there isn't one."""
    if not userdata or not userdata.account_deletion_requested:
        return None
    return userdata.account_deletion_requested + timezone.timedelta(days=GRACE_PERIOD_DAYS)


def request_deletion(user):
    """Schedule *user*'s account for deletion and return the date it will happen.

    Idempotent-ish: asking twice doesn't restart the clock, so nobody can extend their own grace
    period by clicking again.
    """
    userdata = user.userdata
    if not userdata.account_deletion_requested:
        userdata.account_deletion_requested = timezone.now()
        userdata.save(update_fields=["account_deletion_requested"])
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
    """Counts for the confirmation page, so the warning is about *this* account.

    Club memberships are split the way deletion treats them: the ones the club keeps (an admin made
    or edited them) and the ones that go with the account.
    """
    from auctions.models import Auction, AuctionTOS, ClubMember, Lot, MobileDevice

    memberships = ClubMember.objects.filter(user=user, is_deleted=False)
    return {
        "auctions": AuctionTOS.objects.filter(user=user).count(),
        "lots": Lot.objects.filter(user=user, is_deleted=False).count(),
        "club_memberships_kept": memberships.filter(admin_edited=True).count(),
        "club_memberships_deleted": memberships.filter(admin_edited=False).count(),
        "devices": MobileDevice.objects.filter(user=user).count(),
        # Responsibilities other people depend on. These aren't deleted — an auction outlives its
        # organizer's account — but someone who is the only admin should hear it before confirming.
        "auctions_created": Auction.objects.filter(created_by=user, is_deleted=False).count(),
        "clubs_administered": memberships.filter(permission_admin=True).count(),
    }


def _marketing_contacts(user):
    """(club_pk, email) for every list a club holds this person on — collected before scrubbing."""
    from auctions.models import ClubMember

    contacts = []
    for member in ClubMember.objects.filter(user=user).select_related("club"):
        if member.email and (member.club.mailchimp_connected or member.club.brevo_connected):
            contacts.append((member.club_id, member.email))
    return contacts


def _delete_sign_in_identities(user):
    """Drop every way back into this account: password, email records, social logins, JWTs."""
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount, SocialToken

    SocialToken.objects.filter(account__user=user).delete()
    SocialAccount.objects.filter(user=user).delete()
    EmailAddress.objects.filter(user=user).delete()

    # The app holds a refresh token that outlives the session; blacklist it so a phone that never
    # opened again can't refresh its way back in.
    try:
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)
    except ImportError:  # pragma: no cover - blacklist app is installed in this project
        pass


def _delete_personal_rows(user):
    """Rows that are only ever about this person and nobody else's record."""
    from webpush.models import PushInformation, SubscriptionInfo

    from auctions.models import (
        AdCampaignResponse,
        AuctionCampaign,
        AuctionIgnore,
        ChatSubscription,
        MobileDevice,
        MobileOfflineOp,
        PayPalSeller,
        SearchHistory,
        SquareSeller,
        UserIgnoreCategory,
        UserInterestCategory,
        UserLabelPrefs,
        Watch,
    )

    # Push subscriptions: the app's FCM tokens and any browser subscription (the endpoint and its
    # keys are the browser's address for this person, so the SubscriptionInfo goes too).
    subscription_pks = list(PushInformation.objects.filter(user=user).values_list("subscription_id", flat=True))
    PushInformation.objects.filter(user=user).delete()
    SubscriptionInfo.objects.filter(pk__in=subscription_pks).delete()
    MobileDevice.objects.filter(user=user).delete()
    MobileOfflineOp.objects.filter(user=user).delete()

    # Payment-processor connections (access tokens, merchant ids).
    PayPalSeller.objects.filter(user=user).delete()
    SquareSeller.objects.filter(user=user).delete()

    # Preferences, interests and history — all of it is a profile of one person.
    Watch.objects.filter(user=user).delete()
    ChatSubscription.objects.filter(user=user).delete()
    SearchHistory.objects.filter(user=user).delete()
    UserInterestCategory.objects.filter(user=user).delete()
    UserIgnoreCategory.objects.filter(user=user).delete()
    AuctionIgnore.objects.filter(user=user).delete()
    # Promo-email campaigns carry the address they were sent to, so they go rather than unlink.
    AuctionCampaign.objects.filter(user=user).delete()
    UserLabelPrefs.objects.filter(user=user).delete()
    # An ad response is the campaign owner's statistic; keep the row, lose the person.
    AdCampaignResponse.objects.filter(user=user).update(user=None, session="")


def _anonymize_page_views(user):
    """Keep the counts an auction's stats are built on; drop who and from where."""
    from auctions.models import PageView

    PageView.objects.filter(user=user).update(
        user=None, ip_address=None, session_id=None, user_agent=None, latitude=0, longitude=0
    )


def _anonymize_club_memberships(user):
    """Unlink every membership; scrub the ones the club doesn't own.

    ``admin_edited`` is the line: a record a club admin created or edited is the club's own (their
    roster, their dues, their bidder number), so it keeps its contents and only stops pointing at
    the account. A record that exists because the member signed themselves up, and that no admin has
    touched since, is the member's and is emptied and deactivated.
    """
    from auctions.models import ClubHistory, ClubMember

    for member in ClubMember.objects.filter(user=user).select_related("club"):
        # Written with queryset updates rather than member.save(): ClubMember.save() re-links a
        # user-less record to whichever account matches its email, which is what keeps club rosters
        # attached to their members and would immediately undo the unlink here. It also fires the
        # mailing-list sync, and this deletion removes those contacts itself (with the address, which
        # is about to be gone) instead of leaving an archived copy behind.
        if member.admin_edited:
            # Marked do-not-contact as well as unlinked: the club keeps the record, but the person
            # has asked to be deleted, and without this the next admin edit would sync them straight
            # back onto the mailing list we're about to remove them from.
            ClubMember.objects.filter(pk=member.pk).update(user=None, contact_status="do_not_contact")
            action = f"{member} deleted their site account; the club's member record was kept (do not contact)"
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

    An AuctionTOS keeps its bidder number, its pickup location and everything the invoice is built
    from — the auction's totals have to keep adding up, and the seller of a lot this person bought
    still needs their own history to make sense. What goes is the name, contact details and the
    account link.

    The exception is a club-managed auction where the club is keeping its member record: the club
    already holds those details in its roster (that's what ``admin_edited`` decided), so blanking
    them here would only leave the club's own auction records disagreeing with its member list.
    """
    from auctions.models import AuctionTOS, Lot

    # Queryset updates for the same reason as the club records: AuctionTOS.save() re-attaches a
    # row to the account matching its email, and its side effects (invoice recalculation, welcome
    # mail, duplicate merging) have no business running for someone who is leaving.
    for tos in AuctionTOS.objects.filter(user=user).select_related("clubmember"):
        keep_details = tos.clubmember is not None and tos.clubmember.admin_edited
        if keep_details:
            AuctionTOS.objects.filter(pk=tos.pk).update(user=None)
            continue
        AuctionTOS.objects.filter(pk=tos.pk).update(
            user=None, name=DELETED_NAME, email=None, phone_number=None, address=None
        )

    # Lots stay: they're part of an auction's results, and a buyer's invoice references them. The
    # seller is identified by the AuctionTOS record above, which is the auction's own copy.
    # A standalone lot is nobody else's record — it's a listing this person put up, and there's
    # no longer anyone to sell it, so take it off the site (noted before the user link goes).
    standalone_pks = list(
        Lot.objects.filter(user=user, auction__isnull=True, is_deleted=False).values_list("pk", flat=True)
    )
    Lot.objects.filter(user=user).update(user=None)
    Lot.objects.filter(winner=user).update(winner=None)
    if standalone_pks:
        Lot.objects.filter(pk__in=standalone_pks).update(deactivated=True)


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
    resolving. Safe to call twice — every step is idempotent.
    """
    from auctions.tasks import delete_marketing_contact

    contacts = _marketing_contacts(user)
    with transaction.atomic():
        _delete_sign_in_identities(user)
        _delete_personal_rows(user)
        _anonymize_page_views(user)
        _anonymize_club_memberships(user)
        _anonymize_auction_records(user)
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
    due = user_model.objects.filter(
        userdata__account_deletion_requested__isnull=False,
        userdata__account_deletion_requested__lte=cutoff,
        is_active=True,
    )
    count = 0
    for user in due:
        try:
            delete_account(user)
            count += 1
        except Exception:
            # One account's club integration failing must not stall everyone else's deletion.
            logger.exception("Failed to delete account for user %s", user.pk)
    return count
