"""Sanitizing the rich text people paste into Summernote.

Auction rules, lot descriptions and blog posts are all edited in Summernote, so the HTML reaching the
database is whatever the browser -- or whatever was pasted -- produced. This strips it to the
formatting the site renders.

The tag rule is an **allowlist**, because a blocklist cannot be complete: `<svg>` and `<math>` open a
foreign parsing context browsers handle differently from HTML, which is the basis of mutation-XSS,
and new elements keep arriving. A disallowed tag is unwrapped so its text survives; one on
``UNSAFE_SUMMERNOTE_TAGS`` is removed with its contents, which are code or foreign content.

Attributes are an **allowlist** too, for the same reason: checking only ``on*`` and three URI
attributes left every other attribute to ride through, so the rule was only ever as complete as the
list of attributes somebody had thought of. What survives is in ``ALLOWED_SUMMERNOTE_ATTRIBUTES``;
the URI-bearing ones are then checked for script and local-file schemes, with the whitespace
attackers use to split a scheme name stripped first. ``style`` is narrowed again to
``ALLOWED_STYLE_PROPERTIES`` -- ``color`` and ``background-color`` are out because the site picks its
own, ``position`` and ``z-index`` because stored content must not cover the page showing it -- and
anything with ``url()`` goes so stored content cannot fetch from elsewhere.

Here rather than in ``models.py`` because it has no model dependencies and both ``models.py`` and
``forms.py`` import it.
"""

import re

from bs4 import BeautifulSoup

# Tags Summernote legitimately emits. Anything else is stripped; an allowlist can't be bypassed by
# novel or foreign elements the way the old blocklist could.
ALLOWED_SUMMERNOTE_TAGS = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "col",
        "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em", "figcaption",
        "figure", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins",
        "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span",
        "strike", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
        "thead", "time", "tr", "u", "ul", "var",
    }
)  # fmt: skip

# Disallowed tags whose *contents* go too: executable code, foreign (SVG/MathML) or embedded
# content, and raw-text parsing contexts mutation-XSS relies on. Any other disallowed tag is
# unwrapped so its text survives.
UNSAFE_SUMMERNOTE_TAGS = frozenset(
    {
        "applet", "audio", "base", "canvas", "embed", "form", "frame", "frameset",
        "iframe", "img", "link", "map", "math", "meta", "noembed", "noscript",
        "object", "param", "plaintext", "script", "source", "style", "svg",
        "template", "textarea", "title", "track", "video", "xmp",
    }
)  # fmt: skip


#: Attributes allowed to survive on an allowed tag. An allowlist for the same reason the tag rule is
#: one: only ``on*`` and the three URI attributes were ever inspected, so every other attribute rode
#: through untouched and the rule could only ever be as complete as the list of attributes we thought
#: of. ``style`` is filtered further below.
ALLOWED_SUMMERNOTE_ATTRIBUTES = frozenset(
    {"href", "src", "alt", "title", "style", "class", "colspan", "rowspan", "start", "type", "datetime", "cite"}
)

#: CSS properties Summernote's toolbar produces. Everything else goes: ``position``/``z-index`` alone
#: let stored content cover the page it is displayed on.
ALLOWED_STYLE_PROPERTIES = frozenset(
    {
        "font-weight", "font-style", "font-size", "font-family", "text-decoration", "text-decoration-line",
        "text-align", "vertical-align", "line-height", "margin", "margin-left", "margin-right", "margin-top",
        "margin-bottom", "padding", "padding-left", "padding-right", "padding-top", "padding-bottom",
        "width", "height", "border", "border-collapse", "list-style-type",
    }
)  # fmt: skip


def sanitize_summernote_html(text, allowed_tags=None):
    """Remove disallowed Summernote content while preserving supported formatting.

    *allowed_tags* narrows the tag allowlist for a caller that renders somewhere tighter than a page
    -- a printed label wants ``{"br"}`` and nothing else.
    """
    if text is None:
        return None
    if text == "":
        return ""

    allowed_tags = ALLOWED_SUMMERNOTE_TAGS if allowed_tags is None else frozenset(allowed_tags)
    soup = BeautifulSoup(text, "html.parser")

    # Enforce the tag allowlist. ``find_all(True)`` yields tags in document order, so decomposing a
    # parent marks its descendants ``decomposed`` and they are skipped below.
    for tag in soup.find_all(True):
        if getattr(tag, "decomposed", False):
            continue
        name = (tag.name or "").lower()
        if name in allowed_tags:
            continue
        if name in UNSAFE_SUMMERNOTE_TAGS:
            tag.decompose()
        else:
            tag.unwrap()

    for tag in soup.find_all():
        for attr_name, attr_value in list(tag.attrs.items()):
            normalized_attr = attr_name.lower()
            if normalized_attr not in ALLOWED_SUMMERNOTE_ATTRIBUTES:
                # Covers every ``on*`` handler, and everything else nobody has had to think of yet.
                del tag[attr_name]
                continue
            # The URI-bearing attributes allowed in Summernote content.
            if normalized_attr in {"href", "src", "xlink:href"}:
                # Some parsers represent multi-valued attributes as lists.
                values = attr_value if isinstance(attr_value, list) else [attr_value]
                if any(
                    isinstance(value, str)
                    # Schemes used for script execution or local file access, even when the scheme
                    # name is split with whitespace or control characters.
                    and re.match(
                        r"^(?:data|file|javascript|vbscript):",
                        re.sub(r"[\x00-\x20\x7f]+", "", value),
                        flags=re.IGNORECASE,
                    )
                    for value in values
                ):
                    del tag[attr_name]

    # Remove 'color' attribute from <font> tags
    for tag in soup.find_all("font"):
        if tag.has_attr("color"):
            del tag["color"]

    # Clean style attributes: remove color and background-color, and any property containing url().
    for tag in soup.find_all(style=True):
        styles = tag["style"].split(";")
        cleaned_styles = []
        for style in styles:
            if not style.strip():
                continue
            name, *value_parts = style.split(":", 1)
            prop = name.strip().lower()
            value = value_parts[0] if value_parts else ""
            if prop not in ALLOWED_STYLE_PROPERTIES:
                # color and background-color are excluded from the list: the site picks its own.
                continue
            if "url(" in value.lower():
                continue
            cleaned_styles.append(style)
        if cleaned_styles:
            tag["style"] = ";".join(cleaned_styles)
        else:
            del tag["style"]

    return str(soup)


def remove_html_color_tags(text):
    """Compatibility wrapper for legacy callers; performs full Summernote sanitization."""
    return sanitize_summernote_html(text)
