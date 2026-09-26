"""``format_html`` is never called with nothing to format, and ``static_html`` only ever takes a literal.

The first is deprecated in Django 5 and a TypeError from 6.0, on a code path nobody may run until
the upgrade. The second is what makes ``static_html`` safe: it is ``mark_safe`` without the S308
lint, so a variable reaching it would be exactly the stored XSS that rule exists to catch.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase
from django.utils.safestring import SafeString

from auctions.helper_functions import static_html

REPO = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("auctions", "fishauctions")


def _called_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def misuses(source):
    """``(line, problem)`` for every no-argument ``format_html`` and non-literal ``static_html`` call."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        unpacked = any(isinstance(arg, ast.Starred) for arg in node.args)
        if name == "format_html" and len(node.args) <= 1 and not node.keywords and not unpacked:
            found.append((node.lineno, "format_html() with nothing to format: use static_html()"))
        elif name == "static_html" and not (
            len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append((node.lineno, "static_html() takes one string literal: use format_html()"))
    return found


def _python_files():
    for directory in SOURCE_DIRS:
        yield from (REPO / directory).rglob("*.py")


class StaticHtmlTests(SimpleTestCase):
    def test_no_misuse_anywhere(self):
        report = []
        for path in _python_files():
            for line, problem in misuses(path.read_text()):
                report.append(f"{path.relative_to(REPO)}:{line}: {problem}")
        self.assertEqual(report, [], "\n".join(report))

    def test_the_scan_reaches_the_call_sites(self):
        """A scan that found no files would pass forever."""
        tables = (REPO / "auctions" / "tables.py").read_text()
        self.assertIn(REPO / "auctions" / "tables.py", set(_python_files()))
        self.assertIn("static_html(", tables)

    def test_it_is_safe(self):
        self.assertIsInstance(static_html("<br>"), SafeString)


class StaticHtmlCheckerTests(SimpleTestCase):
    def test_a_bare_format_html_is_caught(self):
        self.assertEqual(len(misuses("format_html('<br>')")), 1)
        self.assertEqual(len(misuses("html.format_html('')")), 1)

    def test_format_html_with_a_value_is_fine(self):
        self.assertEqual(misuses("format_html('<b>{}</b>', name)"), [])
        self.assertEqual(misuses("format_html('<b>{n}</b>', n=name)"), [])

    def test_static_html_with_a_variable_is_caught(self):
        self.assertEqual(len(misuses("static_html(name)")), 1)
        self.assertEqual(len(misuses("static_html(f'<b>{name}</b>')")), 1)

    def test_static_html_with_a_literal_is_fine(self):
        self.assertEqual(misuses("static_html('<br>')"), [])
