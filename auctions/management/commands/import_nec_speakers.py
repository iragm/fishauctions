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
from auctions.speaker_topics import STARTER_TOPICS, canonical_topic_name, ensure_speaker_topics

NAMESPACES = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# WordPress writes the talk list as "...bio text... Programs: Talk One Talk Two".  Only the
# split point is recoverable; the individual titles are not (see manage.py split_speaker_talks).
PROGRAMS_RE = re.compile(r"\bPrograms?:\s*(.*)$", re.DOTALL)

# Used to link an imported speaker to a site account. No bio in the NEC export has one.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Non-breaking spaces are all over the pasted bios.
WHITESPACE_RE = re.compile(r"[\s\u00a0]+")

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
            "--topics-only",
            action="store_true",
            help="Only re-apply topics to speakers already imported; leave every other field alone.",
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
        self.topics_only = options["topics_only"]
        if self.dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written."))
        if self.topics_only:
            self.stdout.write(self.style.WARNING("Topics only — no bios, photos or new speakers."))

        self.stdout.write(f"Found {len(speakers)} speakers and {len(attachments_by_id)} attachments.")
        if not self.dry_run:
            # Topics are a closed vocabulary; make sure it exists before mapping onto it.
            ensure_speaker_topics()

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

        if self.topics_only:
            self.stdout.write(self.style.SUCCESS(f"{updated} speakers re-tagged."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{created} speakers created, {updated} updated. "
                    f"Photos: {image_results['downloaded']} downloaded, "
                    f"{image_results['skipped']} skipped, {image_results['failed']} failed."
                )
            )
        if not self.dry_run:
            # Anything outside the fixed vocabulary with nobody left on it is debris from an
            # older import that mapped names differently. Vocabulary rows are kept even when
            # empty -- they have to stay in the add-speaker dropdown.
            orphans = SpeakerTopic.objects.filter(speakers__isnull=True).exclude(name__in=STARTER_TOPICS)
            orphan_count = orphans.count()
            if orphan_count:
                orphans.delete()
                self.stdout.write(f"Removed {orphan_count} topics that are no longer used.")
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

        if self.topics_only:
            return self._retag_speaker(post_id, name, item)

        body = clean_text(self._findtext(item, "content:encoded"))
        bio, programs = self._split_bio_and_programs(body)

        fields = {
            "name": name,
            "bio": bio,
            "programs": programs,
            "source_url": self._findtext(item, "link") or "",
            "imported_from_nec": True,
            # Everything here came out of the NEC's own database, so it stays NEC-only
            # Not settable from the add-speaker form: import and the Django admin only.
            "nec_only": True,
        }
        linked_user = self._user_for_email(self._speaker_email(item))
        if linked_user:
            fields["user"] = linked_user

        if self.dry_run:
            exists = Speaker.objects.filter(wordpress_post_id=post_id).exists()
            self.stdout.write(
                f"  {'update' if exists else 'create'} {name} "
                f"({len(self._canonical_topic_names(item))} topics, {'photo' if self._photo_url(item, attachments_by_parent, attachments_by_id) else 'no photo'})"
            )
            return not exists

        with transaction.atomic():
            speaker, created = Speaker.objects.update_or_create(wordpress_post_id=post_id, defaults=fields)
            speaker.topics.set(self._topics_for(item))

        if not skip_images:
            self._attach_photo(
                speaker,
                self._photo_url(item, attachments_by_parent, attachments_by_id),
                replace_images=replace_images,
                image_results=image_results,
            )
        return created

    def _retag_speaker(self, post_id, name, item):
        """Re-apply this speaker's topics from the export, touching nothing else.

        For when the vocabulary changes after an import: the export is the only record of what
        each speaker's subjects were, but re-running the whole import to recover them would
        overwrite bios and undo `split_speaker_talks`.  Returns False (an update) for a speaker
        that is already here, None for one that isn't -- this never creates a speaker.
        """
        speaker = Speaker.objects.filter(wordpress_post_id=post_id).first()
        if not speaker:
            return None
        if self.dry_run:
            topics = self._canonical_topic_names(item)
            self.stdout.write(f"  retag {name} ({', '.join(topics) if topics else 'no topics'})")
            return False
        speaker.topics.set(self._topics_for(item))
        return False

    def _canonical_topic_names(self, item):
        """This speaker's export categories as vocabulary names, dropped ones left out."""
        names = [canonical_topic_name(clean_text(raw)) for raw in self._topic_names(item)]
        return [name for name in names if name]

    def _topics_for(self, item):
        """The vocabulary rows to tag this speaker with."""
        topics = []
        for name in self._canonical_topic_names(item):
            # iexact, and create only as a backstop: ensure_speaker_topics() has already
            # made every vocabulary row, so this should always find one.
            topic = SpeakerTopic.objects.filter(name__iexact=name).first()
            if not topic:
                topic = SpeakerTopic.objects.create(name=name)
            topics.append(topic)
        return topics

    def _split_bio_and_programs(self, body):
        """Separate the bio from the trailing "Programs:" run-on list."""
        if not body:
            return "", ""
        match = PROGRAMS_RE.search(body)
        if not match:
            return body, ""
        return body[: match.start()].strip(), clean_text(match.group(1))

    def _speaker_email(self, item):
        """The speaker's email address, if the export carries one.

        This particular export has none in any of its 405 bios -- an email was simply not part
        of the old site's speaker record -- so this returns "" throughout that import.  It is
        here so the linking rule isn't missing the day an export does have them.
        """
        body = clean_text(self._findtext(item, "content:encoded"))
        match = EMAIL_RE.search(body)
        return match.group(0) if match else ""

    def _user_for_email(self, email):
        """The site account with this email address, if there is exactly one."""
        if not email:
            return None
        from django.contrib.auth.models import User

        matches = list(User.objects.filter(email__iexact=email)[:2])
        # Two accounts sharing an address is ambiguous, and linking to the wrong one would hand
        # somebody else's record to a stranger -- so link to neither.
        return matches[0] if len(matches) == 1 else None

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
