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
  club — it loses the account link and nothing else (``ClubMember.admin_edited``), including its
  place on the club's mailing list: the club collected that address and is the one who answers for
  it. A row that exists only because the member signed themselves up and no admin ever touched it is
  theirs, and goes — along with the Mailchimp/Brevo contact it created.
* **The auction's own notes stay.** An AuctionTOS an admin typed in at the door
  (``manually_added``) is the auction's record of who was there, written from what the person said
  in person, so it keeps its contents and only loses the account link. One the person created by
  joining the auction themselves is theirs, and its name and contact details go.
* **Everything personal actually goes**: the site profile and its address/coordinates, devices and
  their push tokens, browsing and search history, watched lots, saved payment-processor connections,
  and the sign-in identities (password, email address records, linked Google account, app tokens).

Club and auction history record what deletion did, so an admin reading the roster later can see why
a record lost its name. Those histories are free text and sometimes quote an email address, so the
person's addresses are rewritten to ``[deleted]`` in the history of every auction they took part in,
and of any club whose record of them was their own. Names are left alone — an auction's history has
to stay readable, and the account behind the name is gone either way.

The page says all of this in plain language before asking for confirmation — a deletion page that
quietly does less than it claims is the one thing Apple actually rejects.
"""

import logging
import re

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

# Written over an email address quoted in free-text history that the auction or club keeps.
REDACTED_EMAIL = "[deleted]"


def deletion_due_date(userdata):
    """When *userdata*'s pending deletion runs, or None if there isn't one."""
    if not userdata or not userdata.account_deletion_requested:
        return None
    return userdata.account_deletion_requested + timezone.timedelta(days=GRACE_PERIOD_DAYS)


def _blacklist_refresh_tokens(user):
    """Retire the app's long-lived tokens, so a signed-in phone stops being signed in."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    except ImportError:  # pragma: no cover - blacklist app is installed in this project
        return
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def request_deletion(user):
    """Schedule *user*'s account for deletion and return the date it will happen.

    Idempotent-ish: asking twice doesn't restart the clock, so nobody can extend their own grace
    period by clicking again.

    The web session ends in the view; the app's refresh tokens end here, because they outlive it by
    months. Without that, someone who deletes from a desktop keeps a working app for the whole grace
    period, never meets the one thing that calls the deletion off (signing in — using the app isn't
    signing in), and then finds the account gone. Signing in again mints new tokens.
    """
    userdata = user.userdata
    if not userdata.account_deletion_requested:
        userdata.account_deletion_requested = timezone.now()
        userdata.save(update_fields=["account_deletion_requested"])
        _blacklist_refresh_tokens(user)
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

    Club memberships and auction records are each split the way deletion treats them: the ones the
    club or auction keeps, and the ones that go with the account.
    """
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
        # Responsibilities other people depend on. These aren't deleted — an auction outlives its
        # organizer's account — but someone who is the only admin should hear it before confirming.
        "auctions_created": Auction.objects.filter(created_by=user, is_deleted=False).count(),
        "clubs_administered": memberships.filter(permission_admin=True).count(),
    }


def _marketing_contacts(user):
    """(club_pk, email) for every mailing list this deletion removes the person from.

    Only the lists that came from the person's own member records. ``admin_edited`` draws the same
    line here as it does everywhere else: if the club owns the member record, it owns that record's
    place on the club's mailing list too — the club collected the address and is the one who answers
    for it, so deletion leaves the contact alone and the page tells the person to ask the club.
    """
    from auctions.models import ClubMember

    contacts = []
    for member in ClubMember.objects.filter(user=user, admin_edited=False).select_related("club"):
        if member.email and (member.club.mailchimp_connected or member.club.brevo_connected):
            contacts.append((member.club_id, member.email))
    return contacts


def _personal_emails(user):
    """Every address this person's records are keyed on — collected before anything is blanked."""
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

    SocialToken.objects.filter(account__user=user).delete()
    SocialAccount.objects.filter(user=user).delete()
    EmailAddress.objects.filter(user=user).delete()
    # Already done when the deletion was requested; repeated here because a token can be issued
    # between the two (signing in cancels the deletion, so that's a cancel-then-ask-again).
    _blacklist_refresh_tokens(user)


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

    # Push subscriptions: the app's FCM tokens and any browser subscription (the endpoint and its
    # keys are the browser's address for this person, so the SubscriptionInfo goes too).
    subscription_pks = list(PushInformation.objects.filter(user=user).values_list("subscription_id", flat=True))
    PushInformation.objects.filter(user=user).delete()
    SubscriptionInfo.objects.filter(pk__in=subscription_pks).delete()
    MobileDevice.objects.filter(user=user).delete()
    MobileOfflineOp.objects.filter(user=user).delete()
    # The log of what was pushed to those devices, and which prompts they'd already been shown.
    PushNotificationSent.objects.filter(user=user).delete()
    CheckinNudge.objects.filter(user=user).delete()
    # Their own Bluetooth hardware, reported by the app when they paired it. The printer profiles it
    # taught us are already ThermalPrinterProfile rows and aren't about this person at all.
    ObservedPrinter.objects.filter(user=user).delete()
    # Camera sightings from their phone, keyed to its AR session. The lot map is solved out of these
    # into Lot positions, and the buffer is pruned constantly, so nothing depends on keeping them.
    LotObservation.objects.filter(user=user).delete()

    # Payment-processor connections (access tokens, merchant ids).
    PayPalSeller.objects.filter(user=user).delete()
    SquareSeller.objects.filter(user=user).delete()

    # Preferences, interests and history — all of it is a profile of one person.
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
    # Who this person refused to sell to is their own list and goes with them. Bans *of* them stay:
    # that list belongs to the person who wrote it.
    UserBan.objects.filter(user=user).delete()
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
    roster, their dues, their bidder number, their mailing list), so it keeps its contents and only
    stops pointing at the account. A record that exists because the member signed themselves up, and
    that no admin has touched since, is the member's and is emptied and deactivated.
    """
    from auctions.models import ClubHistory, ClubMember

    for member in ClubMember.objects.filter(user=user).select_related("club"):
        # Written with queryset updates rather than member.save(): ClubMember.save() re-links a
        # user-less record to whichever account matches its email, which is what keeps club rosters
        # attached to their members and would immediately undo the unlink here. It also fires the
        # mailing-list sync, which has nothing to do here either way — a kept record keeps its
        # contact, and a member's own record has its contact deleted outright by delete_account.
        if member.admin_edited:
            # contact_status is deliberately not touched. Marking do-not-contact would archive the
            # club's Mailchimp contact and delete its Brevo one on the next sync, which is exactly
            # the club-owned data this branch exists to leave alone.
            ClubMember.objects.filter(pk=member.pk).update(user=None)
            # member.name, not str(member), which falls back to the email address — this line is
            # kept forever and must not be the one place the address survives.
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

    An AuctionTOS keeps its bidder number, its pickup location and everything the invoice is built
    from — the auction's totals have to keep adding up, and the seller of a lot this person bought
    still needs their own history to make sense.

    ``manually_added`` decides the rest. A row an admin typed in at the door is the auction's own
    note of who was there, taken down from what the person said in person, so it keeps its contents
    and only loses the account link. A row that exists because the person joined the auction
    themselves is theirs: name, email, phone and address all go.
    """
    from auctions.models import AuctionHistory, AuctionTOS, Lot

    # Queryset updates for the same reason as the club records: AuctionTOS.save() re-attaches a
    # row to the account matching its email, and its side effects (invoice recalculation, welcome
    # mail, duplicate merging) have no business running for someone who is leaving.
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


def _redact_emails_from_history(emails, auction_pks, member_owned_club_pks):
    """Rewrite the person's addresses to ``[deleted]`` in the histories that are kept.

    Auction and club history are prose an admin reads back later ("changed email from a@b.com to
    c@d.com"), so an address outlives every structured field that held it. Names are left alone —
    the history has to stay readable, and the account behind the name is gone anyway.

    Scoped to the auctions the person took part in and the clubs whose record of them was their own.
    Scoping the auctions is what keeps this off ``AuctionHistory`` as a whole: ``action__icontains``
    is a leading-wildcard LIKE that no index can help, and this runs for every account in the daily
    batch. An address only ever reaches an auction's history through the person's own participation
    in it, so that is the same set of rows.

    Club history is left alone where the club kept its record: it kept the address in that record,
    and blanking the changelog would leave the club's own history disagreeing with its own roster.
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
    resolving. Safe to call twice — every step is idempotent.
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
    # No is_active filter: _scrub_profile clears account_deletion_requested, and that is what stops
    # an account being processed twice. Filtering on is_active as well would silently strand the
    # request of anyone an admin had deactivated for some other reason in between.
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
            # One account's club integration failing must not stall everyone else's deletion.
            logger.exception("Failed to delete account for user %s", user.pk)
    return count
