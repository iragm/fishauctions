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
