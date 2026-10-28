"""Articles waiting for a person's AI agent to transcribe them, and saving what it read.

With ``DOCUMENT_SERVER_OCR`` off the site reads no pictures: a photographed page or a scanned PDF
becomes a document waiting to be transcribed, and a batch with any scanned page stops before it is put
together. A member's own agent works through them over ``/mcp/`` (``waiting_articles``,
``read_waiting_page``, ``save_transcription``), so the reading is paid for by whoever wants it done.

What it saves stays the uploader's and the club's: a document keeps its owner, club and visibility, and
a batch's articles take the batch's. Who may: whoever can read it and may edit it, the web's own rule
(:func:`.search.visible_documents` and :func:`.search.can_manage`), on every surface. The one wider
reach is the owner's (``reach``): a superuser on ``/mcp/admin/``, which only reads, and approving a
``propose_change`` there sees every club's waiting scans, never anyone else's "only me". That is how
the owner's daily routine transcribes members' scans: it reads them there and proposes each one.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.db.models import Q

from auctions.documents.extract import (
    MAX_PICTURES,
    MAX_TEXT_CHARS,
    PICTURE_EXTENSIONS,
    as_jpeg,
    frames_as_jpeg,
    parser_version,
)
from auctions.documents.models import Document, DocumentBatch, Visibility

_REFERENCE = re.compile(r"\s*(document|batch)\s*[#:\-]?\s*(\d+)\s*", re.IGNORECASE)


def owner_reach(request) -> bool:
    """Whether ``request`` carries the owner's wider reach: set by ``/mcp/admin/`` and by approving a
    proposal, never by the palette or ``/mcp/``.
    """
    return bool(getattr(request, "owner_reach", False)) and request.user.is_superuser


def _waiting(user, reach: bool = False):
    """``(documents, batches)`` waiting for ``user``'s agent: ones they can read and may edit, or with
    the owner's ``reach``, every one but somebody else's "only me".
    """
    from auctions.documents.search import clubs_to_file_under, visible_batches, visible_documents

    if reach:
        mine_or_shared = ~Q(visibility=Visibility.PRIVATE)
        if user is not None:
            mine_or_shared |= Q(owner=user)
        documents = Document.objects.filter(mine_or_shared, removed=False, status=Document.TRANSCRIBE)
        batches = DocumentBatch.objects.filter(mine_or_shared, status=DocumentBatch.TRANSCRIBE)
        return documents, batches
    documents = visible_documents(user).filter(status=Document.TRANSCRIBE)
    batches = visible_batches(user).filter(status=DocumentBatch.TRANSCRIBE)
    # can_manage: the uploader, or anyone who edits its club, which a superuser does for every club.
    clubs = Q(club__isnull=False) if user.is_superuser else Q(club__in=clubs_to_file_under(user))
    theirs = Q(owner=user) | clubs
    return documents.filter(theirs), batches.filter(theirs)


def waiting(user, club=None, reach: bool = False) -> list:
    """Documents and batches waiting for ``user``'s agent, oldest first."""
    documents, batches = _waiting(user, reach)
    if club is not None:
        documents, batches = documents.filter(club=club), batches.filter(club=club)
    items = [*documents.select_related("club").defer("text"), *batches.select_related("club")]
    return sorted(items, key=lambda item: (item.createdon, item.pk))


def waiting_count(user) -> int:
    documents, batches = _waiting(user)
    return documents.count() + batches.count()


def reference(item) -> str:
    """What the tools call an item: ``document 12`` or ``batch 5``."""
    return f"{'batch' if isinstance(item, DocumentBatch) else 'document'} {item.pk}"


def normalise(said) -> str:
    """``said`` as :func:`reference` writes it (``Batch #5`` -> ``batch 5``), or blank."""
    match = _REFERENCE.fullmatch(str(said or ""))
    return f"{match.group(1).lower()} {int(match.group(2))}" if match else ""


def find(user, said: str, reach: bool = False):
    """The waiting item ``said`` names, if ``user`` may transcribe it. ``(item, problem)``."""
    match = _REFERENCE.fullmatch(str(said or ""))
    if not match:
        return None, "Name it as waiting_articles does, e.g. “document 12” or “batch 5”."
    kind, pk = match.group(1).lower(), int(match.group(2))
    documents, batches = _waiting(user, reach)
    item = (batches if kind == "batch" else documents).select_related("club").filter(pk=pk).first()
    if item is not None:
        return item, None
    return None, f"There's no {kind} {pk} waiting for you to transcribe."


def _batch_pages(batch) -> list:
    return list(batch.pages.order_by("position", "pk"))


def _data(field) -> bytes:
    with field.open("rb") as handle:
        return handle.read()


def page_count(item) -> int:
    """How many pages ``item`` has to read: a batch's pages, a PDF's pages or a picture's frames."""
    if isinstance(item, DocumentBatch):
        return item.pages.count()
    data = _data(item.file)
    if Path(item.original_name).suffix.lower() in PICTURE_EXTENSIONS:
        # The frames page_picture can show: an icon-sized thumbnail frame isn't a page.
        return len(frames_as_jpeg(data)[0])
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return 0
    try:
        return len(pdf)
    finally:
        pdf.close()


def pages_done(item) -> list[int]:
    """A batch's page numbers already in an article this batch made; nothing for a document."""
    if not isinstance(item, DocumentBatch):
        return []
    used = set(item.documents.values_list("pages", flat=True))
    return [number for number, page in enumerate(_batch_pages(item), 1) if page.pk in used]


def page_picture(item, number: int) -> bytes | None:
    """Page ``number`` (from 1) of ``item`` as a bounded JPEG, or ``None`` if there is no such page or it
    won't open. Nothing is saved: listing and reading pages writes nothing.
    """
    import pypdfium2 as pdfium

    from auctions.documents.extract import ExtractionError
    from auctions.documents.stitch import _pdf_page

    if number < 1:
        return None
    try:
        if isinstance(item, DocumentBatch):
            pages = _batch_pages(item)
            if number > len(pages):
                return None
            page = pages[number - 1]
            if page.pdf_page is None:
                return as_jpeg(_data(page.file))
            if page.image:
                return as_jpeg(_data(page.image))
            return _pdf_page(_data(page.file), page.pdf_page)[0]
        data = _data(item.file)
        if Path(item.original_name).suffix.lower() in PICTURE_EXTENSIONS:
            frames = frames_as_jpeg(data, limit=MAX_PICTURES)[0]
            return frames[number - 1] if number <= len(frames) else None
        return _pdf_page(data, number - 1)[0]
    except (ExtractionError, pdfium.PdfiumError, IndexError, OSError):
        return None


def save(item, *, text: str, title: str = "", author: str = "", year=None, topics=None, pages=()) -> Document:
    """Save what an agent read. A document gets its text; a batch gets one more article, made of
    ``pages`` (numbers from 1), owned and filed as the batch is. Saving the same article of a batch
    again (same title, same pages) replaces its text rather than adding it twice. Queues indexing.
    """
    from auctions.documents import index

    text = text.strip()[:MAX_TEXT_CHARS]
    given = {"title": title.strip()[:300], "author": author.strip()[:200], "year": year}
    given = {name: value for name, value in given.items() if value}
    if isinstance(item, Document):
        document = item
        for name, value in given.items():
            setattr(document, name, value)
        if topics is not None:
            document.topics = topics
        document.text, document.text_edited, document.read_with = text, True, ""
        document.parser_version = parser_version()
        document.auto_fields = [name for name in document.auto_fields or [] if name not in given]
        document.save(
            update_fields=[
                *given,
                "topics",
                "text",
                "text_edited",
                "read_with",
                "parser_version",
                "auto_fields",
            ]
        )
        index.queue(document)
        return document
    rows = _batch_pages(item)
    used = [rows[number - 1] for number in dict.fromkeys(pages)]
    first, last = min(pages), max(pages)
    name = f"{item} pages {first}-{last}" if first != last else f"{item} page {first}"
    document = next(
        (
            each
            for each in item.documents.filter(title=given.get("title", ""), original_name=name)
            if {page.pk for page in each.pages.all()} == {page.pk for page in used}
        ),
        None,
    )
    if document is None:
        document = Document(owner=item.owner, club=item.club, visibility=item.visibility, batch=item)
    document.original_name = name
    document.title = given.get("title", "")
    document.author = given.get("author", "")
    document.year = given.get("year")
    document.topics = topics or []
    document.text, document.text_edited, document.read_with = text, True, ""
    document.parser_version = parser_version()
    document.auto_fields = []
    document.save()
    document.pages.set(used)
    index.queue(document)
    return document


def finish(batch) -> None:
    """Close a batch an agent has worked through: it is done, and says how many articles it made."""
    count = batch.documents.count()
    batch.status = DocumentBatch.DONE
    batch.notes = (
        f"{count} article{'s' if count != 1 else ''} from {batch.pages.count()} pages, transcribed by an AI agent."
    )
    batch.error = ""
    batch.save(update_fields=["status", "notes", "error"])
