"""The library's forms: upload, adding pages to a batch, edit, and a reader's report. ``update_document``
and ``add_document`` on ``/mcp/`` save through :class:`DocumentEditForm` and :class:`DocumentUploadForm`,
so the page and the tools validate the same way.
"""

import datetime
import hashlib
import re
from pathlib import Path

from django import forms
from django.db.models import Q, Sum
from django.utils import timezone

from auctions.documents.extract import PICTURE_EXTENSIONS
from auctions.documents.models import TOPICS, BatchPage, Document, DocumentBatch, DocumentFeedback, Visibility
from auctions.documents.search import clubs_to_file_under, visible_documents
from auctions.models import Club

#: One file, and one whole upload. nginx allows ``/library/`` a 100M body (``nginx_fishauctions.conf``),
#: because a scanned newsletter is easily 30 MB; the page checks the total before sending.
MAX_UPLOAD_BYTES = 95 * 1024 * 1024
MAX_REQUEST_BYTES = 100 * 1000 * 1000
MAX_FILES_PER_UPLOAD = 100
#: Reading pictures is paid for by the site, so one person's uploads are bounded. An archive is
#: hundreds of scans, so the daily count is generous; the space is what really bounds it.
MAX_UPLOADS_PER_DAY = 1000
MAX_BYTES_PER_PERSON = 5 * 1024 * 1024 * 1024
#: Pages in one batch. Past this, start another: stitching only ever joins pages within one batch.
MAX_BATCH_PAGES = 3000

PAGES = "pages"
WHOLE = "whole"


def _clean_year(year):
    if year and not 1850 <= year <= timezone.now().year:
        problem = "That doesn't look like the year it was written."
        raise forms.ValidationError(problem)
    return year


def natural_key(name: str) -> list:
    """Sort key putting scan9.jpg before scan10.jpg, the order a scanner or camera numbered them."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


def _sha256(upload) -> str:
    digest = hashlib.sha256()
    for piece in upload.chunks():
        digest.update(piece)
    return digest.hexdigest()


def _pdf_pages(upload) -> int:
    """How many pages a PDF has, without rendering any; 0 for one that won't open."""
    import pypdfium2 as pdfium

    try:
        upload.seek(0)
        pdf = pdfium.PdfDocument(upload.read())
    except pdfium.PdfiumError:
        return 0
    finally:
        upload.seek(0)
    try:
        return len(pdf)
    finally:
        pdf.close()


def _is_page(upload) -> bool:
    return Path(upload.name).suffix.lower() in PICTURE_EXTENSIONS | {".pdf"}


class _MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class _MultipleFileField(forms.FileField):
    """Django's documented recipe for a file input that takes several files: a list of uploads."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", _MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        uploads = [one for one in (data if isinstance(data, list | tuple) else [data]) if one]
        if not uploads:
            return [super().clean(None, initial)]
        return [super(_MultipleFileField, self).clean(one, initial) for one in uploads]


class _Placement:
    """The club and visibility fields, which upload and edit share."""

    def _place(self, user, current_club=None):
        clubs = clubs_to_file_under(user)
        if current_club is not None:
            clubs = Club.objects.filter(Q(pk__in=clubs.values("pk")) | Q(pk=current_club.pk)).order_by("name")
        field = self.fields["club"]
        field.queryset = clubs
        field.required = False
        field.empty_label = "None"
        field.help_text = "The club whose library it belongs in."
        if not clubs.exists():
            del self.fields["club"]
            self.fields["visibility"].choices = [
                choice for choice in Visibility.choices if choice[0] != Visibility.CLUB
            ]

    def _check_placement(self, cleaned):
        if cleaned.get("visibility") == Visibility.CLUB and not cleaned.get("club"):
            self.add_error("visibility", "Choose the club whose members can read it.")


class _Uploads:
    """Checks every upload shares: size, daily count, space, duplicates and takedowns."""

    def _check_quota(self, uploads):
        mine = Document.objects.filter(owner=self.user)
        today = mine.filter(createdon__gte=timezone.now() - datetime.timedelta(days=1)).count()
        today += BatchPage.objects.filter(
            batch__owner=self.user, batch__createdon__gte=timezone.now() - datetime.timedelta(days=1)
        ).count()
        used = (mine.aggregate(total=Sum("size"))["total"] or 0) + sum(upload.size for upload in uploads)
        if len(uploads) > MAX_FILES_PER_UPLOAD:
            return f"Upload up to {MAX_FILES_PER_UPLOAD} files at a time."
        if today + len(uploads) > MAX_UPLOADS_PER_DAY:
            return f"That would be more than {MAX_UPLOADS_PER_DAY} uploads today. Try the rest tomorrow."
        if used > MAX_BYTES_PER_PERSON:
            return "That's more than your library space. Delete something first."
        return ""

    def _why_not(self, upload, seen: set[str]) -> tuple[str, str]:
        """The file's hash, and why it can't go in, if it can't."""
        if upload.size > MAX_UPLOAD_BYTES:
            return "", f"Files can be up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        digest = _sha256(upload)
        if digest in seen:
            return digest, "The same file twice in one upload."
        seen.add(digest)
        batch = getattr(self, "batch", None)
        if batch is not None and batch.pages.filter(sha256=digest).exists():
            return digest, "Already in this batch."
        already = visible_documents(self.user).filter(sha256=digest).first()
        if already:
            return digest, f"That file is already in the library, as “{already.display_title}”."
        if Document.objects.filter(sha256=digest, removed=True).exists():
            return digest, "That file was taken down after a copyright notice."
        return digest, ""

    def _sort_out(self, uploads):
        """Fill ``accepted`` and ``skipped``; raise if nothing is left."""
        seen: set[str] = set()
        for upload in uploads:
            digest, reason = self._why_not(upload, seen)
            if reason:
                self.skipped.append(f"{upload.name}: {reason}" if len(uploads) > 1 else reason)
            else:
                self.accepted.append((upload, digest))
        if not self.accepted:
            raise forms.ValidationError(self.skipped)


def add_pages(batch, accepted) -> int:
    """Store uploads as pages of ``batch``: a picture is one page, a PDF one per page sharing its file.
    Returns how many pages were added.
    """
    added = 0
    for upload, digest in accepted:
        if Path(upload.name).suffix.lower() == ".pdf":
            count = _pdf_pages(upload)
            if not count:
                continue
            first = BatchPage.objects.create(
                batch=batch, file=upload, pdf_page=0, original_name=upload.name[:255], sha256=digest
            )
            BatchPage.objects.bulk_create(
                BatchPage(
                    batch=batch, file=first.file.name, pdf_page=number, original_name=first.original_name, sha256=digest
                )
                for number in range(1, count)
            )
            added += count
        else:
            BatchPage.objects.create(batch=batch, file=upload, original_name=upload.name[:255], sha256=digest)
            added += 1
    return added


class DocumentUploadForm(_Placement, _Uploads, forms.Form):
    """Upload: either pages to put back together into articles (a batch), or one document per file.

    A file that is too big or already in the library is skipped and named (``skipped``) when others in
    the same upload are fine; the form fails only when none is.
    """

    file = _MultipleFileField(label="Files", help_text="Choose as many as you like.")
    mode = forms.ChoiceField(
        required=False,
        label="These are",
        choices=[
            (PAGES, "Pages of articles: put them back together (scans, photos of pages, scanned newsletters)"),
            (WHOLE, "Whole documents: one per file (Word files, PDFs of single articles, books)"),
        ],
        initial=PAGES,
        widget=forms.RadioSelect,
    )
    more_coming = forms.BooleanField(
        required=False, label="More pages to come: wait for them before putting the articles together"
    )
    visibility = forms.ChoiceField(
        required=False,
        label="Who can read it",
        choices=Visibility.choices,
        initial=Visibility.PUBLIC,
        widget=forms.RadioSelect,
    )
    club = forms.ModelChoiceField(queryset=Club.objects.none())
    title = forms.CharField(max_length=300, required=False, help_text="Blank: read from the document. One file only.")
    author = forms.CharField(max_length=200, required=False)
    year = forms.IntegerField(required=False, help_text="The year it was written.")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.accepted: list[tuple] = []
        self.skipped: list[str] = []
        self._place(user)

    def clean_year(self):
        return _clean_year(self.cleaned_data.get("year"))

    def clean_mode(self):
        # The page always sends it; a caller that doesn't is sending one whole document.
        return self.cleaned_data.get("mode") or WHOLE

    def clean_visibility(self):
        return self.cleaned_data.get("visibility") or Visibility.PUBLIC

    def clean(self):
        cleaned = super().clean()
        self._check_placement(cleaned)
        uploads = cleaned.get("file") or []
        if not uploads:
            return cleaned
        problem = self._check_quota(uploads)
        if not problem and cleaned.get("mode") == PAGES:
            others = [upload.name for upload in uploads if not _is_page(upload)]
            if others:
                problem = (
                    f"Only pictures and PDFs can be pages: {', '.join(others[:3])}. Upload those as whole documents."
                )
        if problem:
            raise forms.ValidationError(problem)
        self._sort_out(uploads)
        return cleaned

    def save(self):
        """A :class:`DocumentBatch` in pages mode, else a list of :class:`Document`."""
        cleaned = self.cleaned_data
        if cleaned.get("mode") == PAGES:
            batch = DocumentBatch.objects.create(
                owner=self.user,
                club=cleaned.get("club"),
                visibility=cleaned["visibility"],
                status=DocumentBatch.WAITING if cleaned.get("more_coming") else DocumentBatch.READING,
            )
            add_pages(batch, self.accepted)
            return batch
        one = len(self.accepted) == 1 and not self.skipped
        documents = []
        for upload, digest in self.accepted:
            document = Document(
                owner=self.user,
                club=cleaned.get("club"),
                visibility=cleaned["visibility"],
                title=cleaned.get("title", "") if one else "",
                author=cleaned.get("author", ""),
                year=cleaned.get("year"),
                original_name=upload.name[:255],
                size=upload.size,
                sha256=digest,
                file=upload,
            )
            document.save()
            documents.append(document)
        return documents


class BatchPagesForm(_Uploads, forms.Form):
    """More pages for a batch that isn't finished."""

    file = _MultipleFileField(label="More pages")
    more_coming = DocumentUploadForm.base_fields["more_coming"]

    def __init__(self, *args, user, batch, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.batch = batch
        self.accepted: list[tuple] = []
        self.skipped: list[str] = []

    def clean(self):
        cleaned = super().clean()
        uploads = cleaned.get("file") or []
        if not uploads:
            return cleaned
        problem = self._check_quota(uploads)
        others = [upload.name for upload in uploads if not _is_page(upload)]
        if not problem and others:
            problem = f"Only pictures and PDFs can be pages: {', '.join(others[:3])}."
        if not problem and self.batch.pages.count() + len(uploads) > MAX_BATCH_PAGES:
            problem = f"A batch holds up to {MAX_BATCH_PAGES} pages. Start another for the rest."
        if problem:
            raise forms.ValidationError(problem)
        self._sort_out(uploads)
        return cleaned


class _TextField(forms.CharField):
    """A textarea comes back with CRLF line endings; without this every save "changed" the text."""

    def to_python(self, value):
        return super().to_python(value).replace("\r\n", "\n")


#: Past this the text isn't offered for correction: the whole of it is posted back on every save, and
#: a long one would be over Django's DATA_UPLOAD_MAX_MEMORY_SIZE, refusing even a title change.
MAX_EDITABLE_TEXT = 200_000


class DocumentEditForm(_Placement, forms.ModelForm):
    topics = forms.MultipleChoiceField(choices=TOPICS, required=False, widget=forms.CheckboxSelectMultiple)
    visibility = forms.ChoiceField(label="Who can read it", choices=Visibility.choices, widget=forms.RadioSelect)
    text = _TextField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 16, "class": "font-monospace"}),
        help_text="Corrections here are kept: reading the file again is the only thing that replaces them.",
    )
    resolve_reports = forms.BooleanField(required=False, label="This fixes the problems readers reported")

    class Meta:
        model = Document
        fields = ["title", "author", "year", "visibility", "club", "topics", "text"]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self._place(user, current_club=self.instance.club)
        if len(self.instance.text) > MAX_EDITABLE_TEXT:
            del self.fields["text"]
        if not self.instance.feedback.filter(resolved=False).exists():
            del self.fields["resolve_reports"]

    def clean_year(self):
        return _clean_year(self.cleaned_data.get("year"))

    def clean(self):
        cleaned = super().clean()
        self._check_placement(cleaned)
        return cleaned

    def save(self, commit=True):
        document = super().save(commit=False)
        if "text" in self.changed_data:
            document.text_edited = True
            document.chunker_version = ""  # re-cut the passages from the corrected text
        # A person has now said what these are; a better model later must leave them alone.
        document.auto_fields = [name for name in document.auto_fields if name not in self.changed_data]
        if commit:
            document.save()
            if self.cleaned_data.get("resolve_reports"):
                document.feedback.filter(resolved=False).update(resolved=True)
        return document

    @property
    def needs_indexing(self):
        return "text" in self.changed_data


class DocumentFeedbackForm(forms.ModelForm):
    class Meta:
        model = DocumentFeedback
        fields = ["reason", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}
        labels = {"reason": "What's wrong?", "note": "Details"}
