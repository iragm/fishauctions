"""Bring the library up to date after a parser, chunker or embedding-model change.

By default queues only what is stale (``auctions.documents.index.stale_documents``); ``--all``
queues every document, ``--reread`` reads every file again (discarding hand corrections), and
``--now`` runs here instead of on the documents worker. ``--status`` changes nothing and says how
the library stands -- the place to look when search seems to have got worse, since a retired
embedding model fails quietly into keyword-only search.
"""

from django.core.management.base import BaseCommand

from auctions.documents import index
from auctions.models import Document


class Command(BaseCommand):
    help = "Re-index library documents whose text, passages or vectors are out of date."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Every document, not only stale ones.")
        parser.add_argument("--reread", action="store_true", help="Read every file again, discarding corrections.")
        parser.add_argument("--now", action="store_true", help="Run here, one at a time, instead of queueing.")
        parser.add_argument("--status", action="store_true", help="Report, change nothing.")

    def handle(self, *args, **options):
        if options["status"]:
            self.status()
            return
        documents = Document.objects.all() if options["all"] or options["reread"] else index.stale_documents()
        pks = list(documents.order_by("pk").values_list("pk", flat=True))
        for pk in pks:
            document = Document(pk=pk)
            if options["now"]:
                if options["reread"]:
                    Document.objects.filter(pk=pk).update(parser_version="", text_edited=False)
                index.index_document(pk)
                self.stdout.write(f"{pk}: {Document.objects.get(pk=pk).get_status_display()}")
            else:
                index.queue(document, reread=options["reread"])
        self.stdout.write(f"{'Indexed' if options['now'] else 'Queued'} {len(pks)} documents.")

    def status(self):
        from django.db.models import Count

        from auctions.models import DocumentChunk

        by_status = dict(Document.objects.values_list("status").annotate(count=Count("pk")))
        self.stdout.write("Documents: " + (", ".join(f"{count} {name}" for name, count in by_status.items()) or "none"))
        self.stdout.write(f"Stale (would be queued): {index.stale_documents().count()}")
        current = index.embedding_model_name()
        if current:
            missing = DocumentChunk.objects.exclude(embedding_model=current).count()
            self.stdout.write(f"Passages without a {current} vector: {missing} of {DocumentChunk.objects.count()}")
            vector = index.query_vector("library status check")
            self.stdout.write("Embeddings: " + ("working" if vector is not None else "FAILING, search is keyword-only"))
        else:
            self.stdout.write("Embeddings: off (LLM_EMBEDDING_MODEL is blank), search is keyword-only")
        failed = Document.objects.filter(status=Document.FAILED).values_list("pk", "error")[:20]
        for pk, error in failed:
            self.stdout.write(f"  failed {pk}: {error}")
