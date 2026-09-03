"""This site's own source code, read out of the public repository it is published from.

The tool on the other end of this (``read_source``, in :mod:`auctions.palette_actions`) exists for
one question: **how does this actually work?** Every other tool on ``/mcp/`` answers out of the
database -- what a lot sold for, who has paid, when the meeting is -- and none of them can say why a
lot got no breeder points, what "pretty much over" means, or how lots get recommended to somebody.
The answers to those are written down in exactly one place, and it is a public repository, so an
agent asked the question can read the same lines a maintainer would.

**The whole repository is fetched as one archive, and everything is answered out of that.** That is
the design decision worth knowing, because the obvious alternative -- GitHub's contents API for
listings and the raw CDN for files -- cannot search the *code*, only filenames, and "how does the
lot recommendation system work" is not a filename. GitHub's code search API is the other obvious
answer and it refuses anonymous callers outright, so it would have made this feature depend on a
credential. ``codeload.github.com`` needs none, the archive is 4.5 MB for this repository and
arrives in about a second, and one fetch an hour then answers listings, filename searches, file
reads and a genuine content grep with no further network at all.

**What it can serve is bounded by what is already published, and that is the whole security
argument.** Nothing here touches a filesystem path -- not one. The archive is read in memory, a
manifest of what is in it is built, and every path is resolved against that manifest, so the tool
can only ever hand back a file that is already on a public web page. That matters more than it
looks: on this deployment the source is bind-mounted into the container next to ``.env``, a Google
Wallet keyfile and the logs, and a "read a file off disk" tool with an allowlist of directories
would be one forgotten entry away from serving a database password. ``.env`` is gitignored, so it is
not in the archive, so as far as this module is concerned it does not exist.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tarfile
import time
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TIMEOUT = 30

#: How long the downloaded archive is worth keeping. An hour is chosen from how often the answer
#: changes rather than from how often it is asked: a file that appeared five minutes ago is not what
#: somebody asking how a feature works is asking about.
ARCHIVE_CACHE_SECONDS = 3600

#: How long one process keeps the *extracted* archive before throwing it away. A conversation asks
#: several questions in a row and re-extracting for each one is wasted work; holding ten megabytes
#: per worker for an hour to save a fifth of a second is not. Five minutes is the middle.
MEMO_SECONDS = 300

#: Refuse an archive bigger than this rather than pulling it into memory. This repository's is about
#: 4.5 MB compressed; the ceiling is for a fork whose repository is full of video.
MAX_ARCHIVE_BYTES = 80_000_000

#: A file bigger than this is listed but never has its text kept.
MAX_TEXT_FILE_BYTES = 2_000_000

#: How much of a file one call hands back. Two bounds rather than one, because lines and characters
#: are both real: :data:`auctions.mcp.tools.MAX_RESULT_CHARS` is 20,000 and a result that busts it
#: is refused wholesale, so the character bound is the one that keeps this from ever being the
#: reason a call comes back empty. Both are said in the answer, with how to ask for the next page.
DEFAULT_LINES = 120
MAX_LINES = 400
MAX_CHARS = 12000

#: How many paths a filename search names at once.
MAX_MATCHES = 40

#: How many lines a content search returns, and how many of them may come from one file. The
#: per-file cap is what stops a word that appears ninety times in ``models.py`` filling the answer
#: with one file when the interesting thing is *which files* it is in.
MAX_GREP_MATCHES = 30
MAX_GREP_PER_FILE = 4
GREP_LINE_CHARS = 220

#: What counts as text. An extension list rather than sniffing, because the answer has to be the
#: same every time for the same repository, and because a file this misses is still listed and
#: still readable through its own path -- it just does not take part in a content search.
TEXT_SUFFIXES = (
    ".py",
    ".html",
    ".htm",
    ".js",
    ".mjs",
    ".css",
    ".scss",
    ".md",
    ".txt",
    ".rst",
    ".sh",
    ".bash",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".in",
    ".json",
    ".csv",
    ".sql",
    ".example",
    ".conf",
    ".env",
    ".xml",
    ".svg",
    ".gitignore",
    ".dockerignore",
    "dockerfile",
    "makefile",
    "license",
)


class SourceUnavailable(Exception):
    """The repository could not be reached, or is not configured. Carries the sentence to say."""


#: What a network problem and a bad response say. Two sentences rather than one, because "couldn't
#: reach it" and "reached it and it wouldn't answer" are different things for whoever reads the log.
UNREACHABLE = "I couldn't reach the source code repository just now."
UNREADABLE = "I couldn't read the source code repository just now."

#: The extracted archive, held per process. ``{"stamp": …, "sizes": …, "text": …, "at": …}``.
_MEMO: dict[str, Any] = {}


def configured() -> bool:
    """Whether this deployment publishes its source. Blank ``SOURCE_CODE_URL`` turns the tool off."""
    return bool(repository())


def repository() -> tuple[str, str] | None:
    """``("iragm", "fishauctions")`` from the configured URL, or ``None``.

    Only GitHub is understood. That is not a limitation worth engineering around: the setting names
    one repository, and a fork that lives somewhere else can point the tool at nothing and lose a
    feature it never had.
    """
    raw = (getattr(settings, "SOURCE_CODE_URL", "") or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


def branch() -> str:
    return (getattr(settings, "SOURCE_CODE_BRANCH", "") or "master").strip()


def home_url() -> str:
    """The repository's own page -- what an answer links to when it can't link to a file."""
    owner_repo = repository()
    if not owner_repo:
        return ""
    return f"https://github.com/{owner_repo[0]}/{owner_repo[1]}"


def blob_url(path: str, start: int = 0, end: int = 0) -> str:
    """The GitHub page for one file, with the lines this answer quoted anchored on it."""
    owner_repo = repository()
    if not owner_repo:
        return ""
    url = f"https://github.com/{owner_repo[0]}/{owner_repo[1]}/blob/{branch()}/{path}"
    if start:
        url += f"#L{start}"
        if end and end != start:
            url += f"-L{end}"
    return url


def _cache_key(kind: str, value: str = "") -> str:
    owner_repo = repository() or ("", "")
    stamp = f"{owner_repo[0]}/{owner_repo[1]}@{branch()}:{value}"
    return "source_" + kind + "_" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:32]


def _archive() -> bytes:
    """The repository as one gzipped tar, from ``codeload``. Cached; one download an hour."""
    owner_repo = repository()
    if not owner_repo:
        message = "This site doesn't publish its source code."
        raise SourceUnavailable(message)
    key = _cache_key("archive")
    cached = cache.get(key)
    if cached is not None:
        return cached
    url = f"https://codeload.github.com/{owner_repo[0]}/{owner_repo[1]}/tar.gz/{branch()}"
    try:
        response = requests.get(url, headers={"User-Agent": "auction-site-read-source"}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Could not download the source archive: %s", exc)
        raise SourceUnavailable(UNREACHABLE) from exc
    if response.status_code != 200:
        logger.warning("Source archive request returned %s", response.status_code)
        raise SourceUnavailable(UNREADABLE)
    blob = response.content
    if not blob:
        raise SourceUnavailable(UNREADABLE)
    if len(blob) > MAX_ARCHIVE_BYTES:
        message = "The source code repository is too big to read here."
        raise SourceUnavailable(message)
    cache.set(key, blob, timeout=ARCHIVE_CACHE_SECONDS)
    return blob


def _is_text(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(TEXT_SUFFIXES) or lowered.rsplit("/", 1)[-1] in ("dockerfile", "makefile", "license")


def _extract(blob: bytes) -> tuple[dict[str, int], dict[str, str]]:
    """``(sizes, text)`` out of the archive. Read in memory; nothing is written anywhere.

    GitHub wraps the tree in a single top-level directory named for the commit, which is stripped
    so paths read the way the repository spells them.
    """
    sizes: dict[str, int] = {}
    text: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                path = member.name.split("/", 1)[1] if "/" in member.name else member.name
                if not path:
                    continue
                sizes[path] = member.size
                if not _is_text(path) or member.size > MAX_TEXT_FILE_BYTES:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                try:
                    text[path] = handle.read().decode("utf-8")
                except (UnicodeDecodeError, OSError):
                    # Listed, still readable by path, just not searchable. An SVG with a stray byte
                    # in it is not worth failing the whole extraction over.
                    continue
    except (tarfile.TarError, EOFError) as exc:
        logger.warning("Could not read the source archive: %s", exc)
        raise SourceUnavailable(UNREADABLE) from exc
    if not sizes:
        message = "The source code repository came back empty."
        raise SourceUnavailable(message)
    return sizes, text


def _loaded() -> tuple[dict[str, int], dict[str, str]]:
    """The extracted repository, memoised per process for :data:`MEMO_SECONDS`."""
    blob = _archive()
    stamp = hashlib.sha256(blob).hexdigest()[:16]
    now = time.monotonic()
    if _MEMO.get("stamp") == stamp and now - _MEMO.get("at", 0) < MEMO_SECONDS:
        return _MEMO["sizes"], _MEMO["text"]
    sizes, text = _extract(blob)
    _MEMO.clear()
    _MEMO.update({"stamp": stamp, "sizes": sizes, "text": text, "at": now})
    return sizes, text


def forget() -> None:
    """Drop the process-local copy. For tests, and for anything that wants the memory back."""
    _MEMO.clear()


def tree() -> dict[str, int]:
    """Every file in the repository, path -> size in bytes.

    This is the allowlist. Nothing else in this module answers about a path that is not a key here,
    which is what makes ``..``, an absolute path and a secret sitting beside the source all the same
    kind of nothing: they are not in the repository, so they do not resolve.
    """
    return _loaded()[0]


def normalize(path: str) -> str:
    """A path as the repository spells it. Leading slashes and ``./`` go; nothing is resolved."""
    cleaned = (path or "").strip().strip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def exists(path: str) -> bool:
    return normalize(path) in tree()


def listing(path: str = "") -> dict[str, Any]:
    """What is directly inside one directory: its files with sizes, and its subdirectories."""
    prefix = normalize(path)
    files = tree()
    if prefix and prefix in files:
        message = f"{prefix} is a file, not a directory."
        raise ValueError(message)
    scope = prefix + "/" if prefix else ""
    directories: set[str] = set()
    here: list[dict[str, Any]] = []
    for candidate, size in files.items():
        if scope and not candidate.startswith(scope):
            continue
        rest = candidate[len(scope) :]
        if "/" in rest:
            directories.add(rest.split("/", 1)[0])
        else:
            here.append({"name": rest, "bytes": size})
    return {
        "directories": sorted(directories),
        "files": sorted(here, key=lambda row: row["name"]),
    }


def find(query: str, limit: int = MAX_MATCHES) -> list[str]:
    """Paths whose name or directory contains *query*. The filename half of a search."""
    wanted = (query or "").strip().lower()
    if not wanted:
        return []
    scored: list[tuple[int, str]] = []
    for candidate in tree():
        lowered = candidate.lower()
        if wanted not in lowered:
            continue
        name = lowered.rsplit("/", 1)[-1]
        # A hit on the filename beats a hit further up the path, and an exact filename beats both.
        rank = 0 if name == wanted or name.startswith(wanted + ".") else (1 if wanted in name else 2)
        scored.append((rank, candidate))
    scored.sort(key=lambda row: (row[0], len(row[1]), row[1]))
    return [path for _rank, path in scored[:limit]]


#: Where a match ranks, in order. The ranking is the difference between this being useful and being
#: a keyword grep: "recommend" appears in ``Dockerfile``'s ``--no-install-recommends`` and in four
#: markdown headings, and a flat search spends its whole budget on those before reaching the code
#: that does the recommending. So a line that *defines* something by that name comes first, then a
#: file whose path says it is about it, then the application's own Python, then the design notes,
#: then everything else, and last the files that match nearly every word in the codebase.
_DEFINES = "def |class |DEFINE"
_APP_PYTHON = re.compile(r"^(auctions|fishauctions)/.*\.py$")
_NOTES = re.compile(r"\.(md|rst|txt)$")
_LOW_PRIORITY = re.compile(r"(^|/)(test_[^/]+|tests?)\.py$|(^|/)(vendor|migrations|node_modules)/")

RANK_DEFINITION = 0
RANK_PATH = 1
RANK_APP_CODE = 2
RANK_NOTES = 3
RANK_OTHER = 4
RANK_LOW = 5


def _rank(path: str, line: str, wanted: str) -> int:
    """Where one matching line sits in the order above."""
    if _LOW_PRIORITY.search(path):
        return RANK_LOW
    stripped = line.lstrip()
    if stripped.startswith(("def ", "class ", "async def ")) and wanted in stripped.lower():
        return RANK_DEFINITION
    if wanted in path.lower():
        return RANK_PATH
    if _APP_PYTHON.match(path):
        return RANK_APP_CODE
    if _NOTES.search(path):
        return RANK_NOTES
    return RANK_OTHER


def grep(query: str, limit: int = MAX_GREP_MATCHES) -> list[dict[str, Any]]:
    """Lines of the repository's own code containing *query*, case-insensitively, best first.

    The reason this module downloads an archive instead of asking GitHub for one file at a time.
    "How does the lot recommendation system work" is not a filename, and until an agent could grep
    the code the only way to answer it was to read ``views.py`` a hundred and twenty lines at a
    time. Substring rather than regex, deliberately: the caller is a language model writing a
    search box query, not a maintainer, and an unanchored regex over ten megabytes is a way to
    spend a request.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        return []
    _sizes, text = _loaded()
    hits: list[tuple[int, str, int, dict[str, Any]]] = []
    for path in sorted(text):
        body = text[path]
        if wanted not in body.lower():
            continue
        found = 0
        for number, line in enumerate(body.splitlines(), start=1):
            if wanted not in line.lower():
                continue
            hits.append(
                (
                    _rank(path, line, wanted),
                    path,
                    number,
                    {
                        "path": path,
                        "line": number,
                        "text": line.strip()[:GREP_LINE_CHARS],
                    },
                )
            )
            found += 1
            if found >= MAX_GREP_PER_FILE:
                break
    hits.sort(key=lambda row: (row[0], row[1], row[2]))
    return [hit for _rank_value, _path, _number, hit in hits[:limit]]


def read(path: str, start: int = 1, count: int = DEFAULT_LINES) -> dict[str, Any]:
    """A page of one file, numbered from 1, bounded by both lines and characters.

    Returns the text with its line numbers on it. Numbered because that is what makes the answer
    checkable -- "``auctions/models.py`` line 4120" is something a person can go and look at, and a
    quoted paragraph with no line on it is something they have to search for.
    """
    wanted = normalize(path)
    sizes, text = _loaded()
    if wanted not in sizes:
        message = f"There's no file called {wanted} in the repository."
        raise ValueError(message)
    if wanted not in text:
        message = (
            f"{wanted} isn't text I can read here."
            if sizes[wanted] <= MAX_TEXT_FILE_BYTES
            else f"{wanted} is too big to read here ({sizes[wanted] // 1024} KB)."
        )
        raise ValueError(message)
    lines = text[wanted].splitlines()
    total = len(lines)
    start = max(1, start)
    count = max(1, min(count, MAX_LINES))
    chunk = lines[start - 1 : start - 1 + count]
    kept: list[str] = []
    used = 0
    for offset, line in enumerate(chunk):
        rendered = f"{start + offset}\t{line}"
        if used + len(rendered) + 1 > MAX_CHARS and kept:
            break
        kept.append(rendered)
        used += len(rendered) + 1
    # Nothing kept means ``start`` is past the end of the file. It has to report "no more", or an
    # agent paging through hands back the same ``next_line`` it was just given, forever.
    end = start + len(kept) - 1 if kept else start - 1
    more = bool(kept) and end < total
    return {
        "path": wanted,
        "lines": total,
        "showing": f"{start}-{end}" if kept else "nothing",
        "text": "\n".join(kept),
        "more": more,
        "next_line": end + 1 if more else None,
        "url": blob_url(wanted, start, end) if kept else blob_url(wanted),
    }


#: Files worth naming to somebody who has just arrived and does not know the shape of this
#: repository. Read off nothing -- it is a hand-written signpost, and it is short on purpose.
LANDMARKS = (
    ("docs/module_map.md", "One line per module: what it is and what it defines. Start here."),
    ("CLAUDE.md", "How the site is built and the rules that apply everywhere."),
    ("auctions/models.py", "Every model, and most of the rules that matter."),
    ("auctions/views/", "The pages, split by area. Its __init__.py says which module holds what."),
    ("auctions/palette_actions.py", "The action registry behind /mcp/ and the command palette."),
    ("auctions/mcp/", "The MCP server itself: tools, protocol, transport, auth, resources."),
)
