"""One-way Django -> Brevo sync for clubs.

Built the same way as auctions/mailchimp.py: a club connects its own Brevo account by pasting in
a Brevo API key (see the Brevo*View classes in views.py). From then on this module keeps the
club's chosen contact list in sync: members are pushed as Brevo contacts with rich attributes
plus lifecycle/value/category info (in the MEMBER_TAGS / CATEGORIES attributes), so club admins
can build automations and segmented campaigns.

The sync is intentionally one-way. The only thing that flows back is unsubscribe/bounce/spam
status (via the webhook) so we stop emailing those people in Brevo without ever touching their
site-level email preferences.

Two things differ from Mailchimp, both driven by Brevo's platform:
  * Brevo's public OAuth program is private/org-scoped, so clubs authenticate with a per-club API
    key (stored encrypted) sent in the "api-key" header instead of an OAuth token.
  * Brevo has no native "tags", so the lifecycle tags and top categories are written into the
    MEMBER_TAGS and CATEGORIES contact attributes (admins segment with "MEMBER_TAGS contains ...").

The category ranking, "in scope" rule, and self-service link helpers are shared with the
Mailchimp module rather than duplicated. All Brevo API access goes through get_client(); tests
mock that single entry point.
"""

import logging
import re
from urllib.parse import quote

import requests
from django.urls import reverse
from django.utils import timezone

# Reuse the platform-agnostic helpers from the Mailchimp module (same source of truth).
from auctions.mailchimp import _self_service_url, _site_domain, _top_category_names, in_scope_members

logger = logging.getLogger(__name__)

API_BASE = "https://api.brevo.com/v3"

# Custom contact attributes provisioned on connect. FIRSTNAME / LASTNAME already exist on every
# Brevo account. Each tuple is (name, brevo_type). Brevo attribute names are uppercase.
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

# Marketing webhook events we honor (Brevo's create-webhook spelling). The inbound payload uses a
# slightly different spelling (unsubscribe / hard_bounce / contact_deleted), handled in the view.
WEBHOOK_EVENTS = ["unsubscribed", "hardBounce", "spam", "contactDeleted"]


class BrevoError(Exception):
    """Raised for unrecoverable Brevo problems the caller should surface/log (e.g. auth)."""


class BrevoApiError(Exception):
    """A non-2xx data-plane response. Carries status_code so callers can treat 400/404 specially."""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _readable_api_error(resp):
    """Pull a short human-readable message out of a Brevo error body ({code, message})."""
    try:
        data = resp.json()
        return data.get("message") or data.get("error") or resp.text
    except Exception:
        return getattr(resp, "text", "") or str(resp)


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def blocked_ip_from_error(exc):
    """Classify a key-validation failure: bad key vs. Brevo's "unauthorized IP" block.

    Brevo returns 401 for both, but when it's blocking the caller's IP the message includes the
    offending IP (which is exactly what the admin must whitelist). Returns:
      * the blocked IP string when Brevo named one,
      * "" when it's clearly an IP-authorization block but no IP was parseable,
      * None for anything else (e.g. a genuinely invalid key) so callers show the bad-key message.
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
    """Best-effort public IP this server uses for outbound calls, for Brevo IP whitelisting.

    Cached for a day (the egress IP is stable); returns "" if it can't be determined, so the UI
    can fall back to generic wording. Never raises.
    """
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
    """Thin authenticated wrapper around the Brevo REST API.

    Authenticates with the club's API key (the "api-key" header), so callers never deal with
    auth. Raises BrevoApiError on 4xx/5xx; lets requests.RequestException propagate so the Celery
    task can retry network blips.
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
    """Return [{'id','name'}] for the connected account.

    We intentionally don't surface Brevo's per-list subscriber totals: they don't line up with
    what an admin expects when choosing a list, so we only show names.
    """
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
    """Return the id of the club's Brevo folder, creating it if needed (lists must live in one)."""
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
            # 400 here almost always means "attribute already exists" — safe to ignore.
            if exc.status_code != 400:
                logger.error("Failed to create Brevo attribute %s for club %s: %s", name, club.pk, exc.detail)


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
    """Map our contact model to a Brevo intent.

    contact        -> subscribed   (emailBlacklisted False)
    non_essential  -> unsubscribed (emailBlacklisted True, kept so they can resubscribe)
    do_not_contact -> archived     (contact deleted from Brevo)
    A bad/blank email or a deactivated member is also archived.
    """
    if member.is_deleted or not member.email or member.email_address_status == "BAD":
        return "archived"
    if member.contact_status == "do_not_contact":
        return "archived"
    if member.contact_status == "non_essential":
        return "unsubscribed"
    return "subscribed"


def member_attributes(member):
    """Build the Brevo attributes payload for a member.

    Reuses the model's tag vocabulary (compute_mailchimp_tags) and the shared category ranking;
    Brevo has no native tags, so the active tag/category names go into text attributes.
    """
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
    """Upsert one member into the club's Brevo list (or delete them) and record the result.

    Returns True on a successful sync/delete, False when there's nothing to do. Respects
    Brevo-side unsubscribes (won't resubscribe) unless force_status=True (the explicit
    resubscribe self-service action).
    """
    club = member.club
    if not club.brevo_connected:
        return False
    client = get_client(club)
    if not client:
        return False

    # Keep the power-seller/buyer tags accurate before we compute the attribute set.
    member.refresh_cached_totals(save=True)
    desired = _desired_status(member)

    try:
        if desired == "archived":
            _delete_contact(client, member)
            _record_sync(member, status="archived", contact_id="")
            _clear_error(club)
            return True

        # Never resurrect someone Brevo told us unsubscribed/bounced, unless explicitly forced.
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
            # Brevo rejected this specific address (invalid email, etc.). Record on the member row
            # but don't propagate — other members in the batch should still sync.
            logger.warning("Brevo rejected member %s (%s): %s", member.pk, member.email, e.detail)
            _record_sync(member, status="cleaned", contact_id=member.brevo_contact_id or "")
        else:
            _record_error(club, e.detail)
            logger.error("Brevo sync failed for member %s (club %s): %s", member.pk, club.pk, e.detail)
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
    # 200/204 == updated an existing contact (no body). Reuse the stored id, or look it up once.
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
    try:
        client.request("DELETE", f"/contacts/{quote(member.email)}")
    except BrevoApiError as e:
        if e.status_code != 404:
            raise


def change_member_email(member, old_email):
    """Move a contact to the member's current email: delete the old contact, then re-sync.

    Brevo's update endpoint can't rename a contact's email, so we drop the stale contact and let
    sync_member recreate it under the new address (carrying all attributes and list membership).
    """
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
            logger.exception("Failed to delete old Brevo contact for member %s", member.pk)


# --- bulk / scope helpers --------------------------------------------------------------------


def backfill(club):
    """Queue a sync for every in-scope member after an initial connection (and nightly).

    Reuses the shared in_scope rule and the per-member task path so each contact's id/status is
    captured locally.
    """
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
