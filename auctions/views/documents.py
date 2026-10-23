"""The library: the list of your own and your clubs' documents (``/library/``), adding them
(``/library/add/``), searching with a written answer (``/library/search/``), reading one
(``/library/<pk>-<slug>/``), correcting, reporting and deleting, batches of pages being put back
together into articles (``/library/batch/<pk>/``), and the pictures on a document, added through a
signed link (``/library/<pk>/pictures/<token>/``).

Everything goes through :func:`auctions.documents.search.visible_documents`; a document someone can't
see is a 404, never a 403, so its existence isn't confirmed. The one exception is ``web`` visibility:
a document's page, file, scans and pictures are open to anyone, signed in or not, through
:func:`~auctions.documents.search.readable_documents`. Who may change one is
:func:`~auctions.documents.search.can_manage`.
"""

from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from auctions.documents import index, pictures, stitch
from auctions.documents.forms import (
    MAX_REQUEST_BYTES,
    BatchPagesForm,
    DocumentEditForm,
    DocumentFeedbackForm,
    DocumentUploadForm,
    add_pages,
)
from auctions.documents.models import TOPICS, Document, DocumentBatch, DocumentPicture, Visibility
from auctions.documents.render import to_html
from auctions.documents.search import (
    answer,
    can_manage,
    can_use_library,
    clubs_to_file_under,
    delete_document,
    my_library,
    readable_documents,
    readable_pages,
    search,
    visible_batches,
    visible_documents,
)
from auctions.filters import DocumentFilter
from auctions.models import Club, Species
from auctions.tables import DocumentHTMxTable
from auctions.views.base import HTMxTableView

#: Passages listed under a search, and documents per page of the list.
RESULTS_PER_PAGE = 20
DOCUMENTS_PER_PAGE = 50

#: Served inline: types a browser shows without running anything. Everything else is a download.
INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

__all__ = [
    "BatchPageView",
    "DocumentAnswerView",
    "DocumentBatchView",
    "DocumentDeleteView",
    "DocumentDetailView",
    "DocumentEditView",
    "DocumentFeedbackView",
    "DocumentFileView",
    "DocumentPictureView",
    "DocumentPicturesView",
    "DocumentReindexView",
    "DocumentShortLinkView",
    "LibraryAddView",
    "LibrarySearchView",
    "LibraryView",
]


class LibraryMixin(LoginRequiredMixin):
    """Signed in, with the library on for this account (:func:`~auctions.documents.search.can_use_library`)."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_use_library(request.user):
            raise Http404
        return super().dispatch(request, *args, **kwargs)


def _filtered(request, documents):
    """Narrow by the page's club, topic and species filters. Returns the queryset and what was chosen."""
    chosen = {}
    club = request.GET.get("club", "").strip()
    if club:
        documents = documents.filter(club__slug=club)
        chosen["club"] = Club.objects.filter(slug=club).first()
    topic = request.GET.get("topic", "").strip()
    if topic in dict(TOPICS):
        documents = documents.filter(topics__contains=[topic])
        chosen["topic"] = topic
    species = request.GET.get("species", "").strip()
    if species.isdigit():
        documents = documents.filter(species__pk=int(species))
        chosen["species"] = Species.objects.filter(pk=int(species)).first()
    return documents, chosen


def _visible_or_404(request, pk):
    return get_object_or_404(visible_documents(request.user).select_related("club", "owner"), pk=pk)


class _SignIn(Exception):
    pass


class ReadableMixin:
    """For a document's page, file, scans and pictures, which a ``web`` document opens to anyone. Not
    found for somebody signed out is the sign-in page, as :class:`LibraryMixin` would have sent them.
    """

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except _SignIn:
            return redirect_to_login(request.get_full_path())

    def found_or_404(self, queryset, **lookup):
        found = queryset.filter(**lookup).first()
        if found is not None:
            return found
        if not self.request.user.is_authenticated:
            raise _SignIn
        raise Http404


def _managed_or_404(request, pk):
    document = _visible_or_404(request, pk)
    if not can_manage(request.user, document):
        raise Http404
    return document


class LibraryView(LibraryMixin, HTMxTableView):
    """The list: what you uploaded and what is in your clubs' libraries, filtered as you type, with an
    Add button. Asking the whole library a question is :class:`LibrarySearchView`.
    """

    model = Document
    table_class = DocumentHTMxTable
    filterset_class = DocumentFilter
    template_name = "documents/library.html"
    htmx_table_header_template = "documents/_library_header.html"
    table_pagination = {"per_page": DOCUMENTS_PER_PAGE}

    def get(self, request, *args, **kwargs):
        # Old links to the search, from before it had a page of its own.
        if any(request.GET.get(key) for key in ("q", "topic", "species")):
            return redirect(reverse("library_search") + "?" + request.GET.urlencode())
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return my_library(self.request.user).select_related("club").defer("text").order_by("-createdon", "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        chosen = self.request.GET.get("visibility", "")
        context["visibility_choices"] = [("", "Anyone"), *Visibility.choices]
        context["selected_visibility"] = chosen if chosen in Visibility.values else ""
        context["selected_visibility_label"] = dict(Visibility.choices).get(chosen, "")
        context["club"] = Club.objects.filter(slug=self.request.GET.get("club", "")).first()
        if not self.request.htmx:
            context["removed"] = Document.objects.filter(owner=user, removed=True).order_by("-createdon")
            context["batches"] = DocumentBatch.objects.filter(owner=user).exclude(status=DocumentBatch.DONE)
        return context


class LibrarySearchView(LibraryMixin, TemplateView):
    """Search every document you can read, with a written answer, narrowed by club, topic or species.

    Kept as it was when it was the library's front page. Marked for a rework with the site's own AI
    page: the answer here is one model call over the top passages, and filters are links, not words.
    """

    template_name = "documents/search.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        documents, chosen = _filtered(self.request, visible_documents(user))
        query = self.request.GET.get("q", "").strip()[:300]
        context.update(chosen)
        context["query"] = query
        context["topics"] = TOPICS
        if query:
            context["hits"], context["total"] = search(user, query, documents=documents, limit=RESULTS_PER_PAGE)
        return context


def _last_placement(user):
    """The visibility and club of what ``user`` last added, so the next upload starts there."""
    latest = [
        row
        for row in (
            Document.objects.filter(owner=user, batch__isnull=True).order_by("-createdon").first(),
            DocumentBatch.objects.filter(owner=user).order_by("-createdon").first(),
        )
        if row is not None
    ]
    if not latest:
        return Visibility.PUBLIC, None
    last = max(latest, key=lambda row: row.createdon)
    return last.visibility, last.club_id


class LibraryAddView(LibraryMixin, TemplateView):
    """Add documents: drop or paste files, or paste the text. Starts with the visibility and club used last."""

    template_name = "documents/add.html"

    def get_context_data(self, form=None, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        if form is None:
            visibility, club_pk = _last_placement(user)
            # Arriving from a club's sidebar: that club, if they may file there.
            arriving = Club.objects.filter(slug=self.request.GET.get("club", "")).first()
            if arriving is not None:
                club_pk = arriving.pk
            if club_pk and not clubs_to_file_under(user).filter(pk=club_pk).exists():
                club_pk = None
            # Search engines are never the starting point: one article shared with the world mustn't
            # carry the next pile of scans along with it.
            if visibility == Visibility.WEB or (visibility == Visibility.CLUB and not club_pk):
                visibility = Visibility.PUBLIC
            form = DocumentUploadForm(user=user, initial={"visibility": visibility, "club": club_pk})
        context["form"] = form
        context["max_request_bytes"] = MAX_REQUEST_BYTES
        # DATA_UPLOAD_MAX_MEMORY_SIZE bounds the form's text fields, with a little room for the rest.
        context["max_text_bytes"] = settings.DATA_UPLOAD_MAX_MEMORY_SIZE - 64 * 1024
        return context

    def post(self, request, *args, **kwargs):
        """Upload. ``add_document`` on ``/mcp/`` for text an agent has read."""
        form = DocumentUploadForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        saved = form.save()
        if form.skipped:
            messages.warning(request, "Skipped " + "; ".join(form.skipped))
        if isinstance(saved, DocumentBatch):
            stitch.queue(saved)
            return redirect(saved.get_absolute_url())
        documents = saved
        for document in documents:
            index.queue(document)
        if len(documents) == 1:
            messages.success(request, "Uploaded. It's being read; that can take a few minutes for scans.")
            return redirect(documents[0].get_absolute_url())
        messages.success(request, f"Uploaded {len(documents)} documents. They're being read, one at a time.")
        return redirect("library")


class DocumentAnswerView(LibraryMixin, View):
    """The written answer above a search, loaded by htmx after the page so the page never waits on it."""

    def get(self, request, *args, **kwargs):
        query = request.GET.get("q", "").strip()[:300]
        documents, _chosen = _filtered(request, visible_documents(request.user))
        hits, _total = search(request.user, query, documents=documents, limit=RESULTS_PER_PAGE) if query else ([], 0)
        result = answer(request.user, query, hits)
        return render(request, "documents/_answer.html", {"result": result, "query": query})


class DocumentShortLinkView(ReadableMixin, View):
    """``/library/<pk>/``, the address before titles were in it: on to the one with the title, for
    whoever may read it, so the redirect never spells out somebody else's title.
    """

    def get(self, request, pk):
        return redirect(self.found_or_404(readable_documents(request.user), pk=pk).get_absolute_url())


class DocumentDetailView(ReadableMixin, TemplateView):
    """One document: what was read out of it, its tags, and for its managers what readers reported.
    Open to anyone for a ``web`` document, the only kind a search engine is let index.
    """

    template_name = "documents/detail.html"

    def get(self, request, *args, **kwargs):
        self.document = self.found_or_404(
            readable_documents(request.user).select_related("club", "owner"), pk=kwargs["pk"]
        )
        if kwargs.get("slug") != self.document.slug:
            return redirect(self.document.get_absolute_url())
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = self.document
        manager = can_manage(self.request.user, document)
        pictures_on_it = list(document.pictures.all())
        placed = set()
        passages = _passages(document, pictures_on_it, placed)
        text_html = "" if passages else to_html(document.text, pictures_on_it, placed)
        context.update(
            document=document,
            has_library=can_use_library(self.request.user),
            indexable=document.visibility == Visibility.WEB,
            can_manage=manager,
            topics=[(slug, label) for slug, label in TOPICS if slug in (document.topics or [])],
            passages=passages,
            text_html=text_html,
            scans=document.pages.order_by("position"),
            pictures=pictures_on_it,
            gallery=[picture for picture in pictures_on_it if picture.pk not in placed],
            pictures_link=pictures.upload_link(document, self.request.user) if manager else "",
            species=document.species.all().order_by("scientific_name"),
            reports=document.feedback.filter(resolved=False).select_related("user") if manager else [],
            feedback_form=DocumentFeedbackForm(),
            dmca_url=reverse("dmca_notice")
            + "?material="
            + self.request.build_absolute_uri(document.get_absolute_url()),
        )
        return context


def _passages(document, pictures_on_it, placed):
    """The text as its passages, each anchored at its offset so a search hit can link to it.

    Sliced from the text rather than read from ``DocumentChunk.text``, which repeats a split table's
    header row. A passage cut out of the middle of a block (a long table, a long paragraph) is joined to
    the one before it, so the Markdown is never rendered half a table at a time; its anchor rides along
    at the top. Empty while there are no passages; the page then shows the text whole.
    """
    scans = dict(document.pages.values_list("position", "pk")) if document.batch_id else {}
    rows = []
    page = None
    for start, end, number in document.chunks.order_by("position").values_list("start", "end", "page"):
        at_a_block = start == 0 or document.text[:start].endswith("\n\n") or number != page
        if rows and not at_a_block:
            rows[-1]["end"] = end
            rows[-1]["also"].append(start)
            continue
        rows.append(
            {
                "start": start,
                "end": end,
                "also": [],
                "page": number,
                "new_page": number != page,
                "scan": scans.get(number),
            }
        )
        page = number
    for row in rows:
        row["html"] = to_html(document.text[row["start"] : row["end"]], pictures_on_it, placed)
    return rows


class DocumentFileView(ReadableMixin, View):
    """The original file. Pictures and PDFs open in the browser; anything else downloads, as bytes."""

    def get(self, request, pk):
        document = self.found_or_404(readable_documents(request.user), pk=pk)
        if not document.file:
            # Put together from a batch: its originals are its pages.
            first = document.pages.order_by("position").first()
            if first is None:
                raise Http404
            return redirect("batch_page", pk=first.pk)
        try:
            handle = document.file.open("rb")
        except FileNotFoundError as error:
            raise Http404 from error
        extension = Path(document.original_name).suffix.lower()
        inline = extension in INLINE_TYPES
        response = FileResponse(
            handle,
            as_attachment=not inline,
            filename=document.original_name,
            content_type=INLINE_TYPES.get(extension, "application/octet-stream"),
        )
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DocumentEditView(LibraryMixin, View):
    """Correct the title, author, year, club, topics or the text itself. ``update_document`` on ``/mcp/``."""

    template_name = "documents/edit.html"

    def get(self, request, pk):
        document = _managed_or_404(request, pk)
        form = DocumentEditForm(instance=document, user=request.user)
        return render(request, self.template_name, {"document": document, "form": form})

    def post(self, request, pk):
        document = _managed_or_404(request, pk)
        form = DocumentEditForm(request.POST, instance=document, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {"document": document, "form": form})
        form.save()
        if form.needs_indexing:
            index.queue(document)
        messages.success(request, "Saved.")
        return redirect(document.get_absolute_url())


class DocumentReindexView(LibraryMixin, View):
    """Read the file again with today's reader, replacing any hand corrections."""

    def post(self, request, pk):
        document = _managed_or_404(request, pk)
        if document.batch_id:
            messages.error(
                request,
                "This article was put together from a batch of pages. Correct its text with Edit, or put the "
                "whole batch together again from the batch's page.",
            )
            return redirect(document.get_absolute_url())
        index.queue(document, reread=True)
        messages.success(request, "It's being read again.")
        return redirect(document.get_absolute_url())


class DocumentDeleteView(LibraryMixin, View):
    """Delete a document and its file. ``delete_document`` on ``/mcp/``."""

    def post(self, request, pk):
        document = _managed_or_404(request, pk)
        title = delete_document(document)
        messages.success(request, f"Deleted {title}.")
        return redirect("library")


class DocumentFeedbackView(LibraryMixin, View):
    """A reader reporting a problem. Copyright isn't one of the reasons: that is a DMCA notice."""

    def post(self, request, pk):
        document = _visible_or_404(request, pk)
        form = DocumentFeedbackForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.document = document
            report.user = request.user
            report.save()
            messages.success(request, "Thanks. Whoever looks after this document will see it.")
        else:
            messages.error(request, "Say what's wrong with it.")
        return redirect(document.get_absolute_url())


def _batch_or_404(request, pk):
    return get_object_or_404(visible_batches(request.user).select_related("club", "owner"), pk=pk)


class DocumentBatchView(LibraryMixin, View):
    """A batch of pages: how far reading and stitching have got, the articles it made, and for whoever
    looks after it, adding pages and the buttons that move it on.
    """

    template_name = "documents/batch.html"

    def get(self, request, pk, form=None):
        batch = _batch_or_404(request, pk)
        pages = batch.pages.all()
        manager = can_manage(request.user, batch)
        context = {
            "batch": batch,
            "can_manage": manager,
            "page_count": pages.count(),
            "pages_read": pages.filter(read=True).count(),
            "pages_settled": batch.stitched_through,
            "documents": batch.documents.filter(removed=False).order_by("pk"),
            "form": form or (BatchPagesForm(user=request.user, batch=batch) if manager else None),
            "max_request_bytes": MAX_REQUEST_BYTES,
        }
        return render(request, self.template_name, context)

    def post(self, request, pk):
        """``action``: ``add`` (more pages), ``finish`` (put the articles together now, or carry on after a
        failure), ``again`` (rebuild every article from scratch), or ``delete``.
        """
        batch = _batch_or_404(request, pk)
        if not can_manage(request.user, batch):
            raise Http404
        action = request.POST.get("action", "")
        if batch.status == batch.STITCHING and action in ("add", "again"):
            messages.error(request, "It's being put together right now. Wait for it to finish first.")
        elif action == "add":
            form = BatchPagesForm(request.POST, request.FILES, user=request.user, batch=batch)
            if not form.is_valid():
                return self.get(request, pk, form=form)
            added = add_pages(batch, form.accepted)
            if form.skipped:
                messages.warning(request, "Skipped " + "; ".join(form.skipped))
            more = form.cleaned_data.get("more_coming")
            batch.status = batch.WAITING if more or batch.status == batch.DONE else batch.READING
            batch.save(update_fields=["status"])
            stitch.queue(batch)
            messages.success(request, f"Added {added} pages.")
        elif action == "finish":
            settled = batch.pages.exclude(assignments=[]).exists()
            batch.status = batch.STITCHING if batch.status == batch.FAILED and settled else batch.READING
            batch.error = ""
            batch.save(update_fields=["status", "error"])
            stitch.queue(batch)
        elif action == "again":
            batch.status = batch.READING
            batch.save(update_fields=["status"])
            stitch.queue(batch)
            messages.success(request, "Its articles will be put together again from the start.")
        elif action == "delete":
            for document in batch.documents.all():
                delete_document(document)
            batch.delete()
            messages.success(request, "Deleted the batch and its articles.")
            return redirect("library")
        return redirect(batch.get_absolute_url())


class BatchPageView(ReadableMixin, View):
    """One scanned page, as a picture: what a reader checks a transcription against."""

    def get(self, request, pk):
        page = self.found_or_404(readable_pages(request.user), pk=pk)
        stored = page.image or page.file
        if page.pdf_page is not None and not page.image:
            raise Http404  # not read yet, so not rendered yet
        try:
            handle = stored.open("rb")
        except FileNotFoundError as error:
            raise Http404 from error
        extension = Path(stored.name).suffix.lower()
        response = FileResponse(handle, content_type=INLINE_TYPES.get(extension, "application/octet-stream"))
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DocumentPictureView(ReadableMixin, View):
    """One picture from a document, to whoever can read the document."""

    def get(self, request, pk):
        picture = self.found_or_404(
            DocumentPicture.objects.filter(document__in=readable_documents(request.user)), pk=pk
        )
        try:
            handle = picture.file.open("rb")
        except FileNotFoundError as error:
            raise Http404 from error
        extension = Path(picture.file.name).suffix.lower()
        response = FileResponse(handle, content_type=INLINE_TYPES.get(extension, "application/octet-stream"))
        response["X-Content-Type-Options"] = "nosniff"
        return response


@method_decorator(csrf_exempt, name="dispatch")
class DocumentPicturesView(View):
    """Add pictures to a document, or take them off, through the signed link in
    :mod:`auctions.documents.pictures`. The link is the credential, so no session and no CSRF token:
    an assistant's script posts ``file`` fields (with a ``caption`` each) and gets JSON back; the page
    this shows posts the same form and gets the page again.
    """

    template_name = "documents/pictures.html"

    def _document(self, pk, token):
        user = pictures.signer_of(pk, token)
        if user is None or not can_use_library(user):
            raise Http404
        document = Document.objects.filter(pk=pk, removed=False).select_related("owner", "club").first()
        if document is None or not can_manage(user, document):
            raise Http404
        return document

    def get(self, request, pk, token):
        document = self._document(pk, token)
        context = {
            "document": document,
            "pictures": document.pictures.all(),
            "limit": pictures.MAX_PICTURES_PER_DOCUMENT,
        }
        return render(request, self.template_name, context)

    def post(self, request, pk, token):
        document = self._document(pk, token)
        from_page = request.POST.get("from_page") == "1"
        remove = request.POST.get("remove", "")
        if remove.isdigit():
            for picture in document.pictures.filter(pk=int(remove)):
                picture.delete()
            if from_page:
                return redirect("document_pictures", pk=document.pk, token=token)
            return JsonResponse({"removed": int(remove), "pictures_left": document.pictures.count()})
        uploads = request.FILES.getlist("file")
        if not uploads:
            problems = ["Send each picture as a field named file, with a caption field after each if it has one."]
            added = []
        else:
            added, problems = pictures.add_pictures(document, uploads, request.POST.getlist("caption"))
        if from_page:
            if added:
                messages.success(request, f"Added {len(added)} picture{'s' if len(added) != 1 else ''}.")
            for problem in problems:
                messages.error(request, problem)
            return redirect("document_pictures", pk=document.pk, token=token)
        return JsonResponse(
            {
                "added": [
                    {
                        "picture": picture.pk,
                        "caption": picture.caption,
                        "url": request.build_absolute_uri(reverse("document_picture", args=[picture.pk])),
                    }
                    for picture in added
                ],
                "problems": problems,
                "document_url": request.build_absolute_uri(document.get_absolute_url()),
            },
            status=200 if added or not problems else 400,
        )
