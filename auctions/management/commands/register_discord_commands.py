import requests
from django.conf import settings
from django.core.management.base import BaseCommand


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
        payload = {
            "name": "connect",
            "description": "Connect this Discord server to a club",
            "options": [
                {
                    "name": "club_uuid",
                    "description": "The UUID of the club to link to this server",
                    "type": 3,  # STRING
                    "required": True,
                }
            ],
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code in (200, 201):
            self.stdout.write(self.style.SUCCESS("Successfully registered /connect command."))
        else:
            self.stderr.write(f"Failed to register command: {resp.status_code} {resp.text}")
