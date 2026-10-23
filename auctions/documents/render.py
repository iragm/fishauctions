"""A document's Markdown as the HTML its page shows, with its own pictures placed where the text puts them.

The text is whatever an upload, a model or a person wrote, so the HTML is cut down to an allowlist after
Markdown has made it, the way :mod:`auctions.html_sanitize` treats rich text. A picture in the text is
``![caption](/library/picture/<pk>/)`` -- the ``picture_url`` ``read_document`` hands back -- and only a
picture of this document survives: any other image becomes its words, so the page never fetches from
somewhere else.
"""

import re
from urllib.parse import urlparse

import bleach
import markdown
from bs4 import BeautifulSoup
from django.urls import reverse
from django.utils.safestring import mark_safe

_TAGS = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
        "li", "ol", "p", "pre", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
    }
)  # fmt: skip
_ATTRIBUTES = {"a": ["href", "title"], "img": ["src", "alt", "title"], "td": ["align"], "th": ["align"]}
_PICTURE_PATH = re.compile(r"^/library/picture/(\d+)/$")
_PICTURE_IN_TEXT = re.compile(r"/library/picture/(\d+)/")


def to_html(text: str, pictures) -> str:
    """``text`` as safe HTML, with ``pictures`` (this document's) shown where it names them."""
    html = markdown.markdown(text or "", extensions=["tables", "sane_lists", "fenced_code"])
    html = bleach.clean(html, tags=_TAGS, attributes=_ATTRIBUTES, protocols=["http", "https", "mailto"], strip=True)
    soup = BeautifulSoup(html, "html.parser")
    by_pk = {picture.pk: picture for picture in pictures}
    for image in soup.find_all("img"):
        found = _PICTURE_PATH.match(urlparse(image.get("src", "")).path)
        picture = by_pk.get(int(found.group(1))) if found else None
        if picture is None:
            image.replace_with(image.get("alt", ""))
            continue
        image.attrs = {
            "src": reverse("document_picture", args=[picture.pk]),
            "alt": image.get("alt") or picture.caption or "Picture",
            "class": "library-picture",
            "loading": "lazy",
        }
    for link in soup.find_all("a"):
        link["rel"] = "nofollow noopener"
    for table in soup.find_all("table"):
        table["class"] = "table table-sm"
    return mark_safe(str(soup))  # noqa: S308 -- built from the allowlist above


def unplaced_pictures(text: str, pictures) -> list:
    """The pictures the text doesn't place, which the page shows together above it."""
    placed = {int(pk) for pk in _PICTURE_IN_TEXT.findall(text or "")}
    return [picture for picture in pictures if picture.pk not in placed]
