"""Guards against the template mistakes that produce a wrong page without an error.

The bug this exists to prevent: ``{# … #}`` (and ``{% … %}``, and ``{{ … }}``) must open and
close on the same line, because Django's lexer has no ``re.DOTALL``. Spread one over two lines
and Django doesn't recognise it — it copies the whole thing, delimiters included, onto the page.
Nothing raises, nothing logs, and it reaches production looking like a developer note left in
the middle of the club page. It has happened several times; this makes it fail the build instead.

The rule is checked against the template source rather than rendered output on purpose: rendering
every template with the context it expects isn't practical, and the mistake is visible in the
file either way.
"""

import contextlib
import io
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from auctions import template_lint

REPO_ROOT = Path(__file__).resolve().parent.parent


class TemplateTagsAreParseableTests(SimpleTestCase):
    def test_no_template_renders_a_wrong_page(self):
        findings = template_lint.check_templates(REPO_ROOT)
        if findings:
            report = "\n".join(
                f"  {path.relative_to(REPO_ROOT)}:{number}: {message}" for path, number, message in findings
            )
            self.fail(f"{len(findings)} template problem(s) that would render a wrong page:\n{report}")

    def test_the_checker_actually_looks_at_this_repo_s_templates(self):
        """A checker pointed at nothing would pass for ever without anyone noticing."""
        found = template_lint.iter_template_files(REPO_ROOT)
        self.assertGreater(len(found), 100, "expected to find the site's templates")
        self.assertIn(REPO_ROOT / "auctions" / "templates" / "base.html", found)

    def test_one_template_can_be_checked_on_its_own(self):
        """The edit hook in `.claude/hooks/` passes a single file, not the tree.

        A file path used to match no ``templates`` directory and be reported clean whatever was in
        it, so the hook that runs at every template edit passed on a broken template every time.
        """
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.html"
            broken.write_text("<p>{# a note that\n   spilled over #}</p>\n", encoding="utf-8")
            self.assertEqual(template_lint.iter_template_files(broken), [broken])
            self.assertTrue(template_lint.check_templates(broken))
            with contextlib.redirect_stderr(io.StringIO()):  # main() reports to stderr; the suite doesn't need it
                self.assertEqual(template_lint.main([str(broken)]), 1)

    def test_a_file_that_is_not_a_template_is_skipped_rather_than_walked(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "thing.py"
            script.write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(template_lint.iter_template_files(script), [])


class TemplateLintTests(SimpleTestCase):
    """The checker itself, so a refactor can't quietly turn it into a no-op."""

    def test_a_single_line_comment_is_fine(self):
        self.assertEqual(template_lint.check_text("{# all on one line #}\n<p>hi</p>"), [])

    def test_a_comment_split_over_two_lines_is_caught(self):
        problems = template_lint.check_text("{# this note got\n   too long #}\n")
        self.assertEqual([number for number, _ in problems], [1, 2])
        self.assertIn("never closed", problems[0][1])
        self.assertIn("closes nothing", problems[1][1])

    def test_a_split_block_tag_is_caught(self):
        problems = template_lint.check_text('{% include "x.html"\n   with a=1 %}\n')
        self.assertEqual(problems[0][0], 1)

    def test_a_split_variable_is_caught(self):
        problems = template_lint.check_text("{{ event.title\n   |title }}\n")
        self.assertEqual(problems[0][0], 1)

    def test_several_tags_on_one_line_are_all_read(self):
        self.assertEqual(template_lint.check_text("{% if a %}{{ b }}{% endif %}{# note #}"), [])

    def test_the_last_tag_on_a_busy_line_is_still_checked(self):
        problems = template_lint.check_text("{% if a %}{{ b }}{# unclosed note\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("never closed", problems[0][1])

    def test_javascript_braces_are_not_mistaken_for_tags(self):
        """Minified JS and CSS end nested blocks with '}}' constantly — never flag those."""
        self.assertEqual(template_lint.check_text("<script>f({a:{b:1}});x=function(){return {}}</script>"), [])
        self.assertEqual(template_lint.check_text("<style>#a{color:red}#b{top:0}</style>"), [])

    def test_verbatim_blocks_are_left_alone(self):
        """Emitting braces literally is the whole point of verbatim."""
        self.assertEqual(template_lint.check_text("{% verbatim %}\n{{ handlebars\n{% endverbatim %}\n"), [])

    def test_an_orphan_closer_is_caught(self):
        """What's left when someone deletes the opening line of a multi-line comment."""
        problems = template_lint.check_text("<p>hi</p>\n   and the rest of the note #}\n")
        self.assertEqual(problems[0][0], 2)
        self.assertIn("closes nothing", problems[0][1])


class OneModalContainerPerPageTests(SimpleTestCase):
    """Only base.html may declare ``id="modals-here"``.

    A second element with that id is not a cosmetic duplicate. htmx resolves ``hx-target`` with
    ``querySelector``, which returns whichever comes first in document order, so two containers take
    turns being the target -- and because a modal response used to be able to *replace* its
    container, opening a modal destroyed one of them. Two clicks emptied the page of both, and the
    third opened nothing at all. This had been reported, and chased, as a bug in the modal code
    three times.
    """

    def test_every_template_uses_the_inherited_container(self):
        findings = [
            (path, number, message)
            for path, number, message in template_lint.check_templates(REPO_ROOT)
            if template_lint.MODAL_CONTAINER in message
        ]
        self.assertEqual(findings, [], f"templates declaring a second modal container: {findings}")

    def test_base_html_is_allowed_to_declare_it(self):
        path = REPO_ROOT / "auctions" / "templates" / "base.html"
        text = path.read_text(encoding="utf-8")
        self.assertIn(template_lint.MODAL_CONTAINER, text, "base.html should render the modal container")
        self.assertEqual(template_lint.check_modal_container(path, text), [])

    def test_a_second_container_anywhere_else_is_caught(self):
        path = REPO_ROOT / "auctions" / "templates" / "some_page.html"
        problems = template_lint.check_modal_container(path, '<p>hi</p>\n<div id="modals-here"></div>\n')
        self.assertEqual([number for number, _ in problems], [2])
        self.assertIn("already rendered by base.html", problems[0][1])

    def test_pointing_at_the_container_is_not_declaring_one(self):
        """Every modal trigger on the site names it in hx-target; only rendering one is the problem."""
        path = REPO_ROOT / "auctions" / "templates" / "some_page.html"
        self.assertEqual(
            template_lint.check_modal_container(path, '<a hx-get="/x/" hx-target="#modals-here">go</a>'), []
        )
