"""Create the speaker directory's fixed topic vocabulary.

Called by ``ensure_site_defaults`` on every start, so the add-speaker form always has its
dropdown populated. Safe to re-run; it only ever adds what's missing.
"""

from django.core.management.base import BaseCommand

from auctions.speaker_topics import STARTER_TOPICS, ensure_speaker_topics


class Command(BaseCommand):
    help = "Create any missing speaker topics from the fixed vocabulary."

    def handle(self, *args, **options):
        created = ensure_speaker_topics()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created {created} speaker topics."))
        else:
            self.stdout.write(f"All {len(STARTER_TOPICS)} speaker topics already exist.")
