"""One-way Django -> Mailchimp sync for clubs.

A club connects its Mailchimp account via OAuth (views/club_integrations.py). Members sync to the
chosen audience with merge fields and tags. Only unsubscribe/cleaned status flows back, via the
webhook, and never touches site email preferences.

All API access goes through get_client(), which tests mock.
"""

import hashlib
import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.urls import reverse
from django.utils import timezone

from auctions.helper_functions import scrub_emails

logger = logging.getLogger(__name__)

# Merge fields provisioned on connect, as (tag, name, type); tags are at most 10 characters.
MERGE_FIELDS = (
    ("MEMBERNO", "Member number", "number"),
    ("EXPIRES", "Membership expires", "date"),
    ("RENEW", "Renewal / membership link", "url"),
    ("BARCODE", "Membership barcode", "url"),
    ("PHONE", "Phone number", "text"),
    ("ADDRESS", "Address", "text"),
    ("CLUBUNSUB", "Unsubscribe link", "url"),
    ("RESUB", "Resubscribe link", "url"),
    ("NOCOMM", "Stop all contact link", "url"),
)


class MailchimpError(Exception):
    """Raised for unrecoverable Mailchimp problems the caller should surface/log."""


def _readable_api_error(exc):
    """The 'detail' from a Mailchimp error body, or the raw text."""
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
    """Exchange an OAuth code for (access_token, server_prefix) over plain HTTP, like SquareCallbackView."""
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


def account_defaults(client):
    """{'from_name', 'from_email', 'contact'} from the club's Mailchimp account, or None.

    Campaigns send from the club's own verified domain, so the sender can't come from this site.
    Prefers an existing audience's campaign_defaults, then the account root. None when neither has an
    email; creating an audience then would bake in an unsendable default.
    """
    try:
        lists = client.lists.get_all_lists(count=200).get("lists", [])
    except Exception:
        logger.exception("Mailchimp: couldn't read existing audiences for account defaults")
        lists = []
    for lst in lists:
        defaults = lst.get("campaign_defaults") or {}
        if defaults.get("from_email"):
            return {
                "from_name": defaults.get("from_name") or "",
                "from_email": defaults["from_email"],
                "contact": lst.get("contact") or {},
            }
    try:
        root = client.root.get_root(fields=["account_name", "email", "contact"])
    except Exception:
        logger.exception("Mailchimp: couldn't read the account root for account defaults")
        return None
    if not root.get("email"):
        return None
    return {
        "from_name": root.get("account_name") or "",
        "from_email": root["email"],
        "contact": root.get("contact") or {},
    }


def format_mailing_address(contact):
    """A Mailchimp `contact` block as a multi-line postal address for donation letters, without the
    company line.
    """
    if not contact:
        return ""
    lines = [contact.get("addr1"), contact.get("addr2")]
    city_line = " ".join(x for x in (contact.get("city"), contact.get("state")) if x)
    if contact.get("zip"):
        city_line = f"{city_line} {contact['zip']}".strip()
    lines.append(city_line)
    country = contact.get("country") or ""
    # Only non-US countries are printed.
    if country and country.upper() not in {"US", "USA"}:
        lines.append(country)
    return "\n".join(x.strip() for x in lines if x and x.strip())


def create_audience(client, club):
    """Create a '{club name} Members' audience and return (id, name), using account_defaults for the sender."""
    defaults = account_defaults(client)
    if not defaults:
        msg = (
            "Mailchimp hasn't got a sender address for this account yet. Send one campaign from "
            "Mailchimp (or create an audience there), then come back and pick it from the list."
        )
        raise MailchimpError(msg)
    contact = dict(defaults["contact"])
    contact["company"] = contact.get("company") or club.name
    body = {
        "name": f"{club.name} Members",
        "contact": contact,
        "permission_reminder": f"You are receiving this because you are a member of {club.name}.",
        "email_type_option": True,
        "campaign_defaults": {
            # from_name isn't validated; from_email is, so has no fallback.
            "from_name": defaults["from_name"] or club.name,
            "from_email": defaults["from_email"],
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
        except Exception as exc:
            logger.error(
                "Failed to create Mailchimp merge field %s for club %s: %s",
                tag,
                club.pk,
                scrub_emails(_readable_api_error(exc)),
            )


def ensure_segments(club):
    """Pre-create a static segment for each tag so they're ready as members sync."""
    from auctions.models import Category, ClubMember

    client = get_client(club)
    if not client or not club.mailchimp_audience_id:
        return
    try:
        existing = {
            s["name"] for s in client.lists.list_segments(club.mailchimp_audience_id, count=200).get("segments", [])
        }
    except Exception:
        existing = set()
    all_tags = list(ClubMember.MAILCHIMP_TAGS) + list(
        Category.objects.exclude(name="Uncategorized").values_list("name", flat=True)
    )
    for tag in all_tags:
        if tag in existing:
            continue
        try:
            client.lists.create_segment(club.mailchimp_audience_id, {"name": tag, "static_segment": []})
        except Exception:
            # Usually already exists under a different case.
            logger.debug("Could not pre-create Mailchimp segment %s for club %s", tag, club.pk)


def ensure_webhook(club):
    """Register the unsubscribe/cleaned/upemail webhook for the audience (idempotent)."""
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
                # Ignore our own API changes.
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
    """Map contact status to Mailchimp status.

    contact        -> subscribed
    non_essential  -> unsubscribed (can resubscribe)
    do_not_contact -> archived
    A bad or blank email or a deactivated member is also archived.
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
        "CLUBUNSUB": _self_service_url(member, "club_member_unsubscribe"),
        "RESUB": _self_service_url(member, "club_member_resubscribe"),
        "NOCOMM": _self_service_url(member, "club_member_nocomm"),
    }
    return fields


def sync_member(member, force_status=False):
    """Upsert one member into the audience and reconcile tags. Returns False when there's nothing to do.

    Won't resubscribe a Mailchimp-side unsubscribe unless force_status=True.
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
            # Rejected address: record on the member and continue. Log the pk, never the address.
            logger.warning("Mailchimp rejected member %s: %s", member.pk, scrub_emails(detail))
            _record_sync(member, status="cleaned", web_id=member.mailchimp_web_id or "")
        else:
            # Auth or server error: record on the club for the status panel.
            _record_error(club, detail)
            logger.error("Mailchimp sync failed for member %s (club %s): %s", member.pk, club.pk, scrub_emails(detail))
        return False


def _top_category_names(member):
    """Up to five category names relevant to this member: from UserInterestCategory, else from their
    lots in this club's auctions.
    """
    from collections import Counter
    from itertools import chain

    from auctions.models import Lot, UserInterestCategory

    if member.user_id:
        top = (
            UserInterestCategory.objects.filter(user_id=member.user_id)
            .exclude(category__name="Uncategorized")
            .select_related("category")
            .order_by("-interest")[:5]
        )
        names = {uic.category.name for uic in top}
        if names:
            return names

    if not member.email:
        return set()

    seller_cats = (
        Lot.objects.filter(
            auctiontos_seller__auction__club=member.club,
            auctiontos_seller__email=member.email,
            species_category__isnull=False,
        )
        .exclude(species_category__name="Uncategorized")
        .values_list("species_category__name", flat=True)
    )
    winner_cats = (
        Lot.objects.filter(
            auctiontos_winner__auction__club=member.club,
            auctiontos_winner__email=member.email,
            species_category__isnull=False,
        )
        .exclude(species_category__name="Uncategorized")
        .values_list("species_category__name", flat=True)
    )
    counter = Counter(chain(seller_cats, winner_cats))
    return {name for name, _ in counter.most_common(5)}


def _sync_tags(client, member, list_id):
    from mailchimp_marketing.api_client import ApiClientError

    from auctions.models import Category

    tag_states = member.compute_mailchimp_tags()

    top_cats = _top_category_names(member)
    for cat_name in Category.objects.exclude(name="Uncategorized").values_list("name", flat=True):
        tag_states[cat_name] = cat_name in top_cats

    tags = [{"name": name, "status": "active" if active else "inactive"} for name, active in tag_states.items()]
    try:
        client.lists.update_list_member_tags(list_id, subscriber_hash(member.email), {"tags": tags})
    except ApiClientError as e:
        # No traceback: the API response echoes the address.
        logger.error(
            "Failed to update Mailchimp tags for member %s: %s", member.pk, scrub_emails(_readable_api_error(e))
        )


def _archive_member(client, member, list_id):
    """Soft-delete (archive) the contact; ignore 404 if they were never synced."""
    from mailchimp_marketing.api_client import ApiClientError

    try:
        client.lists.delete_list_member(list_id, subscriber_hash(member.email))
    except ApiClientError as e:
        if getattr(e, "status_code", None) != 404:
            raise


def delete_contact_by_email(club, email):
    """Permanently delete a contact from the audience by address, for account deletion. Returns True
    when a call was made.
    """
    from mailchimp_marketing.api_client import ApiClientError

    if not email or not club.mailchimp_connected:
        return False
    client = get_client(club)
    if not client:
        return False
    try:
        client.lists.delete_list_member_permanent(club.mailchimp_audience_id, subscriber_hash(email))
    except ApiClientError as e:
        if getattr(e, "status_code", None) != 404:
            raise
    return True


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
            # No traceback: the API body carries the addresses.
            logger.error(
                "Failed to update Mailchimp email for member %s: %s", member.pk, scrub_emails(_readable_api_error(e))
            )


# --- bulk / scope helpers --------------------------------------------------------------------


def in_scope_members(club):
    """Members eligible to sync: not deactivated, with an email. Opted-out members are included so they get archived."""
    from auctions.models import ClubMember

    return ClubMember.objects.filter(club=club, is_deleted=False).exclude(email__isnull=True).exclude(email="")


def backfill(club):
    """Queue a per-member sync for every in-scope member after connecting."""
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


# --- announcement campaigns -------------------------------------------------------------------
#
# Sent as campaigns to the audience, so Mailchimp applies its unsubscribes and compliance footer.


def verified_sender(client, club):
    """(from_name, from_email) from the audience's campaign defaults, or raise MailchimpError."""
    try:
        settings_ = client.lists.get_list(club.mailchimp_audience_id).get("campaign_defaults") or {}
    except Exception as e:
        msg = f"Couldn't read your Mailchimp audience: {_readable_api_error(e)}"
        raise MailchimpError(msg) from e
    from_email = settings_.get("from_email") or ""
    if not from_email:
        msg = (
            "Your Mailchimp audience has no default from address. Open the audience in Mailchimp, "
            "set its default from name and email, then try again."
        )
        raise MailchimpError(msg)
    return (settings_.get("from_name") or club.name, from_email)


def send_announcement_campaign(club, *, subject, html, plain_text):
    """Create and send one campaign to the audience, returning its id. Raises MailchimpError; runs in Celery."""
    client = get_client(club)
    if not client or not club.mailchimp_audience_id:
        msg = "Mailchimp is not connected to an audience."
        raise MailchimpError(msg)
    from_name, from_email = verified_sender(client, club)
    try:
        campaign = client.campaigns.create(
            {
                "type": "regular",
                "recipients": {"list_id": club.mailchimp_audience_id},
                "settings": {
                    # Mailchimp's internal campaign name, visible in the club's list.
                    "title": f"{club.name} announcement — {subject}"[:100],
                    "subject_line": subject[:150],
                    "from_name": from_name[:100],
                    "reply_to": from_email,
                    # Required: carries the unsubscribe link and address.
                    "auto_footer": True,
                },
            }
        )
    except MailchimpError:
        raise
    except Exception as e:
        msg = f"Mailchimp refused to create the campaign: {_readable_api_error(e)}"
        raise MailchimpError(msg) from e
    campaign_id = campaign.get("id") or ""
    if not campaign_id:
        msg = "Mailchimp created the campaign but returned no id."
        raise MailchimpError(msg)
    try:
        client.campaigns.set_content(campaign_id, {"html": html, "plain_text": plain_text})
        client.campaigns.send(campaign_id)
    except Exception as e:
        msg = f"Mailchimp refused to send the campaign: {_readable_api_error(e)}"
        raise MailchimpError(msg) from e
    return campaign_id


def campaign_opens(club, campaign_id):
    """Unique opens on a sent campaign, or None when the report isn't ready (not 0)."""
    client = get_client(club)
    if not client or not campaign_id:
        return None
    try:
        report = client.reports.get_campaign_report(campaign_id)
    except Exception:
        logger.warning("Mailchimp: couldn't read the report for campaign %s", campaign_id)
        return None
    opens = (report.get("opens") or {}).get("unique_opens")
    return opens if isinstance(opens, int) else None
