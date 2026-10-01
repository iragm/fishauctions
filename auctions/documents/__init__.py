"""The library: documents people upload, read into text, and searched from ``/library/`` and ``/mcp/``.

Most of what goes in is decades old -- BAP reports and club articles from the 1970s and 80s, scanned
badly -- so the images *are* the documents more often than not, and reading them is the main path,
not an edge case.

Every stage is rebuilt from the one before it, and the original file is kept, so a better parser or
embedding model next year is a version bump and ``manage.py reindex_documents``, never a re-upload:

1. **file** -> **markdown** (:mod:`.extract`): ``markitdown`` for every format it knows; scanned PDF
   pages and pictures are transcribed by the site's vision model, one cached answer per image.
   ``PARSER_VERSION``.
2. **markdown** -> **chunks** (:mod:`.index`): split at headings and paragraphs, with character
   offsets back into the text. ``CHUNKER_VERSION``.
3. **chunks** -> **vectors** (:mod:`.index`): ``LLM_EMBEDDING_MODEL``, stored per chunk with the
   model's name; a chunk whose text and model haven't changed keeps its vector.
4. **tags** (:mod:`.index`): species by scientific name, and :data:`.models.TOPICS` with title,
   author and year from one model call, filling only what nobody typed.

:mod:`.search` holds the one visibility rule, hybrid keyword + vector ranking, and the answer the
web page writes. ``/mcp/`` gets the passages and the full text, never that answer: the caller is a
model and will outgrow ours.

**A pile of pages** (:mod:`.stitch`): upload pictures or scanned PDFs as pages and they are read one
by one, then put back together into one document per article, across page breaks, with adverts and
membership lists left out. ``DocumentBatch`` / ``BatchPage``.

**Running it**: reading happens on the ``celery_documents`` compose service (queue ``documents``),
which ``update.sh`` starts with everything else. Uploads live in ``./privatefiles/``
(``DOCUMENT_ROOT``), not ``mediafiles/``, because nginx serves all of ``/media/``: it is gitignored,
so it is in the host's snapshots and nowhere else. ``manage.py reindex_documents --status`` says how
the library stands; the nightly ``tidy_library`` task brings documents up to date with a new model or
parser a hundred at a time.

This file stays docstring-only: ``auctions/models.py`` imports :mod:`.models`, and anything imported
here would load first.
"""
