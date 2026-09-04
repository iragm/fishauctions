"""Discord: verifying its signatures, answering its interactions, and syncing roles.

``DiscordInteractionsView`` is the slash-command endpoint and must answer within three seconds, so
anything slow behind it is queued. The club-side configuration pages are at the bottom.
``InboundEmailRoutingView`` sits here as the other endpoint somebody else's system posts to.
"""

import json
import logging
import secrets
from datetime import datetime
from datetime import timezone as date_tz
from time import time

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models.base import Model as Model
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from auctions.models import (
    BapAward,
    Club,
    ClubDiscordRole,
    ClubHistory,
    ClubMember,
    Lot,
)
from auctions.services import (
    BAP_DECISIONS,
    review_lot_points,
)

from .base import ClubViewMixin, check_club_permission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbound email routing API
# ---------------------------------------------------------------------------


class InboundEmailRoutingView(APIView):
    """Resolve an inbound email address to its forwarding recipient.

    Called by the SES inbound Lambda to determine where to forward a message.
    Requires a shared secret supplied via the ``X-Routing-Secret`` header (must
    match the ``INBOUND_ROUTING_SECRET`` Django setting).

    GET /api/v1/email-routing/resolve/?address=<local_part_or_full_email>

    Returns:
        200 {"recipient": "user@example.com", "display_name": "Spring Auction 2024"}
        400 {"error": "address parameter is required"}
        401 {"error": "invalid or missing routing secret"}
        503 {"error": "email routing is not enabled"}
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from auctions.email_routing import email_routing_enabled, resolve_routing_info

        secret = (getattr(settings, "INBOUND_ROUTING_SECRET", "") or "").strip()
        provided = (request.META.get("HTTP_X_ROUTING_SECRET", "") or "").strip()
        if not secret or not provided or not secrets.compare_digest(provided, secret):
            return Response({"error": "invalid or missing routing secret"}, status=401)

        if not email_routing_enabled():
            return Response({"error": "email routing is not enabled"}, status=503)

        address = (request.query_params.get("address") or "").strip().lower()
        if not address:
            return Response({"error": "address parameter is required"}, status=400)

        # Accept either a bare local-part or a full email; extract local-part only.
        local_part = address.split("@")[0]
        info = resolve_routing_info(local_part)
        if info is None:
            return Response({"error": "no recipient found for this address"}, status=404)
        payload = {"recipient": info["recipient"], "display_name": info["display_name"]}
        # A donation alias has to say so.  The Lambda reads "kind" for two decisions -- post the
        # body back to /api/v1/email-routing/donation/ so it lands on the vendor's row, and treat
        # an empty recipient as "forward to nobody" rather than as a missing answer.  Whitelisting
        # only the two keys above silently turned both off: no vendor reply was ever recorded, and
        # a club with no donation contact had its vendors' replies forwarded to the site fallback
        # inbox, which is the one outcome SES.md says must not happen.
        for key in ("kind", "vendor_key"):
            if info.get(key):
                payload[key] = info[key]
        return Response(payload)


# ---------------------------------------------------------------------------
# Discord integration helpers and views
# ---------------------------------------------------------------------------

# Discord interaction type constants
_DISCORD_TYPE_PING = 1
_DISCORD_TYPE_APPLICATION_COMMAND = 2
_DISCORD_TYPE_COMPONENT = 3
_DISCORD_TYPE_CHANNEL_MESSAGE = 4
_DISCORD_TYPE_MODAL_SUBMIT = 5
_DISCORD_TYPE_MODAL = 9

# Discord component type constants
_DISCORD_COMPONENT_ACTION_ROW = 1
_DISCORD_COMPONENT_TEXT_INPUT = 4
_DISCORD_COMPONENT_BUTTON = 2

# Discord button styles
_DISCORD_BUTTON_STYLE_PRIMARY = 1

# Discord message flag: ephemeral (only visible to the user who triggered it)
_DISCORD_FLAG_EPHEMERAL = 64


def verify_discord_signature(public_key_hex, signature_hex, timestamp, body):
    """Verify a Discord interaction request signature using Ed25519.

    Returns True if the signature is valid, False otherwise.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        message = timestamp.encode() + (body if isinstance(body, bytes) else body.encode())
        key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def assign_discord_role(guild_id, user_id, role_id):
    """Assign a Discord role to a guild member via the Discord REST API.

    PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}
    Returns True on success (204 No Content), False otherwise.
    """
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token:
        logger.warning("DISCORD_BOT_TOKEN not configured – cannot assign Discord role")
        return False
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {bot_token}"}
    try:
        resp = requests.put(url, headers=headers, timeout=10)
        if resp.status_code == 204:
            return True
        logger.warning(
            "Discord role assignment failed: guild=%s user=%s role=%s status=%s response=%s",
            guild_id,
            user_id,
            role_id,
            resp.status_code,
            resp.text,
        )
        return False
    except requests.RequestException as exc:
        logger.exception("Error assigning Discord role: %s", exc)
        return False


def _discord_ephemeral(content):
    return JsonResponse(
        {"type": _DISCORD_TYPE_CHANNEL_MESSAGE, "data": {"content": content, "flags": _DISCORD_FLAG_EPHEMERAL}}
    )


_DISCORD_PERMISSION_MANAGE_GUILD = 1 << 5


def _has_discord_manage_guild(data):
    member_data = data.get("member") or {}
    try:
        return bool(int(member_data.get("permissions", "0")) & _DISCORD_PERMISSION_MANAGE_GUILD)
    except (ValueError, TypeError):
        return False


def _sync_discord_roles(club, bot_token):
    """Fetch roles from Discord and upsert ClubDiscordRole objects.

    Also fetches the bot's own member record to determine its highest role position.
    Roles at or above that position have bot_can_manage=False.

    Returns the number of roles synced, or None if the API call failed.
    """
    headers = {"Authorization": f"Bot {bot_token}"}
    guild_id = club.discord_server_id

    # Fetch all guild roles
    url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        logger.exception("Error fetching Discord roles: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("Discord roles fetch failed: status=%s response=%s", resp.status_code, resp.text)
        return None

    all_roles = resp.json()
    # Build a position lookup by role ID
    position_by_id = {r["id"]: r.get("position", 0) for r in all_roles}

    # Fetch the bot's own guild member to find its highest role position.
    # /members/@me only works with OAuth2 bearer tokens, not bot tokens.
    # Instead: resolve the bot's user ID first, then fetch its guild member by ID.
    bot_max_position = 0
    try:
        user_resp = requests.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=10)
        if user_resp.status_code == 200:
            bot_user_id = user_resp.json().get("id")
            if bot_user_id:
                member_resp = requests.get(
                    f"https://discord.com/api/v10/guilds/{guild_id}/members/{bot_user_id}",
                    headers=headers,
                    timeout=10,
                )
                if member_resp.status_code == 200:
                    bot_role_ids = member_resp.json().get("roles", [])
                    if bot_role_ids:
                        bot_max_position = max(position_by_id.get(rid, 0) for rid in bot_role_ids)
                    logger.info(
                        "Discord bot position resolved: user_id=%s bot_max_position=%s", bot_user_id, bot_max_position
                    )
                else:
                    logger.warning("Discord guild member fetch failed: status=%s", member_resp.status_code)
        else:
            logger.warning("Discord users/@me fetch failed: status=%s", user_resp.status_code)
    except requests.RequestException as exc:
        logger.exception("Error fetching bot's own member record: %s", exc)

    updated = 0
    fetched_role_ids = set()
    for role in all_roles:
        role_id = role.get("id", "")
        role_name = role.get("name", "")
        if role_id == guild_id or role.get("managed"):
            continue
        role_position = role.get("position", 0)
        can_manage = bot_max_position > role_position
        fetched_role_ids.add(role_id)
        obj = ClubDiscordRole.objects.filter(club=club, role_id=role_id).first()
        if obj:
            update_fields = []
            if obj.role_name != role_name:
                obj.role_name = role_name
                update_fields.append("role_name")
            if obj.bot_can_manage != can_manage:
                obj.bot_can_manage = can_manage
                update_fields.append("bot_can_manage")
            if update_fields:
                obj.save(update_fields=update_fields)
        else:
            ClubDiscordRole.objects.create(club=club, role_id=role_id, role_name=role_name, bot_can_manage=can_manage)
        updated += 1
    # Remove roles that no longer exist in Discord (only those with a non-empty role_id;
    # preserve placeholder rows without a Discord ID)
    ClubDiscordRole.objects.filter(club=club).exclude(role_id__in=fetched_role_ids).exclude(role_id="").delete()
    return updated


class DiscordInteractionsView(View):
    """Handle Discord interaction requests at /discord/interactions/.

    Supports:
      - Type 1 (PING)
      - Type 3 (component / button click) with custom_id=join_button
        (behaves like the /membership command: join modal if not joined,
        membership info + link if joined)
      - Type 5 (modal submit) with custom_id=join_modal
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        public_key = getattr(settings, "DISCORD_PUBLIC_KEY", "")
        if not public_key:
            logger.warning("DISCORD_PUBLIC_KEY not configured")
            return HttpResponseForbidden("Discord integration not configured")

        # Signature verification
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not signature or not timestamp:
            return HttpResponseBadRequest("Missing signature headers")
        try:
            if abs(time() - int(timestamp)) > 300:
                return HttpResponseForbidden("Stale request")
        except (ValueError, TypeError):
            return HttpResponseBadRequest("Invalid timestamp")
        body = request.body

        if not verify_discord_signature(public_key, signature, timestamp, body):
            return HttpResponseForbidden("Invalid request signature")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Invalid JSON")

        interaction_type = data.get("type")

        # Type 1 – PING (required for Discord endpoint verification)
        if interaction_type == _DISCORD_TYPE_PING:
            return JsonResponse({"type": _DISCORD_TYPE_PING})

        # Type 3 – Component interaction (button click)
        if interaction_type == _DISCORD_TYPE_COMPONENT:
            custom_id = data.get("data", {}).get("custom_id", "")
            if custom_id == "join_button":
                # The join button mirrors the /membership command: if the user
                # hasn't joined, show the join modal; if they have, show their
                # membership info and link.
                return self._handle_membership_command(data)
            return _discord_ephemeral("Unsupported interaction")

        # Type 2 – Application command (slash command)
        if interaction_type == _DISCORD_TYPE_APPLICATION_COMMAND:
            command_name = data.get("data", {}).get("name", "")
            if command_name == "connect":
                return self._handle_connect_command(data)
            if command_name == "auctions_here":
                return self._handle_auctions_here_command(data)
            if command_name == "announcements_here":
                return self._handle_announcements_here_command(data)
            if command_name == "membership":
                return self._handle_membership_command(data)
            if command_name == "bap":
                return self._handle_bap_command(data)
            if command_name == "join":
                return self._handle_join_command(data)
            return _discord_ephemeral("❌ Unknown command.")

        # Type 5 – Modal submit
        if interaction_type == _DISCORD_TYPE_MODAL_SUBMIT:
            custom_id = data.get("data", {}).get("custom_id", "")
            if custom_id == "join_modal":
                return self._handle_join_modal(data)
            return _discord_ephemeral("Unsupported interaction")

        return _discord_ephemeral("Unsupported interaction")

    def _join_modal_response(self):
        """Return a Discord modal response for joining the club."""
        return JsonResponse(
            {
                "type": _DISCORD_TYPE_MODAL,
                "data": {
                    "custom_id": "join_modal",
                    "title": "Enter your contact information",
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_TEXT_INPUT,
                                    "custom_id": "name",
                                    "label": "Full name",
                                    "style": 1,
                                    "placeholder": "John Smith",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_TEXT_INPUT,
                                    "custom_id": "email",
                                    "label": "Email address",
                                    "style": 1,
                                    "placeholder": "john@example.com",
                                    "required": True,
                                }
                            ],
                        },
                    ],
                },
            }
        )

    def _handle_join_command(self, data):
        guild_id = data.get("guild_id", "")
        if not guild_id or not Club.objects.filter(discord_server_id=guild_id).exists():
            return _discord_ephemeral("❌ No club is configured for this Discord server.")
        if self._already_joined(data):
            return _discord_ephemeral("✅ You've already joined!")
        return self._join_modal_response()

    def _already_joined(self, data):
        """Return True if the Discord user is already a member of the club for this server."""
        guild_id = data.get("guild_id", "")
        if not guild_id:
            return False
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")
        if not discord_id:
            return False
        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return False
        return ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).exists()

    def _handle_join_modal(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")
        discord_username = user_data.get("username", "") or user_data.get("global_name", "") or ""

        # Extract text inputs from modal components
        fields = {}
        for row in data.get("data", {}).get("components", []):
            for comp in row.get("components", []):
                fields[comp.get("custom_id", "")] = comp.get("value", "")

        # Accept either a single ``name`` field or ``first_name`` / ``last_name``.
        name = fields.get("name", "").strip()
        if not name:
            first_name = fields.get("first_name", "").strip()
            last_name = fields.get("last_name", "").strip()
            name = f"{first_name} {last_name}".strip()
        email = fields.get("email", "").strip()

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process your request. Please try again.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ No club is configured for this Discord server.")

        # Already registered with this Discord ID?
        existing = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if existing:
            return _discord_ephemeral("✅ You're already registered!")

        # Email match – link Discord ID and assign role
        if email:
            # note that we do not verify email anywhere
            # this means that anyone can claim any email address by entering it in the modal
            # under no circumstances should the club member expose any information,
            # not even name, to anyone who hasn't been specifically granted a role in the club
            # and anything on discord needs to reflect this, too
            # the user model has an email that can be assumed valid
            if len(email) < 5 or "@" not in email:
                return _discord_ephemeral("❌ Please enter a valid email address.")
            existing_by_email = ClubMember.objects.filter(club=club, email=email, is_deleted=False).first()
            if existing_by_email:
                if existing_by_email.discord_id and existing_by_email.discord_id != discord_id:
                    return _discord_ephemeral("❌ This email is already linked to another Discord account.")
                update_fields = ["discord_id"]
                existing_by_email.discord_id = discord_id
                if discord_username:
                    existing_by_email.discord_username = discord_username
                    update_fields.append("discord_username")
                existing_by_email.save(update_fields=update_fields)
                existing_by_email.maybe_assign_discord_role()
                ClubHistory.objects.create(
                    club=club,
                    user=None,
                    action=f"Discord account linked for {existing_by_email} (@{discord_username or discord_id})",
                    applies_to="MEMBERS",
                )
                return _discord_ephemeral("✅ You're in! Access unlocked.")

        # Create a new club member
        new_member = ClubMember(
            club=club,
            name=name,
            email=email or None,
            discord_id=discord_id,
            discord_username=discord_username or None,
            source="discord",
        )
        new_member.save()
        new_member.maybe_assign_discord_role()
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"New member added via Discord: {new_member} (@{discord_username or discord_id})",
            applies_to="MEMBERS",
        )
        return _discord_ephemeral("✅ You're in! Access unlocked.")

    def _handle_connect_command(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        caller_discord_id = user_data.get("id", "")
        options = {o["name"]: o["value"] for o in data.get("data", {}).get("options", [])}
        club_uuid = options.get("club_uuid", "").strip()

        if not _has_discord_manage_guild(data):
            return _discord_ephemeral("❌ You need the Manage Server permission to run this command.")

        if not guild_id or not club_uuid:
            return _discord_ephemeral("❌ Missing guild ID or club UUID.")

        try:
            club = Club.objects.get(uuid=club_uuid)
        except (Club.DoesNotExist, ValueError, ValidationError):
            return _discord_ephemeral("❌ This isn't a valid club connection code.")

        # Reject if another club already owns this guild
        if Club.objects.filter(discord_server_id=guild_id).exclude(pk=club.pk).exists():
            return _discord_ephemeral("❌ This Discord server is already connected to another club.")

        club.discord_server_id = guild_id
        club.save(update_fields=["discord_server_id"])

        caller_username = user_data.get("username") or caller_discord_id
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Discord server connected by @{caller_username} (Discord ID {caller_discord_id})",
            applies_to="SETTINGS",
        )

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        _sync_discord_roles(club, bot_token) if bot_token else None

        return JsonResponse(
            {
                "type": _DISCORD_TYPE_CHANNEL_MESSAGE,
                "data": {
                    "content": (f"Welcome to the **{club.name}**! Click the button below to register."),
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_ACTION_ROW,
                            "components": [
                                {
                                    "type": _DISCORD_COMPONENT_BUTTON,
                                    "custom_id": "join_button",
                                    "label": "Join our club",
                                    "style": _DISCORD_BUTTON_STYLE_PRIMARY,
                                }
                            ],
                        }
                    ],
                },
            }
        )

    def _handle_auctions_here_command(self, data):
        guild_id = data.get("guild_id", "")
        channel_id = data.get("channel_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        caller_discord_id = user_data.get("id", "")

        if not _has_discord_manage_guild(data):
            return _discord_ephemeral("❌ You need the Manage Server permission to run this command.")

        if not guild_id or not channel_id:
            return _discord_ephemeral("❌ Missing guild or channel ID.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club. Run /connect first.")

        club.auction_channel_id = channel_id
        club.save(update_fields=["auction_channel_id"])
        caller_username = user_data.get("username") or caller_discord_id
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Auction announcement channel set by @{caller_username} (Discord ID {caller_discord_id})",
            applies_to="SETTINGS",
        )
        return _discord_ephemeral("✅ Auction announcements will be posted in this channel.")

    def _handle_announcements_here_command(self, data):
        """/announcements_here — point this club's announcements at the channel it was run in.

        Deliberately a second channel rather than reusing ``auction_channel_id``: an auction
        announcement is news for everybody, and a club announcement is often for members only, so
        the two land in different rooms on most servers. Same shape and same permission bar as
        /auctions_here, and it writes to ClubHistory for the same reason — a channel that stops
        working six months later needs a record of who set it.
        """
        guild_id = data.get("guild_id", "")
        channel_id = data.get("channel_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        caller_discord_id = user_data.get("id", "")

        if not _has_discord_manage_guild(data):
            return _discord_ephemeral("❌ You need the Manage Server permission to run this command.")

        if not guild_id or not channel_id:
            return _discord_ephemeral("❌ Missing guild or channel ID.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club. Run /connect first.")

        club.announcement_channel_id = channel_id
        club.save(update_fields=["announcement_channel_id"])
        caller_username = user_data.get("username") or caller_discord_id
        ClubHistory.objects.create(
            club=club,
            user=None,
            action=f"Club announcement channel set by @{caller_username} (Discord ID {caller_discord_id})",
            applies_to="SETTINGS",
        )
        return _discord_ephemeral("✅ Club announcements will be posted in this channel.")

    def _handle_membership_command(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process this request.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club.")

        member = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if not member:
            return self._join_modal_response()

        lines = [f"**{club.name}** — Your membership"]
        lines.append(f"Member since: {member.createdon.strftime('%B %d, %Y')}")

        expiry = member.membership_expiration_date
        if not expiry:
            if club.membership_annual_fee:
                lines.append("Status: ❌ Expired — please renew your membership")
        else:
            today = timezone.now().date()
            expiry_ts = int(datetime.combine(expiry, datetime.min.time(), date_tz.utc).timestamp())
            if expiry >= today:
                lines.append(f"Status: ✅ Active — expires <t:{expiry_ts}:D>")
            else:
                lines.append(f"Status: ❌ Expired <t:{expiry_ts}:D> — please renew your membership")

        lines.append(f"\n[View your membership]({member.simple_membership_link})")

        return _discord_ephemeral("\n".join(lines))

    def _handle_bap_command(self, data):
        guild_id = data.get("guild_id", "")
        member_data = data.get("member") or {}
        user_data = member_data.get("user") or data.get("user") or {}
        discord_id = user_data.get("id", "")

        if not guild_id or not discord_id:
            return _discord_ephemeral("❌ Unable to process this request.")

        club = Club.objects.filter(discord_server_id=guild_id).first()
        if not club:
            return _discord_ephemeral("❌ This server is not connected to a club.")

        if not club.enable_breeder_award_program:
            return _discord_ephemeral("❌ This club does not use the Breeder Award Program.")

        member = ClubMember.objects.filter(club=club, discord_id=discord_id, is_deleted=False).first()
        if not member:
            return self._join_modal_response()

        lines = [f"**{club.name}** — Your points"]

        bap_rank = ClubMember.objects.filter(club=club, bap_points__gt=member.bap_points, is_deleted=False).count() + 1
        lines.append(f"BAP: {member.bap_points} pts (#{bap_rank}) — {member.bap_points_ytd} pts this year")

        if club.separate_hap:
            hap_rank = (
                ClubMember.objects.filter(club=club, hap_points__gt=member.hap_points, is_deleted=False).count() + 1
            )
            lines.append(f"HAP: {member.hap_points} pts (#{hap_rank}) — {member.hap_points_ytd} pts this year")

        if club.separate_cap:
            cap_rank = (
                ClubMember.objects.filter(club=club, culture_points__gt=member.culture_points, is_deleted=False).count()
                + 1
            )
            lines.append(
                f"Culture: {member.culture_points} pts (#{cap_rank}) — {member.culture_points_ytd} pts this year"
            )

        recent_awards = BapAward.objects.filter(club_member=member).order_by("-date", "-pk")[:5]
        if recent_awards:
            lines.append("\n**Recent awards:**")
            for award in recent_awards:
                lines.append(f"• {award.date} — {award}")

        return _discord_ephemeral("\n".join(lines))


class LotBapPointsView(LoginRequiredMixin, View):
    """Inline BAP approve/reject/undo endpoint for the Pending BAP table."""

    def _render_buttons(self, request, lot, club):
        lot.refresh_from_db()
        try:
            award = lot.bap_award
        except Exception:
            award = None
        lot.bap_award_cached = award
        return render(
            request,
            "auctions/bap_lot_buttons.html",
            # ``Lot.default_bap_points``, not a third opinion about it: this used to read the
            # category override and not the genus one, so approving a lot whose genus the club
            # values differently re-rendered the row with a number the table had never shown.
            {"lot": lot, "club": club, "default_points": lot.default_bap_points(club)},
        )

    def post(self, request, pk):
        lot = get_object_or_404(Lot, pk=pk, is_deleted=False, banned=False)
        club = lot.auction.club if lot.auction else None
        if not club or not check_club_permission(request.user, club, "permission_manage_bap"):
            return HttpResponse(status=403)

        # "reject" is what this page's buttons have always posted; the service calls it "deny",
        # which is the word somebody says out loud. Same decision either way.
        action = request.POST.get("action", "approve")
        decision = "deny" if action == "reject" else action
        if decision not in BAP_DECISIONS:
            return HttpResponse(status=400)

        def _parse_pts(key):
            try:
                return max(0, int(str(request.POST.get(key, 0)).strip() or 0))
            except (ValueError, TypeError):
                return 0

        review_lot_points(
            lot,
            club,
            acting_user=request.user,
            decision=decision,
            bap=_parse_pts("bap_points"),
            hap=_parse_pts("hap_points"),
            cap=_parse_pts("cap_points"),
        )
        return self._render_buttons(request, lot, club)


class ClubDiscordConfigView(LoginRequiredMixin, ClubViewMixin, View):
    """Full-page Discord settings for a club."""

    active_tab = "discord"

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return render(request, "auctions/club_discord_settings.html", self._context(request))

    def post(self, request, *args, **kwargs):
        club = self.club
        club.create_events_for_auctions = "create_events_for_auctions" in request.POST
        club.save(update_fields=["create_events_for_auctions"])
        if request.headers.get("HX-Request"):
            return HttpResponse(status=204)
        messages.success(request, "Discord event settings saved.")
        return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

    def _context(self, request):
        roles = ClubDiscordRole.objects.filter(club=self.club).order_by("role_name")
        client_id = getattr(settings, "DISCORD_BOT_CLIENT_ID", "")
        oauth_url = (
            f"https://discord.com/oauth2/authorize?client_id={client_id}"
            "&scope=bot%20applications.commands&permissions=2415921152"
            if client_id
            else ""
        )
        club_uuid = str(self.club.uuid)
        return {
            "club": self.club,
            "roles": roles,
            "oauth_url": oauth_url,
            "club_uuid": club_uuid,
            "view": self,
        }


class ClubDiscordFetchRolesView(LoginRequiredMixin, ClubViewMixin, View):
    """Fetch roles from the Discord API and save them to the database."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        club = self.club
        if not club.discord_server_id:
            messages.error(request, "Save a Discord server ID first.")
            return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        if not bot_token:
            messages.error(request, "DISCORD_BOT_TOKEN is not configured.")
            return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))

        updated = _sync_discord_roles(club, bot_token)
        if updated is None:
            messages.error(request, "Could not fetch roles from Discord. Check your bot token and server ID.")
        else:
            messages.success(request, f"Fetched {updated} role(s) from Discord.")
            from auctions.tasks import sync_discord_member_roles_for_club

            sync_discord_member_roles_for_club.delay(club.pk)
        return redirect(reverse("club_discord_config", kwargs={"slug": club.slug}))


class ClubDiscordEditRoleView(LoginRequiredMixin, ClubViewMixin, View):
    """Edit a single ClubDiscordRole's BAP/HAP thresholds and paid/unpaid flags."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        if not role.bot_can_manage:
            messages.error(
                request,
                f'"{role.role_name}" is at or above the bot\'s role in the Discord hierarchy. '
                "Move the bot's role above it in Discord before configuring it here.",
            )
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))
        return render(request, "auctions/club_discord_role_edit.html", {"club": self.club, "role": role})

    def post(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        if not role.bot_can_manage:
            messages.error(request, f'"{role.role_name}" cannot be edited — the bot\'s role is not above it.')
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))
        is_paid = "is_paid_role" in request.POST
        is_unpaid = "is_unpaid_role" in request.POST
        try:
            bap = max(0, int(request.POST.get("bap_points_for_role", 0)))
        except (TypeError, ValueError):
            bap = 0
        try:
            hap = max(0, int(request.POST.get("hap_points_for_role", 0)))
        except (TypeError, ValueError):
            hap = 0

        with transaction.atomic():
            # Enforce exclusivity: each club can have at most one paid and one unpaid role
            if is_paid:
                ClubDiscordRole.objects.filter(club=self.club, is_paid_role=True).exclude(pk=pk).update(
                    is_paid_role=False
                )
            if is_unpaid:
                ClubDiscordRole.objects.filter(club=self.club, is_unpaid_role=True).exclude(pk=pk).update(
                    is_unpaid_role=False
                )
            role.is_paid_role = is_paid
            role.is_unpaid_role = is_unpaid
            role.bap_points_for_role = bap
            role.hap_points_for_role = hap
            role.save(update_fields=["is_paid_role", "is_unpaid_role", "bap_points_for_role", "hap_points_for_role"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Updated Discord role '{role.role_name}' settings",
            applies_to="SETTINGS",
        )
        messages.success(request, f'Role "{role.role_name}" updated.')
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))


class ClubDiscordSetDefaultRoleView(LoginRequiredMixin, ClubViewMixin, View):
    """Set a ClubDiscordRole as the default for new Discord registrations."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, slug, pk, *args, **kwargs):
        role = get_object_or_404(ClubDiscordRole, pk=pk, club=self.club)
        # Clear any existing default for this club
        ClubDiscordRole.objects.filter(club=self.club, is_default=True).update(is_default=False)
        role.is_default = True
        role.save(update_fields=["is_default"])
        ClubHistory.objects.create(
            club=self.club,
            user=request.user,
            action=f"Set '{role.role_name}' as the default Discord role",
            applies_to="SETTINGS",
        )
        messages.success(request, f'"{role.role_name}" set as the default role.')
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))


class ClubDiscordSendJoinMessageView(LoginRequiredMixin, ClubViewMixin, View):
    """Send a welcome message with a join button to a Discord channel."""

    def dispatch(self, request, *args, **kwargs):
        self.get_club(kwargs.get("slug", ""))
        if request.user.is_authenticated and not self.user_has_club_permission("permission_edit_club"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        channel_id = request.POST.get("channel_id", "").strip()
        if not channel_id:
            messages.error(request, "Please enter a channel ID.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        if not bot_token:
            messages.error(request, "DISCORD_BOT_TOKEN is not configured.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
        payload = {
            "content": f"Welcome to **{self.club.name}**! Click the button below to register and get access to the server.",
            "components": [
                {
                    "type": _DISCORD_COMPONENT_ACTION_ROW,
                    "components": [
                        {
                            "type": _DISCORD_COMPONENT_BUTTON,
                            "custom_id": "join_button",
                            "label": "Join our club",
                            "style": _DISCORD_BUTTON_STYLE_PRIMARY,
                        }
                    ],
                }
            ],
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
        except requests.RequestException as exc:
            logger.exception("Error sending Discord join message: %s", exc)
            messages.error(request, "Network error while sending join message.")
            return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))

        if resp.status_code == 200 or resp.status_code == 201:  # Discord returns 200 or 201 depending on version
            messages.success(request, "Join message sent to the channel!")
        else:
            messages.error(request, f"Discord API error {resp.status_code}: could not send message.")
        return redirect(reverse("club_discord_config", kwargs={"slug": self.club.slug}))
