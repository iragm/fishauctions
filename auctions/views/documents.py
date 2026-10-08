"""The library at ``/library/``: upload, search with a written answer, read, correct, report, delete, and
batches of pages being put back together into articles (``/library/batch/<pk>/``).

Everything goes through :func:`auctions.documents.search.visible_documents`; a document someone can't
see is a 404, never a 403, so its existence isn't confirmed. Who may change one is
:func:`~auctions.documents.search.can_manage`.
"""

from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from auctions.documents import index, stitch
from auctions.documents.forms import (
    MAX_REQUEST_BYTES,
    BatchPagesForm,
    DocumentEditForm,
    DocumentFeedbackForm,
    DocumentUploadForm,
    add_pages,
)
from auctions.documents.models import TOPICS, Document, DocumentBatch
from auctions.documents.search import (
    answer,
    can_manage,
    can_use_library,
    delete_document,
    search,
    visible_batches,
    visible_documents,
    visible_pages,
)
from auctions.models import Club, Species

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
    "DocumentReindexView",
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


def _managed_or_404(request, pk):
    document = _visible_or_404(request, pk)
    if not can_manage(request.user, document):
        raise Http404
    return document


class LibraryView(LibraryMixin, TemplateView):
    """The library: search box, upload form, and either the matching passages or every document."""

    template_name = "documents/library.html"

    def get_context_data(self, form=None, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        documents, chosen = _filtered(self.request, visible_documents(user))
        query = self.request.GET.get("q", "").strip()[:300]
        context.update(chosen)
        context["query"] = query
        context["topics"] = TOPICS
        if form is None:
            # Arriving from a club's sidebar: that club, if they may file there.
            club = chosen.get("club")
            form = DocumentUploadForm(user=user, initial={"club": club.pk} if club else {})
        context["form"] = form
        context["max_request_bytes"] = MAX_REQUEST_BYTES
        if query:
            context["hits"], context["total"] = search(user, query, documents=documents, limit=RESULTS_PER_PAGE)
        else:
            listed = documents.select_related("club").defer("text").order_by("-createdon", "-pk")
            context["page_obj"] = Paginator(listed, DOCUMENTS_PER_PAGE).get_page(self.request.GET.get("page"))
            context["removed"] = Document.objects.filter(owner=user, removed=True).order_by("-createdon")
            context["batches"] = DocumentBatch.objects.filter(owner=user).exclude(status=DocumentBatch.DONE)
        return context

    def post(self, request, *args, **kwargs):
        """Upload. Not a skill: the bytes are a file on this person's device."""
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


class DocumentDetailView(LibraryMixin, TemplateView):
    """One document: what was read out of it, its tags, and for its managers what readers reported."""

    template_name = "documents/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = _visible_or_404(self.request, kwargs["pk"])
        manager = can_manage(self.request.user, document)
        context.update(
            document=document,
            can_manage=manager,
            topics=[(slug, label) for slug, label in TOPICS if slug in (document.topics or [])],
            passages=_passages(document),
            scans=document.pages.order_by("position"),
            species=document.species.all().order_by("scientific_name"),
            reports=document.feedback.filter(resolved=False).select_related("user") if manager else [],
            feedback_form=DocumentFeedbackForm(),
            dmca_url=reverse("dmca_notice")
            + "?material="
            + self.request.build_absolute_uri(document.get_absolute_url()),
        )
        return context


def _passages(document):
    """The text as its passages, each anchored at its offset so a search hit can link to it.

    Sliced from the text rather than read from ``DocumentChunk.text``, which repeats a split table's
    header row. Empty while there are no passages; the page then shows the text whole.
    """
    scans = dict(document.pages.values_list("position", "pk")) if document.batch_id else {}
    rows = []
    page = None
    for start, end, number in document.chunks.order_by("position").values_list("start", "end", "page"):
        rows.append(
            {
                "start": start,
                "text": document.text[start:end],
                "page": number,
                "new_page": number != page,
                "scan": scans.get(number),
            }
        )
        page = number
    return rows


class DocumentFileView(LibraryMixin, View):
    """The original file. Pictures and PDFs open in the browser; anything else downloads, as bytes."""

    def get(self, request, pk):
        document = _visible_or_404(request, pk)
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


class BatchPageView(LibraryMixin, View):
    """One scanned page, as a picture: what a reader checks a transcription against."""

    def get(self, request, pk):
        page = get_object_or_404(visible_pages(request.user), pk=pk)
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
