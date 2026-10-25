"""Data migrations meet prod's rows, not a fresh database's, and one that raises stops the deploy
(entrypoint.sh won't start on a failed migrate). These run forward steps on what a hand-edited site can hold.
"""

from importlib import import_module

from django.apps import apps
from django.test import TestCase
from post_office.models import EmailTemplate

from auctions.models import CommandPalettePage


class DoubledRowsTests(TestCase):
    """Nothing in the schema stops an admin saving a second row of the same name."""

    def test_email_templates_are_rewritten_when_a_name_is_doubled(self):
        EmailTemplate.objects.create(name="invoice_ready", language="", subject="A copy")
        import_module("auctions.migrations.0475_email_templates_match_the_migrations").forwards(apps, None)
        subjects = EmailTemplate.objects.filter(name="invoice_ready", language="").values_list("subject", flat=True)
        self.assertEqual(len(subjects), 2)
        self.assertEqual(len(set(subjects)), 1)

    def test_palette_rows_are_refreshed_when_doubled(self):
        key = {"search_term": "print barcodes", "target": "clubs:barcode_labels", "url": ""}
        CommandPalettePage.objects.create(**key)
        import_module("auctions.migrations.0487_seed_command_palette_paddles").seed(apps, None)
        synonyms = CommandPalettePage.objects.filter(**key).values_list("synonyms", flat=True)
        self.assertEqual(len(synonyms), 2)
        self.assertTrue(all("paddle stickers" in row for row in synonyms))


class PrintReminderLinkTests(TestCase):
    def test_print_reminder_links_the_bagging_guide(self):
        # The test database is migrated, so 0504 has already run on the seeded template.
        template = EmailTemplate.objects.get(name="auction_print_reminder", language="")
        for body in (template.content, template.html_content):
            self.assertIn("/help/bagging-fish/", body)
            self.assertNotIn("/blog/transporting-fish/", body)


class RetiredBlogEmailTests(TestCase):
    """The test database is migrated, so 0508 has already run on the seeded templates."""

    def test_no_seeded_email_links_the_blog(self):
        for template in EmailTemplate.objects.all():
            for body in (template.content, template.html_content):
                self.assertNotIn("/blog/", body or "", template.name)

    def test_invoices_email_drops_the_paypal_post_and_keeps_the_rest(self):
        template = EmailTemplate.objects.get(name="auction_invoices", language="")
        for body in (template.content, template.html_content):
            self.assertIn("make changes or adjustments", body)

    def test_moved_posts_point_at_their_help_section(self):
        template = EmailTemplate.objects.get(name="auction_first", language="")
        self.assertIn("/help/run-an-online-auction/#invite", template.html_content)


class RetiredBlogEditedTemplateTests(TestCase):
    """0508 on what an admin could have saved, not what 0475 seeded."""

    def forwards(self):
        import_module("auctions.migrations.0508_emails_link_the_help_not_the_blog").forwards(apps, None)

    def test_a_reworded_paypal_bullet_keeps_its_words_and_links_the_help(self):
        EmailTemplate.objects.filter(name="auction_invoices").delete()
        html = (
            "{% block content %}<ul><li>Import into PayPal: "
            '<a href="https://{{ domain }}/blog/online-payments-suck/">here</a></li><li>Keep me</li></ul>'
            "{% endblock %}"
        )
        EmailTemplate.objects.create(name="auction_invoices", language="", content="x", html_content=html)
        self.forwards()
        template = EmailTemplate.objects.get(name="auction_invoices")
        self.assertIn("/help/payments/#paypal", template.html_content)
        self.assertIn("Keep me", template.html_content)
        self.assertIn("{% endblock %}", template.html_content)

    def test_a_doubled_template_is_rewritten_in_both_rows(self):
        EmailTemplate.objects.create(
            name="auction_first", language="", content="https://{{ domain }}/blog/encouraging-participation/"
        )
        self.forwards()
        for template in EmailTemplate.objects.filter(name="auction_first"):
            self.assertNotIn("/blog/", template.content)
