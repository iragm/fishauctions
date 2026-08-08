"""Backfill speaker locations that the NEC WordPress export didn't carry.

    manage.py geocode_speakers --dry-run          # see what it would find, spend a few tokens
    manage.py geocode_speakers --limit 25         # do 25 for real
    manage.py geocode_speakers                    # the rest

Two steps per speaker: ask the language model where the person is based *according to their
own bio*, then geocode that place name with the Google Geocoding API.  Both are needed --
the bios name plenty of places, but almost all of them are talk subjects ("Collecting in
Mexico", "Long Island, NY: An Unlikely Hotspot") rather than where the speaker lives, and a
regex has no way to tell those apart.  The prompt is built around exactly that distinction
and the model is told to return nothing when it isn't sure, which is why `--dry-run` and a
review pass are worth doing before letting it write.

Speakers who already have coordinates are skipped, so this is safe to re-run and will never
move a pin somebody placed by hand.
"""

import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from auctions.llm import LLMError, get_provider
from auctions.models import LLMUsage, Speaker

SYSTEM_PROMPT = """You extract the home base of aquarium-hobby speakers from their biography.

You are given a speaker's name and biography. Reply with JSON only:

  {"location": "Denville, NJ, USA", "confidence": "high"}

Rules:
- "location" is where this person LIVES or is BASED, suitable for a geocoder. Include the
  country. Town + state/region is ideal; a country alone is acceptable when that is all the
  bio supports.
- A place the speaker collected fish in, travelled to, gives a talk about, or once visited is
  NOT their home base. Bios are full of these. Ignore them.
- A shop, fish room, club, university or employer the speaker runs or works at IS a strong
  signal of where they are based. Use its location.
- If the bio does not support a confident answer, reply {"location": "", "confidence": "none"}.
  Returning nothing is the correct answer far more often than guessing.
- "confidence" is one of "high", "medium", or "none".

Reply with the JSON object and nothing else."""

# Bios run to ~4600 characters; the home-base signal is almost always in the first part or the
# last line, but truncation risks cutting the signal, so send the whole thing and cap tokens.
MAX_BIO_CHARACTERS = 6000

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


class Command(BaseCommand):
    help = "Use the configured LLM to read a home location out of each speaker's bio, then geocode it."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report findings without saving.")
        parser.add_argument("--limit", type=int, default=0, help="Only process this many speakers.")
        parser.add_argument(
            "--min-confidence",
            choices=["high", "medium"],
            default="medium",
            help="Skip answers below this confidence (default: medium).",
        )
        parser.add_argument(
            "--include-located",
            action="store_true",
            help="Also re-examine speakers that already have a location. Off by default.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Seconds to wait between speakers, if you're being rate limited.",
        )

    def handle(self, *args, **options):
        provider = get_provider()
        if not provider.is_configured():
            self.stderr.write(
                self.style.ERROR(
                    "No LLM provider is configured. Set LLM_PROVIDER / LLM_MODEL and the API key in .env "
                    "(see .env.example) before running this."
                )
            )
            return

        geocode_key = getattr(settings, "GOOGLE_MAPS_SERVER_API_KEY", "")
        dry_run = options["dry_run"]
        if not geocode_key and not dry_run:
            self.stderr.write(
                self.style.ERROR(
                    "GOOGLE_MAPS_SERVER_API_KEY is not set, so nothing could be turned into coordinates. "
                    "Set it, or run with --dry-run to see what the model finds."
                )
            )
            return

        queryset = Speaker.objects.filter(is_deleted=False).exclude(bio="")
        if not options["include_located"]:
            queryset = queryset.filter(latitude__isnull=True, longitude__isnull=True)
        queryset = queryset.order_by("name")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        total = queryset.count()
        if not total:
            self.stdout.write("No speakers need a location.")
            return

        wanted = {"high"} if options["min_confidence"] == "high" else {"high", "medium"}
        self.stdout.write(f"Examining {total} speakers with the {provider.model or provider.name} model.")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written."))

        found = geocoded = skipped = failed = 0
        for speaker in queryset:
            answer = self._ask_model(provider, speaker)
            if answer is None:
                failed += 1
                continue
            location = (answer.get("location") or "").strip()
            confidence = (answer.get("confidence") or "none").strip().lower()
            if not location or confidence not in wanted:
                skipped += 1
                self.stdout.write(f"  {speaker.name:40.40}  —  (no confident answer)")
                continue

            found += 1
            if dry_run:
                self.stdout.write(f"  {speaker.name:40.40}  →  {location}  ({confidence})")
            else:
                coordinates = self._geocode(location, geocode_key)
                if not coordinates:
                    self.stdout.write(
                        self.style.WARNING(f"  {speaker.name:40.40}  →  {location}  (geocoder found nothing)")
                    )
                else:
                    latitude, longitude = coordinates
                    # update() rather than save(): the post_save geocode signal would otherwise
                    # queue a second, redundant lookup for the location we just wrote.
                    Speaker.objects.filter(pk=speaker.pk).update(
                        location=location, latitude=latitude, longitude=longitude
                    )
                    geocoded += 1
                    self.stdout.write(f"  {speaker.name:40.40}  →  {location}  ({latitude:.4f}, {longitude:.4f})")
            if options["sleep"]:
                time.sleep(options["sleep"])

        summary = f"{found} locations found, {skipped} left alone, {failed} model errors."
        if not dry_run:
            summary += f" {geocoded} speakers now have coordinates."
        self.stdout.write(self.style.SUCCESS(summary))

    def _ask_model(self, provider, speaker):
        """Ask the provider for this speaker's home base.  Returns a dict, or None on failure."""
        content = json.dumps(
            {"name": speaker.name, "biography": speaker.bio[:MAX_BIO_CHARACTERS]},
            ensure_ascii=False,
        )
        try:
            result = provider.complete_json(SYSTEM_PROMPT, [{"role": "user", "content": content}])
        except LLMError as error:
            self.stderr.write(self.style.WARNING(f"  {speaker.name}: {error}"))
            return None
        # Same accounting the command palette uses, so this shows up in the usual token totals
        # rather than being an invisible line on the bill.
        LLMUsage.objects.create(
            user=None,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            query=f"speaker location: {speaker.name}"[:600],
            response_kind="speaker_geocode",
            success=True,
        )
        return result.data if isinstance(result.data, dict) else None

    def _geocode(self, location, api_key):
        """Turn a place name into (latitude, longitude), or None."""
        try:
            response = requests.get(GEOCODE_URL, params={"address": location, "key": api_key}, timeout=10)
            response.raise_for_status()
        except requests.RequestException as error:
            self.stderr.write(self.style.WARNING(f"  geocoding '{location}' failed: {error}"))
            return None
        data = response.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        point = data["results"][0]["geometry"]["location"]
        return point["lat"], point["lng"]
