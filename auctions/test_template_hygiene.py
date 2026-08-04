"""Guards against template tags that render as text instead of being parsed.

The bug this exists to prevent: ``{# … #}`` (and ``{% … %}``, and ``{{ … }}``) must open and
close on the same line, because Django's lexer has no ``re.DOTALL``. Spread one over two lines
and Django doesn't recognise it — it copies the whole thing, delimiters included, onto the page.
Nothing raises, nothing logs, and it reaches production looking like a developer note left in
the middle of the club page. It has happened several times; this makes it fail the build instead.

The rule is checked against the template source rather than rendered output on purpose: rendering
every template with the context it expects isn't practical, and the mistake is visible in the
file either way.
"""

from pathlib import Path

from django.test import SimpleTestCase

from auctions import template_lint

REPO_ROOT = Path(__file__).resolve().parent.parent


class TemplateTagsAreParseableTests(SimpleTestCase):
    def test_no_template_tag_spans_two_lines(self):
        findings = template_lint.check_templates(REPO_ROOT)
        if findings:
            report = "\n".join(
                f"  {path.relative_to(REPO_ROOT)}:{number}: {message}" for path, number, message in findings
            )
            self.fail(
                f"{len(findings)} template tag(s) would render onto the page as text instead of being parsed:\n{report}"
            )

    def test_the_checker_actually_looks_at_this_repo_s_templates(self):
        """A checker pointed at nothing would pass for ever without anyone noticing."""
        found = template_lint.iter_template_files(REPO_ROOT)
        self.assertGreater(len(found), 100, "expected to find the site's templates")
        self.assertIn(REPO_ROOT / "auctions" / "templates" / "base.html", found)


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
