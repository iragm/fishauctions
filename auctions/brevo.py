"""One-way Django -> Brevo sync for clubs, built like auctions/mailchimp.py.

A club connects with a Brevo API key (views/club_integrations.py). Members sync to the chosen list
with attributes. Only unsubscribe/bounce/spam status flows back, via the webhook.

Unlike Mailchimp: auth is a per-club encrypted API key (Brevo's OAuth isn't public), and Brevo has
no tags, so tags and top categories go in the MEMBER_TAGS and CATEGORIES attributes.

Shares category ranking, scope and self-service helpers with mailchimp. All API access goes
through get_client(), which tests mock.
"""

import logging
import re
from urllib.parse import quote

import requests
from django.urls import reverse
from django.utils import timezone

from auctions.helper_functions import scrub_emails
from auctions.mailchimp import _self_service_url, _site_domain, _top_category_names, in_scope_members

logger = logging.getLogger(__name__)

API_BASE = "https://api.brevo.com/v3"

# Attributes provisioned on connect, as (name, brevo_type); FIRSTNAME/LASTNAME already exist.
CONTACT_ATTRIBUTES = (
    ("MEMBERNO", "float"),
    ("EXPIRES", "date"),
    ("RENEW", "text"),
    ("BARCODE", "text"),
    ("PHONE", "text"),
    ("ADDRESS", "text"),
    ("MEMBER_TAGS", "text"),
    ("CATEGORIES", "text"),
    ("CLUBUNSUB", "text"),
    ("RESUB", "text"),
    ("NOCOMM", "text"),
)

# Webhook events in Brevo's registration spelling; the view handles the inbound spelling.
WEBHOOK_EVENTS = ["unsubscribed", "hardBounce", "spam", "contactDeleted"]


class BrevoError(Exception):
    """Unrecoverable Brevo problems, such as auth."""


class BrevoApiError(Exception):
    """A non-2xx response, with status_code."""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _readable_api_error(resp):
    """A short message from a Brevo error body."""
    try:
        data = resp.json()
        return data.get("message") or data.get("error") or resp.text
    except Exception:
        return getattr(resp, "text", "") or str(resp)


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def blocked_ip_from_error(exc):
    """Classify a 401: the blocked IP string, "" for an IP block with no parseable IP, or None for
    anything else (a bad key).
    """
    if not isinstance(exc, BrevoApiError) or exc.status_code != 401:
        return None
    detail = exc.detail or ""
    match = _IPV4_RE.search(detail)
    if match:
        return match.group(0)
    low = detail.lower()
    if "authorized ip" in low or "authorised ip" in low or "whitelist" in low or "ip address" in low:
        return ""
    return None


def outbound_ip():
    """This server's outbound IP for Brevo allowlisting, cached a day; "" if unknown. Never raises."""
    from django.core.cache import cache

    cached = cache.get("brevo_outbound_ip")
    if cached is not None:
        return cached
    ip = ""
    try:
        resp = requests.get("https://api.ipify.org", timeout=5)
        if resp.status_code == 200:
            ip = resp.text.strip()
    except requests.RequestException:
        ip = ""
    # Cache a good answer for a day; retry a failed lookup sooner.
    cache.set("brevo_outbound_ip", ip, 60 * 60 * 24 if ip else 300)
    return ip


# --- client ----------------------------------------------------------------------------------


class BrevoClient:
    """Thin wrapper around the Brevo REST API using the club's api-key header.

    Raises BrevoApiError on 4xx/5xx; network errors propagate so Celery can retry.
    """

    def __init__(self, club):
        self.club = club

    def request(self, method, path, *, json_body=None, params=None):
        resp = requests.request(
            method,
            API_BASE + path,
            headers={"api-key": self.club.brevo_api_key, "accept": "application/json"},
            json=json_body,
            params=params,
            timeout=20,
        )
        if resp.status_code >= 400:
            raise BrevoApiError(resp.status_code, _readable_api_error(resp))
        return resp


def get_client(club):
    """Return an authenticated BrevoClient for a connected club, or None."""
    if not club.brevo_api_key:
        return None
    return BrevoClient(club)


# --- connect-time provisioning ---------------------------------------------------------------


def list_contact_lists(client):
    """[{'id', 'name'}] for the account's lists, without subscriber totals."""
    out = []
    offset = 0
    while True:
        resp = client.request("GET", "/contacts/lists", params={"limit": 50, "offset": offset})
        data = resp.json()
        lists = data.get("lists", [])
        for lst in lists:
            out.append({"id": lst.get("id"), "name": lst.get("name")})
        offset += 50
        if offset >= data.get("count", 0) or not lists:
            break
    return out


def ensure_folder(client, club):
    """The club's Brevo folder id, created if needed."""
    if club.brevo_folder_id:
        return club.brevo_folder_id
    resp = client.request("POST", "/contacts/folders", json_body={"name": f"{club.name} (auction site)"})
    folder_id = str(resp.json().get("id", ""))
    if folder_id:
        from auctions.models import Club

        club.brevo_folder_id = folder_id
        Club.objects.filter(pk=club.pk).update(brevo_folder_id=folder_id)
    return folder_id


def create_contact_list(client, club):
    """Create a '{club name} Members' list in the club's folder and return (id, name)."""
    folder_id = ensure_folder(client, club)
    name = f"{club.name} Members"
    body = {"name": name, "folderId": int(folder_id)}
    resp = client.request("POST", "/contacts/lists", json_body=body)
    return str(resp.json().get("id", "")), name


def ensure_attributes(club):
    """Create any missing custom contact attributes on the account (idempotent)."""
    client = get_client(club)
    if not client:
        return
    for name, attr_type in CONTACT_ATTRIBUTES:
        try:
            client.request(
                "POST",
                f"/contacts/attributes/normal/{name}",
                json_body={"type": attr_type},
            )
        except BrevoApiError as exc:
            # 400 usually means it already exists.
            if exc.status_code != 400:
                logger.error(
                    "Failed to create Brevo attribute %s for club %s: %s", name, club.pk, scrub_emails(exc.detail)
                )


def ensure_webhook(club):
    """Register the unsubscribe/bounce/spam/delete marketing webhook (idempotent)."""
    client = get_client(club)
    if not client or not club.brevo_webhook_secret:
        return
    url = _webhook_url(club)
    try:
        existing = client.request("GET", "/webhooks", params={"type": "marketing"}).json().get("webhooks", [])
        for hook in existing:
            if hook.get("url") == url:
                _store_webhook_id(club, str(hook.get("id", "")))
                return
        resp = client.request(
            "POST",
            "/webhooks",
            json_body={
                "type": "marketing",
                "url": url,
                "description": "Auction site member sync (unsubscribe/bounce/spam)",
                "events": WEBHOOK_EVENTS,
            },
        )
        _store_webhook_id(club, str(resp.json().get("id", "")))
    except (BrevoApiError, BrevoError):
        logger.exception("Failed to register Brevo webhook for club %s", club.pk)


def _store_webhook_id(club, webhook_id):
    if not webhook_id:
        return
    from auctions.models import Club

    club.brevo_webhook_id = webhook_id
    Club.objects.filter(pk=club.pk).update(brevo_webhook_id=webhook_id)


# --- per-member sync -------------------------------------------------------------------------


def _desired_status(member):
    """Map contact status to a Brevo intent.

    contact        -> subscribed   (emailBlacklisted False)
    non_essential  -> unsubscribed (emailBlacklisted True, can resubscribe)
    do_not_contact -> archived     (contact deleted)
    A bad or blank email or a deactivated member is also archived.
    """
    if member.is_deleted or not member.email or member.email_address_status == "BAD":
        return "archived"
    if member.contact_status == "do_not_contact":
        return "archived"
    if member.contact_status == "non_essential":
        return "unsubscribed"
    return "subscribed"


def member_attributes(member):
    """The Brevo attributes for a member, with tags and categories as text attributes."""
    active_tags = [name for name, active in member.compute_mailchimp_tags().items() if active]
    categories = sorted(_top_category_names(member))
    return {
        "FIRSTNAME": member.first_name,
        "LASTNAME": member.last_name,
        "MEMBERNO": member.membership_number or 0,
        "EXPIRES": member.membership_expiration_date.isoformat() if member.membership_expiration_date else "",
        "RENEW": member.wallet_link,
        "BARCODE": member.barcode_image_link_png,
        "PHONE": member.phone_as_string,
        "ADDRESS": member.address or "",
        "MEMBER_TAGS": "|".join(active_tags),
        "CATEGORIES": "|".join(categories),
        "CLUBUNSUB": _self_service_url(member, "club_member_unsubscribe"),
        "RESUB": _self_service_url(member, "club_member_resubscribe"),
        "NOCOMM": _self_service_url(member, "club_member_nocomm"),
    }


def sync_member(member, force_status=False):
    """Upsert one member into the list, or delete them, and record the result. False when nothing to do.

    Won't resubscribe a Brevo-side unsubscribe unless force_status=True.
    """
    club = member.club
    if not club.brevo_connected:
        return False
    client = get_client(club)
    if not client:
        return False

    member.refresh_cached_totals(save=True)
    desired = _desired_status(member)

    try:
        if desired == "archived":
            _delete_contact(client, member)
            _record_sync(member, status="archived", contact_id="")
            _clear_error(club)
            return True

        respect_remote_optout = (
            desired == "subscribed" and not force_status and member.brevo_status in ("unsubscribed", "cleaned")
        )
        blacklisted = desired == "unsubscribed" or respect_remote_optout
        contact_id = _upsert_contact(client, member, blacklisted)
        if respect_remote_optout:
            status = member.brevo_status
        else:
            status = "unsubscribed" if blacklisted else "subscribed"
        _record_sync(member, status=status, contact_id=str(contact_id or member.brevo_contact_id or ""))
        _clear_error(club)
        return True
    except BrevoApiError as e:
        if e.status_code in (400, 422):
            # Rejected address: record on the member and continue. Log the pk, never the address.
            logger.warning("Brevo rejected member %s: %s", member.pk, scrub_emails(e.detail))
            _record_sync(member, status="cleaned", contact_id=member.brevo_contact_id or "")
        else:
            _record_error(club, e.detail)
            logger.error("Brevo sync failed for member %s (club %s): %s", member.pk, club.pk, scrub_emails(e.detail))
        return False
    except BrevoError as e:
        _record_error(club, str(e))
        return False


def _upsert_contact(client, member, blacklisted):
    """Create-or-update the contact (Brevo's updateEnabled) and return its contact id."""
    body = {
        "email": member.email,
        "attributes": member_attributes(member),
        "listIds": [int(member.club.brevo_list_id)],
        "emailBlacklisted": blacklisted,
        "updateEnabled": True,
    }
    resp = client.request("POST", "/contacts", json_body=body)
    if resp.status_code == 201 and resp.content:
        new_id = (resp.json() or {}).get("id")
        if new_id:
            return new_id
    # 200/204 updated an existing contact with no body.
    return member.brevo_contact_id or _fetch_contact_id(client, member.email)


def _fetch_contact_id(client, email):
    try:
        resp = client.request("GET", f"/contacts/{quote(email)}")
        return str((resp.json() or {}).get("id", ""))
    except BrevoApiError as e:
        if e.status_code == 404:
            return ""
        raise


def _delete_contact(client, member):
    """Remove the contact from Brevo; ignore 404 if they were never synced."""
    _delete_contact_by_email(client, member.email)


def _delete_contact_by_email(client, email):
    try:
        client.request("DELETE", f"/contacts/{quote(email)}")
    except BrevoApiError as e:
        if e.status_code != 404:
            raise


def delete_contact_by_email(club, email):
    """Delete a contact by address, for account deletion. Returns True when a call was made."""
    if not email or not club.brevo_connected:
        return False
    client = get_client(club)
    if not client:
        return False
    _delete_contact_by_email(client, email)
    return True


def change_member_email(member, old_email):
    """Brevo can't rename a contact's email, so delete the old contact and re-sync."""
    club = member.club
    if not club.brevo_connected or not old_email or old_email == member.email:
        return
    client = get_client(club)
    if not client:
        return
    try:
        client.request("DELETE", f"/contacts/{quote(old_email)}")
    except BrevoApiError as e:
        if e.status_code != 404:
            # No traceback: the URL contains the old address.
            logger.error("Failed to delete old Brevo contact for member %s: %s", member.pk, scrub_emails(e.detail))


# --- bulk / scope helpers --------------------------------------------------------------------


def backfill(club):
    """Queue a per-member sync for every in-scope member, after connecting and nightly."""
    from auctions.tasks import sync_club_member_to_brevo

    count = 0
    for member_id in in_scope_members(club).values_list("pk", flat=True):
        sync_club_member_to_brevo.delay(member_id)
        count += 1
    return count


# --- local bookkeeping -----------------------------------------------------------------------


def _record_sync(member, *, status, contact_id):
    from auctions.models import ClubMember

    member.brevo_status = status
    member.brevo_contact_id = contact_id
    member.brevo_last_synced = timezone.now()
    ClubMember.objects.filter(pk=member.pk).update(
        brevo_status=status,
        brevo_contact_id=contact_id,
        brevo_last_synced=member.brevo_last_synced,
    )


def _record_error(club, message):
    from auctions.models import Club

    Club.objects.filter(pk=club.pk).update(brevo_last_error=(message or "")[:2000])


def _clear_error(club):
    from auctions.models import Club

    Club.objects.filter(pk=club.pk).update(brevo_last_sync=timezone.now(), brevo_last_error="")


def _webhook_url(club):
    path = reverse("brevo_webhook", kwargs={"slug": club.slug, "secret": club.brevo_webhook_secret})
    return f"https://{_site_domain()}{path}"


def member_in_brevo_url(member):
    """Admin deep link to view this contact in Brevo, or '' if not synced."""
    if not member.brevo_contact_id:
        return ""
    return f"https://app.brevo.com/contact/index/{member.brevo_contact_id}"


# --- account details and announcement campaigns ------------------------------------------------


def account_info(client):
    """The account's {'company', 'address'}, which Brevo prints in campaign footers."""
    try:
        data = client.request("GET", "/account").json()
    except Exception:
        logger.exception("Brevo: couldn't read the account")
        return {"company": "", "address": {}}
    return {"company": data.get("companyName") or "", "address": data.get("address") or {}}


def format_mailing_address(address):
    """Brevo's account `address` as a multi-line letter address."""
    if not address:
        return ""
    lines = [address.get("street")]
    city_line = " ".join(x for x in (address.get("city"), address.get("zipCode")) if x)
    lines.append(city_line)
    country = address.get("country") or ""
    if country and country.upper() not in {"US", "USA", "UNITED STATES"}:
        lines.append(country)
    return "\n".join(x.strip() for x in lines if x and x.strip())


def senders(client):
    """Every sender on the account, as [{'id', 'name', 'email', 'active'}]."""
    try:
        data = client.request("GET", "/senders").json()
    except Exception:
        logger.exception("Brevo: couldn't list senders")
        return []
    out = []
    for sender in data.get("senders", []):
        out.append(
            {
                "id": sender.get("id"),
                "name": sender.get("name") or "",
                "email": sender.get("email") or "",
                # Absent means usable.
                "active": sender.get("active", True),
            }
        )
    return out


def default_sender(club):
    """The campaign sender: the club's stored choice (Club.brevo_sender_id), else the first active one."""
    client = get_client(club)
    if not client:
        return None
    available = [s for s in senders(client) if s.get("email") and s.get("active")]
    if not available:
        return None
    if club.brevo_sender_id:
        for sender in available:
            if str(sender["id"]) == str(club.brevo_sender_id):
                return sender
    return available[0]


def send_announcement_campaign(club, *, subject, html, plain_text=None):
    """Create and send one campaign to the club's list, returning its id.

    ``plain_text`` is ignored (Brevo generates it) and only kept to match mailchimp's signature.
    Campaigns, never transactional sends, so Brevo applies the list's unsubscribes and footer.
    """
    client = get_client(club)
    if not client or not club.brevo_list_id:
        msg = "Brevo is not connected to a list."
        raise BrevoError(msg)
    sender = default_sender(club)
    if not sender:
        msg = (
            "Your Brevo account has no verified sender. In Brevo, go to Senders, domains & "
            "dedicated IPs, add and verify a sender, then try again."
        )
        raise BrevoError(msg)
    body = {
        "name": f"{club.name} announcement — {subject}"[:255],
        "subject": subject[:255],
        "sender": {"name": sender["name"] or club.name, "email": sender["email"]},
        "type": "classic",
        "htmlContent": html,
        "recipients": {"listIds": [int(club.brevo_list_id)]},
    }
    try:
        campaign_id = str(client.request("POST", "/emailCampaigns", json_body=body).json().get("id") or "")
    except BrevoApiError as e:
        msg = f"Brevo refused to create the campaign: {e.detail}"
        raise BrevoError(msg) from e
    except Exception as e:
        msg = "Couldn't reach Brevo to create the campaign."
        raise BrevoError(msg) from e
    if not campaign_id:
        msg = "Brevo created the campaign but returned no id."
        raise BrevoError(msg)
    try:
        client.request("POST", f"/emailCampaigns/{campaign_id}/sendNow")
    except BrevoApiError as e:
        msg = f"Brevo refused to send the campaign: {e.detail}"
        raise BrevoError(msg) from e
    except Exception as e:
        msg = "Couldn't reach Brevo to send the campaign."
        raise BrevoError(msg) from e
    return campaign_id


def campaign_opens(club, campaign_id):
    """Unique opens on a sent campaign, or None when Brevo can't tell us yet."""
    client = get_client(club)
    if not client or not campaign_id:
        return None
    try:
        stats = client.request("GET", f"/emailCampaigns/{campaign_id}").json().get("statistics") or {}
    except Exception:
        logger.warning("Brevo: couldn't read statistics for campaign %s", campaign_id)
        return None
    globals_ = stats.get("globalStats") or {}
    opens = globals_.get("uniqueViews")
    return opens if isinstance(opens, int) else None
