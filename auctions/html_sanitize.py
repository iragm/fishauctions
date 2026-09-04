"""Sanitizing the rich text people paste into Summernote.

Auction rules, lot descriptions and blog posts are all edited in Summernote, which means the HTML
reaching the database is whatever the browser -- or whatever the person pasted -- produced. This
strips it down to the formatting the site actually renders.

The tag rule is an **allowlist**, not a blocklist, because a blocklist cannot be complete: `<svg>`
and `<math>` open a foreign parsing context that browsers handle differently from HTML, which is the
basis of mutation-XSS, and new elements keep arriving. A disallowed tag is unwrapped so its text
survives; a disallowed tag on ``UNSAFE_SUMMERNOTE_TAGS`` is removed with everything inside it,
because its contents are code, foreign content, or a raw-text context rather than words.

Attribute rules: every ``on*`` handler goes; the URI-bearing attributes are checked for script and
local-file schemes with the whitespace attackers use to split them stripped first; ``color`` and
``background-color`` go because the site picks its own colours; anything with ``url()`` in it goes
so stored content cannot fetch from elsewhere.

Lives here rather than in ``models.py`` only because that file is at its size ceiling. It has no
model dependencies, and both ``models.py`` and ``forms.py`` import it.
"""

import re

from bs4 import BeautifulSoup

# Tags Summernote legitimately emits for rich-text formatting. Anything not on this
# allowlist is stripped. An allowlist (unlike the previous fixed blocklist) can't be
# bypassed by novel or foreign elements -- e.g. <svg>/<math>, which open a foreign
# parsing context that browsers use for mutation-XSS and which no blocklist enumerates
# completely.
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

# Disallowed tags whose *contents* must also be dropped (not just the tag itself): these
# carry executable code, foreign (SVG/MathML) or embedded/external content, or raw-text
# parsing contexts that mutation-XSS relies on. Any other disallowed tag is unwrapped so
# its plain text survives.
UNSAFE_SUMMERNOTE_TAGS = frozenset(
    {
        "applet", "audio", "base", "canvas", "embed", "form", "frame", "frameset",
        "iframe", "img", "link", "map", "math", "meta", "noembed", "noscript",
        "object", "param", "plaintext", "script", "source", "style", "svg",
        "template", "textarea", "title", "track", "video", "xmp",
    }
)  # fmt: skip


def sanitize_summernote_html(text):
    """Remove disallowed Summernote content while preserving supported formatting."""
    if text is None:
        return None
    if text == "":
        return ""

    soup = BeautifulSoup(text, "html.parser")

    # Enforce the tag allowlist. ``find_all(True)`` yields tags in document order (parents
    # before children), so decomposing a parent marks its descendants ``decomposed`` and we
    # skip them below. Executable/foreign tags are removed with their subtree; any other
    # unexpected tag is unwrapped so its text content is preserved.
    for tag in soup.find_all(True):
        if getattr(tag, "decomposed", False):
            continue
        name = (tag.name or "").lower()
        if name in ALLOWED_SUMMERNOTE_TAGS:
            continue
        if name in UNSAFE_SUMMERNOTE_TAGS:
            tag.decompose()
        else:
            tag.unwrap()

    for tag in soup.find_all():
        for attr_name, attr_value in list(tag.attrs.items()):
            normalized_attr = attr_name.lower()
            if normalized_attr.startswith("on"):
                del tag[attr_name]
                continue
            # These are the URI-bearing attributes we allow in Summernote content.
            if normalized_attr in {"href", "src", "xlink:href"}:
                # Some parsers represent multi-valued attributes as lists, so normalize both cases.
                values = attr_value if isinstance(attr_value, list) else [attr_value]
                if any(
                    isinstance(value, str)
                    # Block URI schemes commonly used for script execution or local file access in user HTML,
                    # even when attackers split the scheme name with ASCII whitespace/control characters.
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

    # Clean style attributes: remove color/background-color (unwanted formatting) and any
    # property containing url() which could load external resources.
    for tag in soup.find_all(style=True):
        styles = tag["style"].split(";")
        cleaned_styles = []
        for style in styles:
            if not style.strip():
                continue
            name, *value_parts = style.split(":", 1)
            prop = name.strip().lower()
            value = value_parts[0] if value_parts else ""
            if prop in {"color", "background-color"}:
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
    """Compatibility wrapper for legacy callers that now performs full Summernote sanitization."""
    return sanitize_summernote_html(text)
