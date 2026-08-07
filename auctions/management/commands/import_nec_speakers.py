"""Import the Northeast Council's speaker database from a WordPress WXR export.

    manage.py import_nec_speakers nec.WordPress.2026-08-03.xml

The export is a WordPress eXtended RSS file containing `speaker` posts, an `attachment`
post per uploaded photo, and a `speaker_topics` taxonomy.  Everything is matched on
`wp:post_id`, so re-running the command updates the rows it created last time instead of
duplicating them.

Two things the export does *not* contain, which is why the importer looks thinner than you
might expect: there are no coordinates or addresses (one bio in 405 says where the speaker
lives), and the "Programs:" talk list was flattened to a single run-on string with no
delimiter when WordPress stripped the HTML, so it is stored as one text field rather than
being split into rows.  `manage.py geocode_speakers` fills in locations afterwards.
"""

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from auctions.models import Speaker, SpeakerTopic

NAMESPACES = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# The export has three spellings of cichlids and two of africa.  A canonical SpeakerTopic
# list is only worth having if the import actually merges them, so map the typos here.
# Keys are compared casefolded against the raw category name from the XML.
TOPIC_ALIASES = {
    "cichids": "Cichlids",
    "cichlids": "Cichlids",
    "africa": "African",
    "african": "African",
    "diy (do it yourself)": "DIY",
    "other non-fish species talk": "Other non-fish species",
    "moving with fish": "Moving with fish",
}

# WordPress writes the talk list as "...bio text... Programs: Talk One Talk Two".  Only the
# split point is recoverable; the individual titles are not.
PROGRAMS_RE = re.compile(r"\bPrograms?:\s*(.*)$", re.DOTALL)

# Non-breaking spaces are all over the pasted bios.
WHITESPACE_RE = re.compile(r"[\s ]+")

IMAGE_TIMEOUT_SECONDS = 30


def clean_text(value):
    """Collapse the runs of nbsp/newlines the WordPress editor left behind, and unescape.

    The unescape is not redundant with XML parsing: every value in this export is wrapped in
    CDATA, where `&amp;` is literal text the parser hands back untouched.  Without this,
    topics import as "Reef &amp; Brackish".
    """
    if not value:
        return ""
    return WHITESPACE_RE.sub(" ", html.unescape(value)).strip()


def canonical_topic_name(raw_name):
    """Fold the export's duplicate spellings onto one name, and title-case the rest.

    Returns None for a name that is only punctuation, so junk categories are skipped.
    """
    cleaned = clean_text(raw_name)
    if not cleaned:
        return None
    alias = TOPIC_ALIASES.get(cleaned.casefold())
    if alias:
        return alias
    # "Freshwater species" and "US Native Fish" are both in the export; leave anything that
    # already has an uppercase letter past the first alone rather than mangling acronyms.
    if cleaned.isupper() or cleaned.islower():
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned


class Command(BaseCommand):
    help = "Import speakers from a WordPress WXR export of the NEC website."

    def add_arguments(self, parser):
        parser.add_argument("xml_file", help="Path to the WordPress export (.xml)")
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Don't download speaker photos from the old site.",
        )
        parser.add_argument(
            "--replace-images",
            action="store_true",
            help="Re-download photos for speakers that already have one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing anything.",
        )

    def handle(self, *args, **options):
        path = Path(options["xml_file"])
        if not path.exists():
            msg = f"{path} does not exist"
            raise CommandError(msg)

        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            msg = f"{path} is not valid XML: {error}"
            raise CommandError(msg) from error

        channel = root.find("channel")
        if channel is None:
            msg = f"{path} has no <channel>; is this a WordPress export?"
            raise CommandError(msg)

        speakers, attachments_by_parent, attachments_by_id = self._partition_items(channel)
        if not speakers:
            msg = "No <item> with wp:post_type of 'speaker' found in this export."
            raise CommandError(msg)

        self.dry_run = options["dry_run"]
        if self.dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written."))

        self.stdout.write(f"Found {len(speakers)} speakers and {len(attachments_by_id)} attachments.")

        created = updated = 0
        image_results = {"downloaded": 0, "skipped": 0, "failed": 0}
        for item in speakers:
            was_created = self._import_speaker(
                item,
                attachments_by_parent,
                attachments_by_id,
                skip_images=options["skip_images"],
                replace_images=options["replace_images"],
                image_results=image_results,
            )
            if was_created is None:
                continue
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{created} speakers created, {updated} updated. "
                f"Photos: {image_results['downloaded']} downloaded, "
                f"{image_results['skipped']} skipped, {image_results['failed']} failed."
            )
        )
        if not self.dry_run:
            # Topics only ever come into existence attached to a speaker, so one with no
            # speakers left is debris — either a name this run merged away, or the last
            # speaker using it was retagged.  Dropping them keeps the topic filter honest.
            orphans = SpeakerTopic.objects.filter(speakers__isnull=True)
            orphan_count = orphans.count()
            if orphan_count:
                orphans.delete()
                self.stdout.write(f"Removed {orphan_count} topics that no longer have any speakers.")
            self.stdout.write(f"{SpeakerTopic.objects.count()} topics in the shared topic list.")

    def _partition_items(self, channel):
        """Split <item> elements into speakers and attachments, indexed the two ways we need.

        A photo is linked to its speaker by `wp:post_parent` on the attachment and by a
        `_thumbnail_id` postmeta on the speaker.  32 of 33 attachments have both; index both
        so a row with only one of them still gets its picture.
        """
        speakers = []
        attachments_by_parent = {}
        attachments_by_id = {}
        for item in channel.findall("item"):
            post_type = self._text(item, "wp:post_type")
            if post_type == "speaker":
                speakers.append(item)
            elif post_type == "attachment":
                url = self._text(item, "wp:attachment_url")
                if not url:
                    continue
                post_id = self._int(item, "wp:post_id")
                parent_id = self._int(item, "wp:post_parent")
                if post_id:
                    attachments_by_id[post_id] = url
                if parent_id:
                    attachments_by_parent.setdefault(parent_id, url)
        return speakers, attachments_by_parent, attachments_by_id

    def _import_speaker(
        self, item, attachments_by_parent, attachments_by_id, *, skip_images, replace_images, image_results
    ):
        """Create or update one Speaker.  Returns True if created, False if updated, None if skipped."""
        post_id = self._int(item, "wp:post_id")
        name = clean_text(self._text(item, "title"))
        if not name:
            self.stderr.write(self.style.WARNING(f"Skipping post {post_id}: no title"))
            return None
        if self._text(item, "wp:status") != "publish":
            return None

        body = clean_text(self._findtext(item, "content:encoded"))
        bio, programs = self._split_bio_and_programs(body)

        fields = {
            "name": name,
            "bio": bio,
            "programs": programs,
            "source_url": self._findtext(item, "link") or "",
            "imported_from_nec": True,
            # Everything here came out of the NEC's own database, so it stays NEC-only
            # regardless of what the add-speaker form defaults to.
            "nec_only": True,
        }

        if self.dry_run:
            exists = Speaker.objects.filter(wordpress_post_id=post_id).exists()
            topics = [canonical_topic_name(raw) for raw in self._topic_names(item)]
            self.stdout.write(
                f"  {'update' if exists else 'create'} {name} "
                f"({len([t for t in topics if t])} topics, {'photo' if self._photo_url(item, attachments_by_parent, attachments_by_id) else 'no photo'})"
            )
            return not exists

        with transaction.atomic():
            speaker, created = Speaker.objects.update_or_create(wordpress_post_id=post_id, defaults=fields)
            topics = []
            for raw_name in self._topic_names(item):
                canonical = canonical_topic_name(raw_name)
                if not canonical:
                    continue
                topic, _ = SpeakerTopic.objects.get_or_create(name=canonical)
                topics.append(topic)
            speaker.topics.set(topics)

        if not skip_images:
            self._attach_photo(
                speaker,
                self._photo_url(item, attachments_by_parent, attachments_by_id),
                replace_images=replace_images,
                image_results=image_results,
            )
        return created

    def _split_bio_and_programs(self, body):
        """Separate the bio from the trailing "Programs:" run-on list."""
        if not body:
            return "", ""
        match = PROGRAMS_RE.search(body)
        if not match:
            return body, ""
        return body[: match.start()].strip(), clean_text(match.group(1))

    def _topic_names(self, item):
        return [
            category.text
            for category in item.findall("category")
            if category.get("domain") == "speaker_topics" and category.text
        ]

    def _photo_url(self, item, attachments_by_parent, attachments_by_id):
        """The speaker's photo URL, preferring the explicit featured image."""
        thumbnail_id = self._postmeta(item, "_thumbnail_id")
        if thumbnail_id and thumbnail_id.isdigit():
            url = attachments_by_id.get(int(thumbnail_id))
            if url:
                return url
        return attachments_by_parent.get(self._int(item, "wp:post_id"))

    def _attach_photo(self, speaker, url, *, replace_images, image_results):
        if not url:
            return
        if speaker.image and not replace_images:
            image_results["skipped"] += 1
            return
        try:
            response = requests.get(url, timeout=IMAGE_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as error:
            image_results["failed"] += 1
            self.stderr.write(self.style.WARNING(f"  {speaker.name}: could not fetch {url} ({error})"))
            return
        if not response.content:
            image_results["failed"] += 1
            self.stderr.write(self.style.WARNING(f"  {speaker.name}: {url} was empty"))
            return
        filename = Path(url.split("?")[0]).name or f"speaker-{speaker.pk}.jpg"
        try:
            speaker.image.save(filename, ContentFile(response.content), save=True)
        except Exception as error:
            # The field resizes on save, so anything that isn't a readable image raises here --
            # a 200 that's really an HTML error page, a truncated file, an unsupported format.
            # One bad photo out of 405 must not take the whole import down with it; the speaker
            # is already saved, they just don't get a picture.
            image_results["failed"] += 1
            self.stderr.write(self.style.WARNING(f"  {speaker.name}: {url} isn't a usable image ({error})"))
            return
        image_results["downloaded"] += 1

    # --- small XML helpers -------------------------------------------------

    def _text(self, item, tag):
        """Text of a namespaced child element, or ''."""
        prefix, _, local = tag.partition(":")
        element = item.find(f"{{{NAMESPACES[prefix]}}}{local}") if local else item.find(tag)
        return (element.text or "").strip() if element is not None else ""

    def _findtext(self, item, tag):
        """Text of either a plain or namespaced child element, without stripping inner content."""
        if ":" in tag:
            return self._text(item, tag)
        element = item.find(tag)
        return (element.text or "") if element is not None else ""

    def _int(self, item, tag):
        value = self._text(item, tag)
        return int(value) if value.isdigit() else None

    def _postmeta(self, item, key):
        """Value of a wp:postmeta entry by key, or ''."""
        wp = NAMESPACES["wp"]
        for meta in item.findall(f"{{{wp}}}postmeta"):
            meta_key = meta.find(f"{{{wp}}}meta_key")
            if meta_key is not None and (meta_key.text or "").strip() == key:
                meta_value = meta.find(f"{{{wp}}}meta_value")
                return (meta_value.text or "").strip() if meta_value is not None else ""
        return ""
