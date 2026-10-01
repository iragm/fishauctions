"""A file, as Markdown: ``markitdown`` for the formats, the site's vision model for the pictures.

Three paths, chosen by what the file is:

* **A picture** (a photographed or scanned page, a photo): every frame is read by the vision model.
* **A PDF with scanned pages** -- a page with almost no text layer, or one picture covering most of
  it (a scan with a bad OCR layer from the 1990s): pages are assembled one by one, scanned pages read
  by the vision model and the rest from their text layer, each under a ``<!-- page N -->`` marker.
* **Everything else**, including a PDF that is all text: ``markitdown``. Pictures inside Word and
  PowerPoint files, and inside zips, come back through :class:`_VisionClient` to the same reader.

Reading a picture never raises: one bad image costs its own text, not the document. Every answer is
kept in :class:`~auctions.models.DocumentImageText` by document, image hash and model, so re-reading a
document after a parser change pays only for pictures it hasn't read with that model.

Only local streams are converted -- ``convert_stream`` with a ``StreamInfo``, never ``convert()`` on
a string, which fetches URLs.
"""

from __future__ import annotations

import base64
import hashlib
import io
import itertools
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings

from auctions.llm import LLMError, get_provider

logger = logging.getLogger(__name__)

#: Bump when anything below changes what a file reads as; ``reindex_documents`` re-reads the rest.
PARSER_REVISION = 1

#: Pictures the vision model may read for one document, cached answers not counted. A club newsletter
#: is 10-30 pages; past this the rest of the document is left out and ``notes`` says so.
MAX_PICTURES = 150
#: A picture smaller than this on either side is an icon, a bullet or a rule.
MIN_PICTURE_SIDE = 100
#: Long side sent to the model. Enough for typewriter text; more costs tokens and reads no better.
MAX_PICTURE_SIDE = 2000
#: Pages are rendered at this resolution before reading.
RENDER_DPI = 200
#: A PDF page with less text than this is a picture of a page.
SCANNED_TEXT_CHARS = 40
#: ... and so is one with a single image covering this much of it, whatever its text layer says.
SCANNED_PICTURE_SHARE = 0.5
#: Text kept from one document. A 50,000-row spreadsheet is not something to search passage by passage.
MAX_TEXT_CHARS = 2_000_000
#: Zip containers (docx, xlsx, pptx, epub, zip) are opened whole by markitdown; refuse a bomb first.
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_BYTES = 300 * 1024 * 1024
#: One vision read; a dense page is slow.
VISION_TIMEOUT_SECONDS = 90.0
VISION_MAX_TOKENS = 8000

PICTURE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}

READ_PROMPT = (
    "This is a scanned page or a photograph, usually from an aquarium club's newsletter or a breeder's "
    "report, often decades old and poorly reproduced. Transcribe every word of text exactly as written, "
    "in reading order, as Markdown, keeping headings, paragraphs, lists and tables. Do not correct, "
    "summarise or translate; write [illegible] for words you cannot make out. Most pages are text only. "
    "Only if the image really contains a photograph or drawing, add one line per picture starting "
    "'Picture:' that describes it and names any fish, plant or animal you can identify; never describe "
    "the page itself, and never guess at a picture that isn't there. Reply with the Markdown and nothing else."
)


def parser_version() -> str:
    """What :attr:`Document.parser_version` is compared against."""
    return f"markitdown-{version('markitdown')}.{PARSER_REVISION}"


def document_model() -> str:
    """The library's model (``settings.DOCUMENT_MODEL``): reading, stitching, filing and answering."""
    return settings.DOCUMENT_MODEL or settings.LLM_MODEL


class ExtractionError(Exception):
    """The file can't be read at all. The message is shown to the person who uploaded it."""


@dataclass
class Extraction:
    text: str
    notes: list[str] = field(default_factory=list)
    #: The model that read its pictures; blank when it had none.
    read_with: str = ""


class Reader:
    """Reads pictures with the vision model within one document's allowance, and counts what happened.

    With a ``document``, answers are kept against it and reused; without one nothing is kept.
    """

    def __init__(self, document=None, allowance: int = MAX_PICTURES):
        self.document = document
        self.allowance = allowance
        self.asked = 0
        self.failed = 0
        self.skipped = 0
        #: Set once any picture is read, so the document records which model read it.
        self.model = ""

    def read(self, data: bytes) -> str:
        """The text in one picture, in any format Pillow opens. Empty when it can't or shouldn't be read."""
        jpeg = as_jpeg(data)
        return self.read_jpeg(jpeg) if jpeg else ""

    def read_jpeg(self, jpeg: bytes) -> str:
        from auctions.models import DocumentImageText

        digest = hashlib.sha256(jpeg).hexdigest()
        model = document_model()
        self.model = model
        kept = DocumentImageText.objects.filter(document=self.document, sha256=digest, model=model)
        cached = kept.first() if self.document is not None else None
        if cached:
            return cached.text
        if self.asked >= self.allowance:
            self.skipped += 1
            return ""
        self.asked += 1
        try:
            text = _ask(jpeg, model)
        except LLMError as error:
            logger.warning("Couldn't read a picture: %s", error)
            self.failed += 1
            return ""
        if self.document is not None:
            DocumentImageText.objects.get_or_create(
                document=self.document, sha256=digest, model=model, defaults={"text": text}
            )
        return text

    def notes(self) -> list[str]:
        said = []
        if self.skipped:
            said.append(f"{self.skipped} pictures or pages weren't read: over the limit of {self.allowance}.")
        if self.failed:
            said.append(f"{self.failed} pictures or pages couldn't be read; reading the document again retries them.")
        return said


def _ask(jpeg: bytes, model: str) -> str:
    """One vision call, inside the site's shared per-minute token budget. Raises :class:`LLMError`."""
    from auctions import palette_assist

    # A background worker can afford to wait; the palette's users are the ones who can't.
    time.sleep(palette_assist.wait_for_the_queue(palette_assist.site_load()))
    reservation = palette_assist.reserve_tokens()
    result = None
    try:
        provider = get_provider(model=model, timeout=VISION_TIMEOUT_SECONDS)
        if not provider.is_configured():
            msg = "No language model is configured"
            raise LLMError(msg)
        uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        content = [{"type": "text", "text": "Read this."}, {"type": "image_url", "image_url": {"url": uri}}]
        result = provider.complete(READ_PROMPT, [{"role": "user", "content": content}], max_tokens=VISION_MAX_TOKENS)
    finally:
        palette_assist.settle_tokens(reservation, result.total_tokens if result else 0)
    return result.text.strip()


def as_jpeg(data: bytes) -> bytes | None:
    """The first frame of a picture as a bounded JPEG, or ``None`` for one that is unreadable or tiny."""
    frames, _total = frames_as_jpeg(data, limit=1)
    return frames[0] if frames else None


def frames_as_jpeg(data: bytes, limit: int = MAX_PICTURES) -> tuple[list[bytes], int]:
    """Up to ``limit`` frames (a multi-page TIFF is a document) as bounded JPEGs, tiny ones dropped, and
    how many frames the file has. Frames past the limit are never decoded.
    """
    from PIL import Image, ImageOps, ImageSequence

    try:
        # Pillow only knows HEIC, what an iPhone photographs a page as, once this plugin is imported.
        import HeifImagePlugin  # noqa: F401
    except ImportError:
        pass
    try:
        with Image.open(io.BytesIO(data)) as image:
            total = getattr(image, "n_frames", 1)
            frames = []
            for frame in itertools.islice(ImageSequence.Iterator(image), limit):
                jpeg = _frame_as_jpeg(frame, ImageOps)
                if jpeg is not None:
                    frames.append(jpeg)
            return frames, total
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        return [], 0


def _frame_as_jpeg(frame, image_ops) -> bytes | None:
    if min(frame.size) < MIN_PICTURE_SIDE:
        return None
    picture = image_ops.exif_transpose(frame).convert("RGB")
    picture.thumbnail((MAX_PICTURE_SIDE, MAX_PICTURE_SIDE))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=85)
    return out.getvalue()


def extract(data: bytes, filename: str, reader: Reader | None = None) -> Extraction:
    """Read one file. Raises :class:`ExtractionError` for a file that can't be read at all."""
    reader = reader or Reader()
    extension = Path(filename).suffix.lower()
    if extension in PICTURE_EXTENSIONS:
        text = _read_picture_file(data, reader)
    elif extension == ".pdf" or data[:5] == b"%PDF-":
        text = _read_scanned_pdf(data, reader)
        if text is None:
            text = _markitdown(data, filename, reader)
    else:
        text = _markitdown(data, filename, reader)
    notes = reader.notes()
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"Only the first {MAX_TEXT_CHARS:,} characters were kept.")
    if not text:
        if reader.failed or reader.skipped:
            raise ExtractionError(" ".join(notes))
        msg = "No text or pictures could be read out of this file."
        raise ExtractionError(msg)
    return Extraction(text=text, notes=notes, read_with=reader.model)


def _read_picture_file(data: bytes, reader: Reader) -> str:
    frames, total = frames_as_jpeg(data, limit=reader.allowance)
    if not frames:
        msg = "This picture couldn't be opened, or it is too small to read."
        raise ExtractionError(msg)
    reader.skipped += max(0, total - reader.allowance)
    if len(frames) == 1:
        return reader.read_jpeg(frames[0])
    return "\n\n".join(f"<!-- page {number} -->\n\n{reader.read_jpeg(jpeg)}" for number, jpeg in enumerate(frames, 1))


def _read_scanned_pdf(data: bytes, reader: Reader) -> str | None:
    """Page by page when any page is scanned; ``None`` when none is, for markitdown to do properly."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        msg = "This PDF couldn't be opened. It may be damaged or password protected."
        raise ExtractionError(msg) from error
    try:
        layers = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            layers.append((textpage.get_text_range().strip(), _looks_scanned(page, textpage)))
            textpage.close()
            page.close()
        if not any(scanned for _text, scanned in layers):
            return None
        parts = []
        for index, (layer, scanned) in enumerate(layers):
            body = layer
            if scanned:
                page = pdf[index]
                picture = page.render(scale=RENDER_DPI / 72).to_pil()
                page.close()
                out = io.BytesIO()
                picture.convert("RGB").save(out, "JPEG", quality=85)
                jpeg = as_jpeg(out.getvalue())
                # The vision read where there is one; a failed read keeps whatever text layer there was.
                body = (reader.read_jpeg(jpeg) if jpeg else "") or layer
            parts.append(f"<!-- page {index + 1} -->\n\n{body}")
        return "\n\n".join(parts)
    finally:
        pdf.close()


def _looks_scanned(page, textpage) -> bool:
    import pypdfium2.raw as pdfium_c

    if textpage.count_chars() < SCANNED_TEXT_CHARS:
        return True
    width, height = page.get_size()
    area = (width * height) or 1
    for image in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=2):
        left, bottom, right, top = image.get_bounds()
        if (right - left) * (top - bottom) >= SCANNED_PICTURE_SHARE * area:
            return True
    return False


def _check_zip(data: bytes) -> None:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as error:
        msg = "This file looks like a zip archive but couldn't be opened."
        raise ExtractionError(msg) from error
    if len(entries) > MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > MAX_ZIP_BYTES:
        msg = "This file unpacks into too much to read."
        raise ExtractionError(msg)


class _VisionClient:
    """The slice of the OpenAI SDK markitdown calls (``client.chat.completions.create``), answered by a
    :class:`Reader`. markitdown's own prompt is ignored for :data:`READ_PROMPT`, so every picture is read
    the same way, and nothing here raises: markitdown doesn't catch a failed caption.
    """

    def __init__(self, reader: Reader):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._reader = reader

    def _create(self, model=None, messages=(), **kwargs):
        text = ""
        for message in messages:
            for part in message.get("content") or []:
                url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
                match = re.match(r"data:[^;,]*;base64,(.*)", url, re.DOTALL)
                if match:
                    try:
                        text = self._reader.read(base64.b64decode(match.group(1)))
                    except (ValueError, TypeError):
                        text = ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _docx_converter_class():
    """markitdown's Word converter, with embedded pictures read through the hook it documents."""
    import html

    from markitdown.converters import DocxConverter

    class DocxConverterReadingPictures(DocxConverter):
        def _image_to_html(self, image_stream, stream_info, **kwargs):
            client = kwargs.get("llm_client")
            if client is None:
                return None
            text = client._reader.read(image_stream.read()).strip()
            if not text:
                return None
            return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"

    return DocxConverterReadingPictures


def _markitdown(data: bytes, filename: str, reader: Reader) -> str:
    from markitdown import MarkItDown, MarkItDownException, StreamInfo

    _check_zip(data)
    client = _VisionClient(reader)
    # Built per document, because the reader is. The documents worker starts a fresh process per task.
    converter = MarkItDown(enable_plugins=False, llm_client=client, llm_model=document_model(), llm_prompt=READ_PROMPT)
    converter.register_converter(_docx_converter_class()(), priority=-1.0)
    extension = Path(filename).suffix.lower() or None
    try:
        result = converter.convert_stream(
            io.BytesIO(data), stream_info=StreamInfo(extension=extension, filename=Path(filename).name)
        )
    except MarkItDownException as error:
        logger.info("markitdown couldn't read %s: %s", filename, error)
        msg = "This kind of file can't be read. Save it as a PDF or a Word (.docx) file and upload that."
        raise ExtractionError(msg) from error
    return result.markdown or ""
