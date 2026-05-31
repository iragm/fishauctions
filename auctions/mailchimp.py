"""One-way Django -> Mailchimp sync for clubs.

A club connects its own Mailchimp account via OAuth (see the Mailchimp*View classes in
views.py). From then on this module keeps the club's chosen audience in sync: members are
pushed as Mailchimp contacts with rich merge fields and lifecycle/value tags, so club admins
can build automations and targeted campaigns.

The sync is intentionally one-way. The only thing that flows back is unsubscribe/cleaned
status (via the webhook) so we stop emailing those people in Mailchimp without ever touching
their site-level email preferences.

All Mailchimp API access goes through get_client(); tests mock that single entry point.
"""

import hashlib
import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Custom merge fields provisioned on connect. (FNAME / LNAME already exist on every new list.)
# Each tuple is (tag, name, type). Mailchimp limits merge tags to 10 characters.
MERGE_FIELDS = (
    ("MEMBERNO", "Member number", "number"),
    ("EXPIRES", "Membership expires", "date"),
    ("RENEW", "Renewal / membership link", "url"),
    ("BARCODE", "Membership barcode", "url"),
    ("PHONE", "Phone number", "text"),
    ("ADDRESS", "Address", "text"),
    ("UNSUB", "Unsubscribe link", "url"),
    ("RESUB", "Resubscribe link", "url"),
    ("NOCOMM", "Stop all contact link", "url"),
)


class MailchimpError(Exception):
    """Raised for unrecoverable Mailchimp problems the caller should surface/log."""


def _readable_api_error(exc):
    """Extract the human-readable 'detail' field from a Mailchimp 400/4xx JSON body, or
    fall back to the raw text.  Used so the last_error column stays short and scannable."""
    import json as _json

    text = getattr(exc, "text", "") or str(exc)
    try:
        data = _json.loads(text)
        return data.get("detail") or data.get("title") or text
    except Exception:
        return text


def get_client(club):
    """Return a configured mailchimp-marketing client for a connected club, or None."""
    if not club.mailchimp_access_token or not club.mailchimp_server_prefix:
        return None
    import mailchimp_marketing as MailchimpMarketing

    client = MailchimpMarketing.Client()
    client.set_config(
        {
            "access_token": club.mailchimp_access_token,
            "server": club.mailchimp_server_prefix,
        }
    )
    return client


def exchange_oauth_code(code, redirect_uri):
    """Exchange an OAuth authorization code for (access_token, server_prefix).

    Uses plain HTTP for the handshake (the marketing SDK only covers data-plane calls),
    mirroring SquareCallbackView. Returns (token, dc) or raises MailchimpError.
    """
    import requests

    token_resp = requests.post(
        "https://login.mailchimp.com/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": settings.MAILCHIMP_CLIENT_ID,
            "client_secret": settings.MAILCHIMP_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=15,
    )
    if token_resp.status_code != 200:
        msg = f"Token exchange failed ({token_resp.status_code}): {token_resp.text}"
        raise MailchimpError(msg)
    access_token = token_resp.json().get("access_token")
    if not access_token:
        msg = "Token exchange response did not include an access_token"
        raise MailchimpError(msg)

    meta_resp = requests.get(
        "https://login.mailchimp.com/oauth2/metadata",
        headers={"Authorization": f"OAuth {access_token}"},
        timeout=15,
    )
    if meta_resp.status_code != 200:
        msg = f"Metadata lookup failed ({meta_resp.status_code}): {meta_resp.text}"
        raise MailchimpError(msg)
    dc = meta_resp.json().get("dc")
    if not dc:
        msg = "Metadata response did not include a data-center prefix"
        raise MailchimpError(msg)
    return access_token, dc


def subscriber_hash(email):
    """Mailchimp identifies contacts by the MD5 of the lowercased, trimmed email."""
    return hashlib.md5((email or "").strip().lower().encode("utf-8")).hexdigest()  # noqa: S324


# --- connect-time provisioning ---------------------------------------------------------------


def list_audiences(client):
    """Return [{'id','name','member_count'}] for the connected account."""
    resp = client.lists.get_all_lists(count=200)
    out = []
    for lst in resp.get("lists", []):
        out.append(
            {
                "id": lst.get("id"),
                "name": lst.get("name"),
                "member_count": (lst.get("stats") or {}).get("member_count", 0),
            }
        )
    return out


def create_audience(client, club, from_email, contact):
    """Create a '{club name} Members' audience and return its id."""
    body = {
        "name": f"{club.name} Members",
        "contact": contact,
        "permission_reminder": f"You are receiving this because you are a member of {club.name}.",
        "email_type_option": True,
        "campaign_defaults": {
            "from_name": club.name,
            "from_email": from_email,
            "subject": "",
            "language": "en",
        },
    }
    resp = client.lists.create_list(body)
    return resp.get("id"), resp.get("name")


def ensure_merge_fields(club):
    """Create any missing custom merge fields on the club's audience (idempotent)."""
    client = get_client(club)
    if not client or not club.mailchimp_audience_id:
        return
    current = client.lists.get_list_merge_fields(club.mailchimp_audience_id, count=100)
    existing = {mf["tag"] for mf in current.get("merge_fields", [])}
    for tag, name, field_type in MERGE_FIELDS:
        if tag in existing:
            continue
        try:
            client.lists.add_list_merge_field(
                club.mailchimp_audience_id,
                {"tag": tag, "name": name, "type": field_type, "public": False, "required": False},
            )
        except Exception:
            logger.exception("Failed to create Mailchimp merge field %s for club %s", tag, club.pk)


def ensure_segments(club):
    """Pre-create a named static segment for each tag so admins get ready-to-use audiences.

    Tags and static segments share Mailchimp's backend, so creating them up front means the
    segments appear immediately and get populated as members sync.
    """
    from auctions.models import ClubMember

    client = get_client(club)
    if not client or not club.mailchimp_audience_id:
        return
    try:
        existing = {
            s["name"] for s in client.lists.list_segments(club.mailchimp_audience_id, count=200).get("segments", [])
        }
    except Exception:
        existing = set()
    for tag in ClubMember.MAILCHIMP_TAGS:
        if tag in existing:
            continue
        try:
            client.lists.create_segment(club.mailchimp_audience_id, {"name": tag, "static_segment": []})
        except Exception:
            # A 400 here usually means the tag/segment already exists under a different case.
            logger.debug("Could not pre-create Mailchimp segment %s for club %s", tag, club.pk)


def ensure_webhook(club):
    """Register the unsubscribe/cleaned/upemail webhook for the club's audience (idempotent)."""
    client = get_client(club)
    if not client or not club.mailchimp_audience_id or not club.mailchimp_webhook_secret:
        return
    url = _webhook_url(club)
    try:
        existing = client.lists.get_list_webhooks(club.mailchimp_audience_id).get("webhooks", [])
        if any(w.get("url") == url for w in existing):
            return
        client.lists.create_list_webhook(
            club.mailchimp_audience_id,
            {
                "url": url,
                "events": {
                    "subscribe": False,
                    "unsubscribe": True,
                    "profile": True,
                    "cleaned": True,
                    "upemail": True,
                    "campaign": False,
                },
                # Ignore changes our own API makes, so we never echo our writes back to ourselves.
                "sources": {"user": True, "admin": True, "api": False},
            },
        )
    except Exception:
        logger.exception("Failed to register Mailchimp webhook for club %s", club.pk)


# --- per-member sync -------------------------------------------------------------------------


def _site_domain():
    try:
        return Site.objects.get_current().domain
    except Site.DoesNotExist:
        return "localhost"


def _self_service_url(member, urlname):
    path = reverse(urlname, kwargs={"slug": member.club.slug, "uuid": member.uuid})
    return f"https://{_site_domain()}{path}"


def _desired_status(member):
    """Map our contact model to a Mailchimp status.

    contact        -> subscribed
    non_essential  -> unsubscribed (no marketing, kept so they can resubscribe)
    do_not_contact -> archived (removed from the active audience)
    A bad/blank email or a deactivated member is also archived.
    """
    if member.is_deleted or not member.email or member.email_address_status == "BAD":
        return "archived"
    if member.contact_status == "do_not_contact":
        return "archived"
    if member.contact_status == "non_essential":
        return "unsubscribed"
    return "subscribed"


def member_merge_fields(member):
    """Build the Mailchimp merge_fields payload for a member."""
    fields = {
        "FNAME": member.first_name,
        "LNAME": member.last_name,
        "MEMBERNO": member.membership_number or 0,
        "EXPIRES": member.membership_expiration_date.isoformat() if member.membership_expiration_date else "",
        "RENEW": member.wallet_link,
        "BARCODE": member.barcode_image_link_png,
        "PHONE": member.phone_as_string,
        "ADDRESS": member.address or "",
        "UNSUB": _self_service_url(member, "club_member_unsubscribe"),
        "RESUB": _self_service_url(member, "club_member_resubscribe"),
        "NOCOMM": _self_service_url(member, "club_member_nocomm"),
    }
    return fields


def sync_member(member, force_status=False):
    """Upsert one member into the club's Mailchimp audience and reconcile their tags.

    Returns True on a successful sync/archive, False when there's nothing to do.
    Respects Mailchimp-side unsubscribes (won't resubscribe) unless force_status=True
    (used by the explicit resubscribe self-service action).
    """
    from mailchimp_marketing.api_client import ApiClientError

    club = member.club
    if not club.mailchimp_connected:
        return False

    client = get_client(club)
    if not client:
        return False

    list_id = club.mailchimp_audience_id
    desired = _desired_status(member)

    # Keep the power-seller/buyer tags accurate before we compute the tag set.
    member.refresh_cached_totals(save=True)

    try:
        if desired == "archived":
            _archive_member(client, member, list_id)
            _record_sync(member, status="archived", web_id="")
            return True

        body = {
            "email_address": member.email,
            "status_if_new": "subscribed" if desired == "subscribed" else "unsubscribed",
            "merge_fields": member_merge_fields(member),
        }
        # Force the status unless we'd be overriding a Mailchimp-side unsubscribe.
        respect_remote_optout = (
            desired == "subscribed" and not force_status and member.mailchimp_status in ("unsubscribed", "cleaned")
        )
        if not respect_remote_optout:
            body["status"] = desired

        result = client.lists.set_list_member(list_id, subscriber_hash(member.email), body)
        _sync_tags(client, member, list_id)
        _record_sync(member, status=result.get("status", desired), web_id=str(result.get("web_id", "") or ""))
        _clear_error(club)
        return True
    except ApiClientError as e:
        detail = _readable_api_error(e)
        status_code = getattr(e, "status_code", None)
        if status_code == 400:
            # Mailchimp rejected this specific address (fake/invalid email, bad merge field, etc.).
            # Record it on the member row but don't propagate — other members in the batch should still sync.
            logger.warning("Mailchimp rejected member %s (%s): %s", member.pk, member.email, detail)
            _record_sync(member, status="cleaned", web_id=member.mailchimp_web_id or "")
        else:
            # 4xx auth or 5xx — record on the club so admins see it in the status panel.
            _record_error(club, detail)
            logger.error("Mailchimp sync failed for member %s (club %s): %s", member.pk, club.pk, detail)
        return False


def _sync_tags(client, member, list_id):
    from mailchimp_marketing.api_client import ApiClientError

    tag_states = member.compute_mailchimp_tags()
    tags = [{"name": name, "status": "active" if active else "inactive"} for name, active in tag_states.items()]
    try:
        client.lists.update_list_member_tags(list_id, subscriber_hash(member.email), {"tags": tags})
    except ApiClientError:
        logger.exception("Failed to update Mailchimp tags for member %s", member.pk)


def _archive_member(client, member, list_id):
    """Soft-delete (archive) the contact; ignore 404 if they were never synced."""
    from mailchimp_marketing.api_client import ApiClientError

    try:
        client.lists.delete_list_member(list_id, subscriber_hash(member.email))
    except ApiClientError as e:
        if getattr(e, "status_code", None) != 404:
            raise


def change_member_email(member, old_email):
    """Move a contact from old_email to the member's current email in Mailchimp."""
    club = member.club
    if not club.mailchimp_connected or not old_email or old_email == member.email:
        return
    client = get_client(club)
    if not client:
        return
    from mailchimp_marketing.api_client import ApiClientError

    try:
        client.lists.update_list_member(
            club.mailchimp_audience_id,
            subscriber_hash(old_email),
            {"email_address": member.email},
        )
    except ApiClientError as e:
        if getattr(e, "status_code", None) == 404:
            # Old address was never synced; just create the new contact.
            sync_member(member)
        else:
            logger.exception("Failed to update Mailchimp email for member %s", member.pk)


# --- bulk / scope helpers --------------------------------------------------------------------


def in_scope_members(club):
    """Members eligible for syncing into this club's audience (see plan: 'club consent only').

    Always excludes deactivated members and those with no email. do_not_contact / bad-email
    members are still returned (sync_member archives them so Mailchimp reflects the opt-out).
    """
    from auctions.models import ClubMember

    return ClubMember.objects.filter(club=club, is_deleted=False).exclude(email__isnull=True).exclude(email="")


def backfill(club):
    """Queue a sync for every in-scope member after an initial connection.

    Reuses the per-member task path so each contact's web_id/status is captured locally
    (Mailchimp's batch endpoint returns asynchronously and makes that bookkeeping harder).
    """
    from auctions.tasks import sync_club_member_to_mailchimp

    count = 0
    for member_id in in_scope_members(club).values_list("pk", flat=True):
        sync_club_member_to_mailchimp.delay(member_id)
        count += 1
    return count


# --- local bookkeeping -----------------------------------------------------------------------


def _record_sync(member, *, status, web_id):
    from auctions.models import ClubMember

    member.mailchimp_status = status
    member.mailchimp_web_id = web_id
    member.mailchimp_last_synced = timezone.now()
    ClubMember.objects.filter(pk=member.pk).update(
        mailchimp_status=status,
        mailchimp_web_id=web_id,
        mailchimp_last_synced=member.mailchimp_last_synced,
    )


def _record_error(club, message):
    from auctions.models import Club

    Club.objects.filter(pk=club.pk).update(mailchimp_last_error=(message or "")[:2000])


def _clear_error(club):
    from auctions.models import Club

    Club.objects.filter(pk=club.pk).update(mailchimp_last_sync=timezone.now(), mailchimp_last_error="")


def _webhook_url(club):
    path = reverse("mailchimp_webhook", kwargs={"slug": club.slug, "secret": club.mailchimp_webhook_secret})
    return f"https://{_site_domain()}{path}"


def member_in_mailchimp_url(member):
    """Admin deep link to view this contact in Mailchimp, or '' if not synced."""
    club = member.club
    if not member.mailchimp_web_id or not club.mailchimp_server_prefix:
        return ""
    return f"https://{club.mailchimp_server_prefix}.admin.mailchimp.com/lists/members/view?id={member.mailchimp_web_id}"
