"""Catches Django template tags that silently render as text instead of being parsed.

Django's lexer is ``({%.*?%}|{{.*?}}|{#.*?#})`` with no ``re.DOTALL``, so **every** template
tag has to open and close on the same line. A tag spread over two lines isn't a syntax error and
nothing warns about it — Django simply doesn't recognise it and copies it to the output, where
members read it. The usual way this happens is a comment that grew too long for one line::

    {# Subscribing, not downloading: the plain .ics link is a one-time import that never
       updates again, so it's the last item rather than the button. #}

which puts that entire note, ``#}`` and all, on the club page. Use ``{% comment %} … {% endcomment %}``
for anything that needs more than one line.

Pure stdlib and no Django import, so this runs three ways off one implementation: as a unit test
(``auctions/test_template_hygiene.py``), from the lint script, and as a pre-commit hook —
``python3 -m auctions.template_lint``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Opener -> closer. All three are line-bound; see the module docstring.
TAG_PAIRS = {"{%": "%}", "{{": "}}", "{#": "#}"}

# Balanced tags, so what's left on a line is anything unpaired.
BALANCED_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}")

# Closers worth reporting on their own, for the line an unclosed opener spills onto (and for the
# orphan left behind when someone deletes the opening line of a multi-line comment). "}}" is
# missing on purpose: minified JavaScript and CSS end nested blocks with it all the time.
ORPHAN_CLOSERS = ("%}", "#}")

TEMPLATE_SUFFIXES = (".html", ".txt")


def iter_template_files(root):
    """Every template under ``root``, found by directory name rather than by asking Django.

    Keeps this importable without settings configured, and picks up templates in any app.

    ``root`` may also be a single **file**, so the same entry point serves the whole tree (CI) and
    one template (the edit hook in ``.claude/hooks/``). Without that, a file path here matched no
    ``templates`` directory and the checker reported nothing at all -- a lint that always passes,
    which is worse than not having one.
    """
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix in TEMPLATE_SUFFIXES else []
    return sorted(
        path
        for templates_dir in root.rglob("templates")
        if templates_dir.is_dir()
        for path in templates_dir.rglob("*")
        if path.is_file() and path.suffix in TEMPLATE_SUFFIXES
    )


def check_text(text):
    """Return ``[(line_number, message)]`` for one template's contents.

    ``{% verbatim %}`` blocks are skipped: their whole job is to emit braces literally, so an
    unpaired one in there is deliberate rather than a mistake.
    """
    problems = []
    in_verbatim = False
    for number, line in enumerate(text.splitlines(), 1):
        if "{% verbatim" in line:
            in_verbatim = True
        if "{% endverbatim" in line:
            in_verbatim = False
            continue
        if in_verbatim:
            continue

        position = 0
        while position < len(line):
            found = [(line.find(opener, position), opener) for opener in TAG_PAIRS]
            found = [(at, opener) for at, opener in found if at != -1]
            if not found:
                break
            at, opener = min(found)
            closer = TAG_PAIRS[opener]
            closes_at = line.find(closer, at + len(opener))
            if closes_at == -1:
                problems.append(
                    (
                        number,
                        f"'{opener}' is never closed with '{closer}' on this line. Django only "
                        f"matches tags within a single line, so this renders onto the page as "
                        f"text. Use {{% comment %}} … {{% endcomment %}} for multi-line notes.",
                    )
                )
                break
            position = closes_at + len(closer)

        leftover = BALANCED_TAG.sub("", line)
        for orphan in ORPHAN_CLOSERS:
            if orphan in leftover:
                problems.append(
                    (
                        number,
                        f"'{orphan}' here closes nothing on this line, so it renders onto the "
                        f"page as text. It's usually the tail of a tag that was opened on an "
                        f"earlier line.",
                    )
                )
    return problems


def check_templates(root):
    """Return ``[(path, line_number, message)]`` for every template under ``root``."""
    findings = []
    for path in iter_template_files(root):
        for number, message in check_text(path.read_text(encoding="utf-8", errors="replace")):
            findings.append((path, number, message))
    return findings


def main(argv=None):
    """Print anything found and exit non-zero, so this works as a lint step.

    Each argument is a directory to walk or a single template to check; with none, the repository.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    roots = [Path(arg) for arg in argv] or [Path(__file__).resolve().parent.parent]
    findings = [finding for root in roots for finding in check_templates(root)]
    for path, number, message in findings:
        sys.stderr.write(f"{path}:{number}: {message}\n")
    if findings:
        sys.stderr.write(f"\n{len(findings)} unparsed template tag(s) would render as text.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
