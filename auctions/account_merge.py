"""Merging two accounts one person owns: what moves, what is dropped, and the request that gates it.

Two-sided, served by :class:`auctions.views.AccountMergeView`: the account being closed asks
(:func:`request_merge`, by typing the other's username) and the account being kept accepts
(:func:`accept_merge`) within :data:`REQUEST_HOURS`. Being signed in to both is the proof of owning
both, so the kept account is never emailed and a stranger's request is a row nobody acts on.

:func:`merge_accounts` does the work, for the page and for ``empty_account_and_move_data`` alike.
Every relation to ``User`` is in exactly one of the tables below (``AccountMergeCoverageTests``), so a
new one fails a test until someone decides where it goes. Sign-ins with Google, Apple or Facebook
and AI agents move, which is the point; email addresses don't, and phones signed in to the closed
account are signed out.
"""

import logging

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long the kept account has to accept.
REQUEST_HOURS = 24

#: Moved by ``UserData.merge_into``, which also merges duplicates (one AuctionTOS per auction, one
#: ClubMember per club, one PayPal/Square connection).
MOVED_BY_MERGE_INTO = frozenset(
    {
        "auctions.Auction.created_by",
        "auctions.PickupLocation.user",
        "auctions.Invoice.buyer",
        "auctions.Lot.user",
        "auctions.Lot.winner",
        "auctions.Bid.user",
        "auctions.PageView.user",
        "auctions.AuctionCampaign.user",
        "auctions.SearchHistory.user",
        "auctions.AuctionIgnore.user",
        "auctions.UserIgnoreCategory.user",
        "auctions.Watch.user",
        "auctions.ChatSubscription.user",
        "auctions.UserInterestCategory.user",
        "auctions.AuctionTOS.user",
        "auctions.ClubMember.user",
        "auctions.PayPalSeller.user",
        "auctions.SquareSeller.user",
    }
)

#: Re-pointed as they are. Copyright strikes and bans of this person go too, or merging would
#: launder them.
REPOINTED = frozenset(
    {
        "admin.LogEntry.user",
        "auctions.AdCampaignGroup.contact_user",
        "auctions.AdCampaignResponse.user",
        "auctions.AgentProposal.decided_by",
        "auctions.AgentProposal.proposed_by",
        "auctions.AssistantSkillRequest.user",
        "auctions.AuctionDropdown.user",
        "auctions.AuctionHistory.user",
        "auctions.AuctionRandomOption.user",
        "auctions.BapAward.awarded_by",
        "auctions.Club.brevo_connected_by",
        "auctions.Club.google_calendar_connected_by",
        "auctions.Club.mailchimp_connected_by",
        "auctions.ClubAPIKey.created_by",
        "auctions.ClubAnnouncement.created_by",
        "auctions.ClubEvent.created_by",
        "auctions.ClubHistory.user",
        "auctions.ClubMember.added_by",
        "auctions.ClubMoney.created_by",
        "auctions.CommandPaletteSearch.user",
        "auctions.ContentReport.reported_by",
        "auctions.ContentReport.resolved_by",
        "auctions.CopyrightNotice.submitted_by",
        "auctions.CopyrightStrike.issued_by",
        "auctions.CopyrightStrike.user",
        "auctions.Document.owner",
        "auctions.DocumentBatch.owner",
        "auctions.DocumentFeedback.user",
        "auctions.DonationEmail.sent_by",
        "auctions.DonationVendor.createdby",
        "auctions.FormFailure.user",
        "auctions.InvoiceAdjustment.user",
        "auctions.LLMUsage.user",
        "auctions.Lot.added_by",
        "auctions.Lot.label_first_printed_by",
        "auctions.Lot.max_bid_revealed_by",
        "auctions.LotHistory.user",
        "auctions.LotObservation.user",
        "auctions.LotQueueEntry.added_by",
        "auctions.MobileOfflineOp.user",
        "auctions.PushNotificationSent.user",
        "auctions.Speaker.created_by",
        "auctions.Speaker.user",
        "auctions.SpeakerComment.user",
        "auctions.SpeciesCommonName.added_by",
        "auctions.Species.added_by",
        "auctions.SpeciesSearchCache.created_by",
        "auctions.SpeciesNameVote.user",
        "auctions.TapToPayAttempt.created_by",
        # Self-bans and duplicates this makes are removed afterwards.
        "auctions.UserBan.banned_user",
        "auctions.UserBan.user",
        "auctions.VoiceCommandLog.user",
        "auctions.VolunteerJob.created_by",
        # AI agents: keys made on /ai/ and the OAuth clients and tokens agents on /mcp/ connected
        # with, so a connected agent carries on as the kept account.
        "auctions.UserAPIKey.user",
        "oauth2_provider.AccessToken.user",
        "oauth2_provider.Application.user",
        "oauth2_provider.DeviceGrant.user",
        "oauth2_provider.Grant.user",
        "oauth2_provider.IDToken.user",
        "oauth2_provider.RefreshToken.user",
        # The reason to merge at all: signing in with Google, Apple or Facebook lands on the kept account.
        "socialaccount.SocialAccount.user",
    }
)

#: Re-pointed unless the kept account already has a row with the same values in these fields, in
#: which case the source's row is dropped. An empty tuple is a one-to-one.
REPOINTED_UNLESS_DUPLICATE = {
    "auctions.AbandonedBid.user": ("lot",),
    "auctions.CheckinNudge.user": ("auction", "kind"),
    "auctions.ObservedPrinter.user": ("ble_name", "model", "profile_slug"),
    "auctions.SignInStitch.user": ("session_id",),
    "auctions.SpeakerTag.user": ("speaker", "tag"),
    "auctions.UserLabelPrefs.user": (),
    "authtoken.Token.user": (),
}

#: Dropped with the account. A phone signed in as the closed account is signed out (its app tokens are
#: blacklisted), so its push registrations go rather than deliver the kept account's notifications to
#: it. Email addresses stay off the kept account by design.
DROPPED = frozenset(
    {
        "account.EmailAddress.user",
        "auctions.MobileDevice.user",
        "auctions.RemotePrintJob.user",
        "webpush.PushInformation.user",
    }
)

#: Left on the closed account: its emptied profile, the blacklisted tokens that must keep naming it, and
#: its cleared request.
LEFT_BEHIND = frozenset(
    {"auctions.UserData.user", "auctions.UserData.merge_into_user", "token_blacklist.OutstandingToken.user"}
)


class MergeRefused(Exception):
    """A request or acceptance that can't go ahead; the message is shown to the user."""


def _cutoff():
    return timezone.now() - timezone.timedelta(hours=REQUEST_HOURS)


def _staff(user):
    return user.is_staff or user.is_superuser


def find_user(username):
    """The active account called *username*, exact case first. None if there isn't one."""
    username = (username or "").strip()
    if not username:
        return None
    active = get_user_model().objects.filter(is_active=True)
    return active.filter(username=username).first() or active.filter(username__iexact=username).first()


def pending_request(user):
    """``(target, expires)`` for *user*'s own live request, or None."""
    userdata = getattr(user, "userdata", None)
    if not userdata or not userdata.merge_into_user_id or not userdata.merge_requested_on:
        return None
    if userdata.merge_requested_on < _cutoff() or not userdata.merge_into_user.is_active:
        return None
    return userdata.merge_into_user, userdata.merge_requested_on + timezone.timedelta(hours=REQUEST_HOURS)


def pending_against(user):
    """The active accounts with a live request to be merged into *user*, oldest first."""
    return list(
        get_user_model()
        .objects.filter(
            is_active=True,
            userdata__merge_into_user=user,
            userdata__merge_requested_on__gte=_cutoff(),
        )
        .order_by("userdata__merge_requested_on")
    )


def request_merge(source, username):
    """Ask for *source* to be merged into the account called *username*; returns it.

    Replaces any earlier request from *source*. Nothing is sent to the other account.
    """
    from auctions.models import UserData

    username = (username or "").strip()
    if _staff(source):
        msg = "Staff accounts can't be merged from this page."
        raise MergeRefused(msg)
    if not username:
        msg = "Type a username."
        raise MergeRefused(msg)
    target = find_user(username)
    if not target:
        msg = f"There's no account called {username}."
        raise MergeRefused(msg)
    if target == source:
        msg = "That's this account. Type the username of the account you want to keep."
        raise MergeRefused(msg)
    if _staff(target):
        msg = "That account can't be merged from this page."
        raise MergeRefused(msg)
    userdata, _ = UserData.objects.get_or_create(user=source)
    userdata.merge_into_user = target
    userdata.merge_requested_on = timezone.now()
    userdata.save(update_fields=["merge_into_user", "merge_requested_on"])
    logger.info("User %s asked to be merged into user %s", source.pk, target.pk)
    return target


def cancel_request(source):
    """Withdraw *source*'s request, if it has one."""
    from auctions.models import UserData

    UserData.objects.filter(user=source).update(merge_into_user=None, merge_requested_on=None)


def decline_request(target, source_pk):
    """Refuse the request from *source_pk* to merge into *target*, if there is one."""
    from auctions.models import UserData

    if not str(source_pk or "").isdigit():
        return
    UserData.objects.filter(user_id=source_pk, merge_into_user=target).update(
        merge_into_user=None, merge_requested_on=None
    )


def merge_summary(source):
    """What the kept account gets, for the confirmation page."""
    from allauth.socialaccount.models import SocialAccount
    from oauth2_provider.models import get_refresh_token_model

    from auctions.models import Auction, AuctionTOS, ClubMember, Lot, UserAPIKey

    sign_ins = []
    for account in SocialAccount.objects.filter(user=source):
        try:
            provider, shown_as = account.get_provider().name, account.get_provider_account().to_str()
        except Exception:
            provider, shown_as = account.provider.title(), ""
        sign_ins.append({"provider": provider, "account": shown_as})
    return {
        "lots_sold": Lot.objects.filter(user=source, is_deleted=False).count(),
        "lots_won": Lot.objects.filter(winner=source, is_deleted=False).count(),
        "auctions_joined": AuctionTOS.objects.filter(user=source).count(),
        "auctions_created": Auction.objects.filter(created_by=source, is_deleted=False).count(),
        "clubs": list(
            ClubMember.objects.filter(user=source, is_deleted=False)
            .order_by("club__name")
            .values_list("club__name", flat=True)
        ),
        "credit": source.userdata.credit,
        "sign_ins": sign_ins,
        # Each connected agent and each key acts as whoever owns it, so they're named like sign-ins.
        "agents": UserAPIKey.objects.filter(user=source, is_active=True).count()
        + get_refresh_token_model()
        .objects.filter(user=source, revoked__isnull=True)
        .order_by()
        .values("application_id")
        .distinct()
        .count(),
    }


def accept_merge(target, source_pk):
    """Merge the account *source_pk* into *target*, if it asked to be and still can. Returns the source.

    The request is re-read under a row lock, so two clicks can't both run it.
    """
    from auctions.models import UserData

    msg = "That request has expired or been withdrawn."
    if not str(source_pk or "").isdigit():
        raise MergeRefused(msg)
    with transaction.atomic():
        userdata = (
            UserData.objects.select_for_update()
            .select_related("user")
            .filter(
                user_id=source_pk,
                user__is_active=True,
                merge_into_user=target,
                merge_requested_on__gte=_cutoff(),
            )
            .first()
        )
        if not userdata or not target.is_active:
            raise MergeRefused(msg)
        if _staff(userdata.user) or _staff(target):
            msg = "Staff accounts can't be merged from this page."
            raise MergeRefused(msg)
        return merge_accounts(userdata.user, target)


def _relation(label):
    app_model, field = label.rsplit(".", 1)
    return apps.get_model(app_model), field


def _repoint(source, target):
    for label in sorted(REPOINTED):
        model, field = _relation(label)
        model.objects.filter(**{field: source}).update(**{field: target})
    for label, keys in REPOINTED_UNLESS_DUPLICATE.items():
        model, field = _relation(label)
        target_rows = model.objects.filter(**{field: target})
        taken = set(target_rows.values_list(*keys)) if keys else ({()} if target_rows.exists() else set())
        for pk, *values in model.objects.filter(**{field: source}).values_list("pk", *keys):
            if tuple(values) in taken:
                model.objects.filter(pk=pk).delete()
                continue
            model.objects.filter(pk=pk).update(**{field: target})
            taken.add(tuple(values))

    from auctions.models import UserBan

    seen = set()
    bans = UserBan.objects.filter(Q(user=target) | Q(banned_user=target)).order_by("pk")
    for pk, *pair in bans.values_list("pk", "user_id", "banned_user_id"):
        if pair[0] == pair[1] or tuple(pair) in seen:
            UserBan.objects.filter(pk=pk).delete()
        seen.add(tuple(pair))


def _drop(source):
    from webpush.models import PushInformation, SubscriptionInfo

    from auctions.account_deletion import blacklist_refresh_tokens

    subscription_pks = list(PushInformation.objects.filter(user=source).values_list("subscription_id", flat=True))
    for label in sorted(DROPPED):
        model, field = _relation(label)
        model.objects.filter(**{field: source}).delete()
    SubscriptionInfo.objects.filter(pk__in=subscription_pks).delete()
    blacklist_refresh_tokens(source)


def _close(source, target):
    """Leave *source* an inactive shell, its username and email free; *target* gets any name it lacks."""
    from auctions.models import UserData

    target_updates = []
    for field in ("first_name", "last_name"):
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
            target_updates.append(field)
    if target_updates:
        target.save(update_fields=target_updates)

    # Every request naming the closed account, including one the kept account made the other way.
    UserData.objects.filter(merge_into_user=source).update(merge_into_user=None, merge_requested_on=None)
    UserData.objects.filter(user=source).update(
        merge_into_user=None, merge_requested_on=None, account_deletion_requested=None
    )
    source.username = f"merged-user-{source.pk}"
    source.first_name = ""
    source.last_name = ""
    source.email = ""
    source.is_active = False
    source.set_unusable_password()
    source.save()


def _tell_the_closed_account(email, old_username, target):
    """Account correspondence to the address being retired, so a merge nobody meant is noticed."""
    from django.contrib.sites.models import Site
    from post_office import mail

    domain = Site.objects.get_current().domain
    mail.send(
        email,
        subject="Your account was merged",
        message=(
            f"Your {domain} account {old_username} was merged into {target.username}, and "
            f"{old_username} is closed. Sign in as {target.username} from now on.\n\n"
            f"If this wasn't you, tell us: https://{domain}{reverse('support')}"
        ),
    )


def merge_accounts(source, target):
    """Move everything *source* has to *target* and close *source*. Not reversible. Returns *source*."""
    from auctions.models import UserData

    if source == target:
        msg = "Cannot merge a user into itself."
        raise ValueError(msg)
    email, old_username = source.email, source.username
    with transaction.atomic():
        UserData.objects.get_or_create(user=source)[0].merge_into(target)
        _repoint(source, target)
        _drop(source)
        _close(source, target)
    if email:
        transaction.on_commit(lambda: _tell_the_closed_account(email, old_username, target))
    logger.info("User %s (%s) merged into user %s", source.pk, old_username, target.pk)
    return source
