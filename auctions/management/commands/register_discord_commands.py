import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

# Discord's bitfield for MANAGE_GUILD. The three setup commands are for whoever runs the
# server; /membership, /bap and /join are for every member in it.
MANAGE_GUILD = "32"

# The list is the whole catalogue on purpose: it is registered with one bulk overwrite, so a
# command deleted from here is a command Discord stops offering. Every name in it has a branch
# in DiscordInteractionsView.post, and the two lists have to stay in step -- a command Discord
# knows about and the view doesn't answers "Unknown command" to whoever ran it.
COMMANDS = [
    {
        "name": "connect",
        "description": "Connect this Discord server to a club",
        "default_member_permissions": MANAGE_GUILD,
        "options": [
            {
                "name": "club_uuid",
                "description": "The UUID of the club to link to this server",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    },
    {
        "name": "auctions_here",
        "description": "Set this channel to receive auction announcements for this club",
        "default_member_permissions": MANAGE_GUILD,
    },
    {
        "name": "announcements_here",
        "description": "Set this channel to receive club announcements",
        "default_member_permissions": MANAGE_GUILD,
    },
    {
        "name": "membership",
        "description": "Check your membership status and expiration date",
    },
    {
        "name": "bap",
        "description": "Check your Breeder Award Program points and ranking",
    },
    {
        "name": "join",
        "description": "Join this club",
    },
]

RATE_LIMIT_RETRIES = 3
MAX_SLEEP_SECONDS = 30


class Command(BaseCommand):
    help = "Register Discord slash commands with the Discord API"

    def handle(self, *args, **options):
        application_id = getattr(settings, "DISCORD_BOT_CLIENT_ID", "")
        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")

        if not application_id:
            self.stderr.write("DISCORD_BOT_CLIENT_ID is not configured.")
            return
        if not bot_token:
            self.stderr.write("DISCORD_BOT_TOKEN is not configured.")
            return

        url = f"https://discord.com/api/v10/applications/{application_id}/commands"
        headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

        # One PUT rather than six POSTs. Creating commands one at a time is rate limited, and the
        # sixth request is the one that gets the 429 -- which used to leave the application with
        # five of the six commands and an error nobody reruns. A bulk overwrite is one request,
        # and it is idempotent: it is the same call whether these commands exist already or not.
        resp = self._send(url, headers)
        if resp is None:
            return

        if resp.status_code in (200, 201):
            registered = self._body(resp)
            names = (
                [c.get("name", "?") for c in registered]
                if isinstance(registered, list)
                else [c["name"] for c in COMMANDS]
            )
            self.stdout.write(
                self.style.SUCCESS(f"Registered {len(names)} commands: " + ", ".join(f"/{n}" for n in names))
            )
        else:
            self.stderr.write(f"Failed to register commands: {resp.status_code} {resp.text}")

    def _send(self, url, headers):
        """PUT the catalogue, waiting out a 429 rather than reporting it as a failure.

        Discord says how long to wait, in seconds, in the body and in the Retry-After header;
        neither is worth guessing at, so a response carrying neither is treated as a failure.
        """
        for attempt in range(RATE_LIMIT_RETRIES):
            try:
                resp = requests.put(url, headers=headers, json=COMMANDS, timeout=30)
            except requests.RequestException as e:
                self.stderr.write(f"Could not reach Discord: {e}")
                return None

            if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES - 1:
                return resp

            wait = self._retry_after(resp)
            if wait is None:
                return resp
            self.stdout.write(f"Rate limited by Discord, waiting {wait:.1f}s...")
            time.sleep(wait)

        return resp

    def _retry_after(self, resp):
        """How long Discord says to wait, in seconds, or None if it didn't say.

        It answers in the body and in the Retry-After header; neither is worth guessing at, so a
        429 carrying neither is handed back as the failure it is rather than slept on blindly.
        """
        body = self._body(resp)
        candidates = [body.get("retry_after") if isinstance(body, dict) else None, resp.headers.get("Retry-After")]
        for value in candidates:
            try:
                return min(float(value) + 0.5, MAX_SLEEP_SECONDS)
            except (TypeError, ValueError):
                continue
        return None

    def _body(self, resp):
        try:
            return resp.json()
        except ValueError:
            return None
