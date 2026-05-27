# Generated manually: remove "direct your reply to" instructions from email templates.
# When SES routing is active, replies go to the auction address automatically via
# Lambda; the manual "Make sure to direct your reply to …" footer is no longer needed.
import re

from django.db import migrations

# Patterns to strip, in order of specificity (most specific first so we don't
# leave orphaned tags).
_PATTERNS = [
    # HTML with <small> wrapper
    re.compile(r"\n*<small>Make sure to direct your reply to \{\{reply_to_email\}\}</small>", re.IGNORECASE),
    # HTML inline (inside an existing <small> block, e.g. join_auction_reminder)
    re.compile(r"<br><br>Make sure to direct your reply to \{\{reply_to_email\}\}", re.IGNORECASE),
    # Plain-text variant
    re.compile(r"\n\nMake sure to direct your reply to \{\{reply_to_email\}\}", re.IGNORECASE),
]


def _strip(text):
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub("", text)
    return text


def remove_reply_to_instructions(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    updated = 0
    for tmpl in EmailTemplate.objects.all():
        new_content = _strip(tmpl.content)
        new_html = _strip(tmpl.html_content)
        if new_content != tmpl.content or new_html != tmpl.html_content:
            tmpl.content = new_content
            tmpl.html_content = new_html
            tmpl.save(update_fields=["content", "html_content"])
            updated += 1


def reverse_func(apps, schema_editor):
    # Re-adding the footer to all templates would be overly fragile;
    # a manual rollback via the admin panel is preferred.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0279_club_auction_email_member_and_more"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.RunPython(remove_reply_to_instructions, reverse_func),
    ]
