"""Guards the two accessibility rules in auctions/template_a11y.py.

Both were fixed once by hand -- ten images with no alt, five icon-only controls with no name -- and
both are invisible when you look at the page, so nothing but a failing build stops them coming
back. These tests are the failing build.
"""

from pathlib import Path

from django.test import SimpleTestCase

from auctions import template_a11y

REPO_ROOT = Path(__file__).resolve().parent.parent


class TemplatesAreAccessibleTests(SimpleTestCase):
    def test_no_image_is_missing_alt_text(self):
        findings = [row for row in template_a11y.check_templates(REPO_ROOT) if "alt" in row[2]]
        if findings:
            report = "\n".join(f"  {path.relative_to(REPO_ROOT)}:{number}" for path, number, _ in findings)
            self.fail(f"{len(findings)} <img> with no alt attribute:\n{report}")

    def test_no_icon_only_control_is_unnamed(self):
        findings = [row for row in template_a11y.check_templates(REPO_ROOT) if "only an icon" in row[2]]
        if findings:
            report = "\n".join(f"  {path.relative_to(REPO_ROOT)}:{number}" for path, number, _ in findings)
            self.fail(f"{len(findings)} icon-only control(s) with no accessible name:\n{report}")

    def test_the_checker_actually_looks_at_this_repo_s_templates(self):
        """A checker pointed at nothing would pass for ever without anyone noticing."""
        found = template_a11y.iter_template_files(REPO_ROOT)
        self.assertGreater(len(found), 100)
        self.assertIn(REPO_ROOT / "auctions" / "templates" / "base.html", found)


class CheckerBehaviourTests(SimpleTestCase):
    def test_an_image_with_no_alt_is_reported(self):
        self.assertEqual(len(template_a11y.check_text('<img src="fish.png">')), 1)

    def test_an_empty_alt_is_a_decision_and_is_accepted(self):
        """alt="" says "decorative, skip it". A missing attribute says nothing at all."""
        self.assertEqual(template_a11y.check_text('<img src="fish.png" alt="">'), [])

    def test_an_alt_built_from_a_template_variable_is_accepted(self):
        self.assertEqual(template_a11y.check_text('<img src="{{ x }}" alt="{{ lot.lot_name }}">'), [])

    def test_an_icon_only_button_is_reported(self):
        self.assertEqual(len(template_a11y.check_text('<button><i class="bi bi-trash"></i></button>')), 1)

    def test_an_icon_only_button_spread_over_three_lines_is_still_reported(self):
        markup = '<button type="submit" class="btn">\n  <i class="bi bi-x-lg"></i>\n</button>'
        self.assertEqual(len(template_a11y.check_text(markup)), 1)

    def test_an_aria_label_names_it(self):
        markup = '<button aria-label="Delete this lot"><i class="bi bi-trash"></i></button>'
        self.assertEqual(template_a11y.check_text(markup), [])

    def test_a_title_names_it_too(self):
        markup = '<button title="Delete this lot"><i class="bi bi-trash"></i></button>'
        self.assertEqual(template_a11y.check_text(markup), [])

    def test_an_icon_next_to_real_text_is_not_reported(self):
        """The rule is about controls with *nothing but* an icon; a label makes it announceable."""
        markup = '<button><i class="bi bi-trash"></i> Delete</button>'
        self.assertEqual(template_a11y.check_text(markup), [])

    def test_an_icon_only_link_is_reported_as_well_as_a_button(self):
        markup = '<a href="/x/"><i class="bi bi-gear"></i></a>'
        self.assertEqual(len(template_a11y.check_text(markup)), 1)

    def test_the_line_number_points_at_the_problem(self):
        markup = "one\ntwo\n<img src='x.png'>\n"
        self.assertEqual(template_a11y.check_text(markup)[0][0], 3)
