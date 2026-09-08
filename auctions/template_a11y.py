"""Two accessibility rules a template cannot break twice, checked against template source.

The site had ten ``<img>`` with no ``alt`` and a handful of icon-only buttons with no accessible
name.  Both were fixed once; without something failing the build they come back, because neither
one is visible when you look at the page -- that is what makes them accessibility bugs rather than
rendering bugs.  This is the same shape as :mod:`auctions.template_lint`: pure stdlib, no Django
import, so one implementation serves the unit test, the lint script and the pre-commit hook.

**Every ``<img>`` needs an ``alt``.**  Including a decorative one, which needs ``alt=""`` -- the
empty string is a decision ("skip this"), a missing attribute makes a screen reader read the file
name instead.  There is no way to tell the two apart from the outside, which is why the rule is
"present" rather than "non-empty".

**A control whose only content is an icon needs a name.**  ``<button><i class="bi bi-trash"></i>
</button>`` is announced as "button" and nothing else: the icon is a font glyph with no text.
``aria-label`` or ``title`` supplies one.  A control with a text label as well is fine and is not
reported, which is why this only fires on controls that contain *nothing but* an icon.

What this deliberately does **not** do is parse HTML.  Templates are not HTML -- half these tags
have a ``{% if %}`` inside the attribute list -- so an HTML parser either rejects them or silently
reinterprets them.  These are two narrow regexes over the source, and the cost of that is that
they are conservative: they find the shapes that are actually written here, and a sufficiently
strange one gets past.  A rule that catches the ten real cases and fails the build on the eleventh
is worth more than a correct parser nobody can run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from auctions.template_lint import iter_template_files

# `<img ...>`, with the attribute list captured. Non-greedy to the first `>`, which is wrong for an
# attribute value containing a literal `>` -- there are none, and a Django tag inside an attribute
# does not contain one either.
IMG = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE)
ALT = re.compile(r"\balt\s*=", re.IGNORECASE)

# A <button> or <a> whose entire content is one icon element: <i>, <span> or <svg> carrying a
# Bootstrap Icons class. DOTALL because these are routinely written over three lines.
ICON_ONLY = re.compile(
    r"<(?P<tag>button|a)\b(?P<attrs>[^>]*)>\s*"
    r"<(?P<icon>i|span|svg)\b[^>]*\bclass=\"[^\"]*\bbi[\b-][^\"]*\"[^>]*>\s*</(?P=icon)>\s*"
    r"</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
NAMED = re.compile(r"\b(aria-label|aria-labelledby|title)\s*=", re.IGNORECASE)

IMG_MESSAGE = (
    '<img> with no alt attribute. A screen reader reads the file name instead. Use alt="" if the '
    "image is decorative -- the empty string is the decision to skip it, a missing attribute is not."
)
ICON_MESSAGE = (
    'This control contains only an icon, so it is announced as "button" and nothing else. Add '
    'aria-label="..." (and aria-hidden="true" on the icon).'
)


def check_text(text):
    """Return ``[(line_number, message)]`` for one template's contents."""
    problems = []
    for match in IMG.finditer(text):
        if not ALT.search(match.group("attrs")):
            problems.append((text[: match.start()].count("\n") + 1, IMG_MESSAGE))
    for match in ICON_ONLY.finditer(text):
        if not NAMED.search(match.group("attrs")):
            problems.append((text[: match.start()].count("\n") + 1, ICON_MESSAGE))
    return sorted(problems)


def check_templates(root):
    """Return ``[(path, line_number, message)]`` for every template under ``root``."""
    findings = []
    for path in iter_template_files(root):
        for number, message in check_text(path.read_text(encoding="utf-8", errors="replace")):
            findings.append((path, number, message))
    return findings


def main(argv=None):
    """Print anything found and exit non-zero, so this works as a lint step."""
    argv = list(sys.argv[1:] if argv is None else argv)
    roots = [Path(arg) for arg in argv] or [Path(__file__).resolve().parent.parent]
    findings = [finding for root in roots for finding in check_templates(root)]
    for path, number, message in findings:
        sys.stderr.write(f"{path}:{number}: {message}\n")
    if findings:
        sys.stderr.write(f"\n{len(findings)} accessibility problem(s).\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
