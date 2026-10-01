"""The library's tables: documents, their chunks, image transcriptions and readers' reports.

Here rather than in ``models.py`` for the same reason as :mod:`auctions.moderation_models`; the rest of
the app is named only as ``"auctions.Club"`` strings.

Files live under ``settings.DOCUMENT_ROOT``, outside ``mediafiles/``, because nginx serves all of
``/media/`` to anybody who has the URL. The only way to a document's file is a view that checks who
is asking.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models

#: The fixed topics a document can be filed under. Code, not a table: changing the list is a decision
#: about the whole site, and the model is told the list on every call.
TOPICS = (
    ("breeding", "Breeding and spawning"),
    ("bap_report", "Breeder award (BAP/HAP) report"),
    ("species_profile", "Species profile"),
    ("fish_health", "Fish health and disease"),
    ("food", "Foods and feeding"),
    ("food_cultures", "Live food cultures"),
    ("plants", "Aquatic plants"),
    ("aquascaping", "Aquascaping"),
    ("maintenance", "Tank maintenance"),
    ("water", "Water chemistry"),
    ("equipment", "Equipment and DIY"),
    ("fish_room", "Fish rooms"),
    ("collecting", "Collecting trips"),
    ("shows", "Shows and judging"),
    ("invertebrates", "Invertebrates"),
    ("marine", "Marine and reef"),
    ("ponds", "Ponds"),
    ("club_news", "Club news and history"),
)
TOPIC_LABELS = dict(TOPICS)


class DocumentStorage(FileSystemStorage):
    """``FileSystemStorage`` rooted at ``settings.DOCUMENT_ROOT``, read on every use so tests can move it."""

    @property
    def base_location(self):
        return settings.DOCUMENT_ROOT

    @property
    def location(self):
        return str(Path(self.base_location).resolve())


_storage = DocumentStorage()


def document_storage():
    """A callable, so migrations name this function rather than freezing a path."""
    return _storage


def document_upload_to(instance, filename):
    """A random name: the person's file name is kept in ``original_name``, never in a path."""
    extension = Path(filename).suffix.lower()[:10]
    return f"{uuid.uuid4().hex}{extension}"


class Visibility(models.TextChoices):
    """Who can read a document, or what a batch's documents will be."""

    PUBLIC = "public", "Everyone on the site"
    CLUB = "club", "Members of its club"
    PRIVATE = "private", "Only me"


class DocumentBatch(models.Model):
    """A stack of page pictures (and scanned PDFs) put back together into articles.

    An archive arrives as a pile: most pictures hold part of an article, some hold the end of one and
    the start of the next. :mod:`auctions.documents.stitch` reads every page, then decides which
    paragraphs belong to which article, and makes one :class:`Document` per article.
    """

    WAITING = "waiting"
    READING = "reading"
    STITCHING = "stitching"
    DONE = "done"
    FAILED = "failed"
    STATUSES = (
        (WAITING, "Waiting for more pages"),
        (READING, "Reading the pages"),
        (STITCHING, "Putting the articles together"),
        (DONE, "Done"),
        (FAILED, "Couldn't be finished"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="document_batches"
    )
    club = models.ForeignKey(
        "auctions.Club", null=True, blank=True, on_delete=models.CASCADE, related_name="document_batches"
    )
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC)
    name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default=READING, db_index=True)
    error = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    #: Which pages are settled, and the articles found so far: ``stitch`` resumes from here.
    stitched_through = models.PositiveIntegerField(default=0)
    articles = models.JSONField(default=dict, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or f"Batch {self.pk}"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("document_batch", kwargs={"pk": self.pk})


class BatchPage(models.Model):
    """One page of a batch: a picture, or one page of an uploaded PDF (``pdf_page``, from 0).

    A PDF's pages share its file; ``image`` is the page rendered when it was read, so it can be shown.
    ``position`` is the page's place once the batch is put in order, which page markers refer to.
    """

    batch = models.ForeignKey(DocumentBatch, on_delete=models.CASCADE, related_name="pages")
    file = models.FileField(storage=document_storage, upload_to=document_upload_to, max_length=255)
    pdf_page = models.PositiveIntegerField(null=True, blank=True)
    image = models.FileField(storage=document_storage, upload_to=document_upload_to, max_length=255, blank=True)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, blank=True)
    position = models.PositiveIntegerField(null=True, blank=True)
    read = models.BooleanField(default=False, db_index=True)
    text = models.TextField(blank=True)
    read_with = models.CharField(max_length=100, blank=True)
    #: The article key (or "skip") for each paragraph of ``text``, once stitched.
    assignments = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.original_name}" + (f" p{self.pdf_page + 1}" if self.pdf_page is not None else "")


class Document(models.Model):
    """One article, report or file, and everything read out of it.

    ``visibility`` says who can read it; ``club`` is whose library it is in, and for "club" visibility
    whose members can read it. ``owner`` survives account deletion as ``NULL`` on a club's document; a
    document in no club is deleted with the account. A document put together from a batch has no file
    of its own: its scans are ``pages``.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    STATUSES = (
        (PENDING, "Waiting to be read"),
        (PROCESSING, "Being read"),
        (READY, "Ready"),
        (FAILED, "Couldn't be read"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    club = models.ForeignKey("auctions.Club", null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC)
    batch = models.ForeignKey(DocumentBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    pages = models.ManyToManyField(BatchPage, blank=True, related_name="documents")
    title = models.CharField(max_length=300, blank=True)
    author = models.CharField(max_length=200, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Year it was written")
    file = models.FileField(storage=document_storage, upload_to=document_upload_to, max_length=255, blank=True)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUSES, default=PENDING, db_index=True)
    error = models.TextField(blank=True)
    notes = models.TextField(blank=True, help_text="What reading the file left out, said plainly.")
    text = models.TextField(blank=True, help_text="Markdown read out of the file.")
    text_edited = models.BooleanField(
        default=False, help_text="Somebody corrected the text by hand, so re-indexing never re-reads the file."
    )
    parser_version = models.CharField(max_length=40, blank=True)
    chunker_version = models.CharField(max_length=20, blank=True)
    read_with = models.CharField(max_length=100, blank=True, help_text="The model that read its pictures, if any.")
    #: Fields a model filled in rather than a person, and which model: a better one may redo them.
    auto_fields = models.JSONField(default=list, blank=True)
    described_with = models.CharField(max_length=100, blank=True)
    topics = models.JSONField(default=list, blank=True)
    species = models.ManyToManyField("auctions.Species", blank=True, related_name="documents")
    removed = models.BooleanField(
        default=False, help_text="Hidden from everyone, e.g. after a copyright notice. The file is kept."
    )
    removed_reason = models.CharField(max_length=300, blank=True)
    createdon = models.DateTimeField(auto_now_add=True, db_index=True)
    indexed_on = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(
        default=0, help_text="Times the reader died on this without finishing; three and it has failed."
    )

    def __str__(self):
        return self.display_title

    @property
    def display_title(self):
        return self.title or self.original_name

    @property
    def topic_labels(self):
        return [TOPIC_LABELS[slug] for slug in self.topics or [] if slug in TOPIC_LABELS]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("document_detail", kwargs={"pk": self.pk})


class DocumentChunk(models.Model):
    """A passage of :attr:`Document.text`, ``text[start:end]``, and its vector.

    ``embedding`` is unit-length float32 bytes; ``embedding_model`` is ``"<model>@<dimensions>"``, so
    vectors from two models are never compared. ``content_hash`` is what was embedded, so a re-chunk that
    produces the same passage keeps its vector.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    heading = models.CharField(max_length=500, blank=True)
    page = models.PositiveIntegerField(null=True, blank=True)
    text = models.TextField()
    start = models.PositiveIntegerField(default=0)
    end = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64, db_index=True)
    embedding = models.BinaryField(null=True, blank=True)
    embedding_model = models.CharField(max_length=100, blank=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["document", "position"], name="docchunk_doc_position")]

    def __str__(self):
        return f"{self.document_id}#{self.position}"


class DocumentImageText(models.Model):
    """What the vision model read off one picture in one document, so reading it again never pays twice.

    Per document rather than shared by hash, so it goes when the document does: it is the document's
    text, and somebody's private papers shouldn't outlive their deletion as a cache.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="image_texts")
    sha256 = models.CharField(max_length=64)
    model = models.CharField(max_length=100)
    text = models.TextField(blank=True)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "sha256", "model"], name="one_reading_per_picture_model")
        ]

    def __str__(self):
        return f"{self.sha256[:12]} by {self.model}"


class DocumentFeedback(models.Model):
    """A reader saying something is wrong with a document. Its owner or club admins see these."""

    REASONS = (
        ("unreadable", "The text is garbled or missing"),
        ("species", "Wrong species"),
        ("topics", "Wrong topics"),
        ("details", "Wrong title, author or year"),
        ("duplicate", "A duplicate of another document"),
        ("offensive", "Offensive, or doesn't belong here"),
        ("other", "Something else"),
    )

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="feedback")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.CharField(max_length=20, choices=REASONS)
    note = models.TextField(max_length=2000, blank=True)
    resolved = models.BooleanField(default=False, db_index=True)
    createdon = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_reason_display()} on {self.document_id}"
