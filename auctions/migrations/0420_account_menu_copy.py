"""Copy that pointed at the old preferences page, and one email that no longer has a reason to exist.

Three unrelated-looking edits with one cause: the preferences ribbon became the Account setup menu,
and the notification settings moved off /preferences/ onto /notifications/ (see
``auctions/account_nav.py``). Anything that told a reader where to go had to move with them.

The blog edit is done as targeted replacements rather than by pasting the whole post again, the way
0353 and 0355 did: the post is long, those two migrations already rewrote it in full, and a fourth
full copy is a fourth chance for the file and the database to disagree. A replacement that finds
nothing is a no-op, which is the right failure if a site has edited the post by hand.
"""

from django.db import migrations

# Emails that told people where to turn these emails off. Both settings named here are on the
# notifications form now; /preferences/ still exists and would have been a page without them on it.
EMAIL_LINK_REPLACEMENTS = [
    ("https://{{ domain }}/preferences/", "https://{{ domain }}/notifications/"),
    ("https://{{domain}}/preferences/", "https://{{domain}}/notifications/"),
]

# Nothing sends this any more: the @ symbol has been refused in usernames since
# `auctions.validators.validate_username_no_at_symbol`, so no new account can be in the state it
# warns about, and everyone who was in it was written to years ago.
RETIRED_TEMPLATES = ["username_is_email"]

BLOG_REPLACEMENTS = [
    (
        "You can delete your account at any time from [your preferences](/preferences/) &mdash; "
        "there's a *Delete account* link under *More*, and it works the same way in the app.",
        "You can [delete your account](/account/delete/) at any time &mdash; it's the last item in "
        "the *Account setup* menu, and it works the same way in the app.",
    ),
    (
        "- If your username is an email address, that *will* be visible to non-logged-in users, and "
        "you'll probably get spam.  You'll get an email recommending that you "
        "[change your username](/username/), which is likely the reason you're reading this page.",
        "- Usernames can no longer contain an @ symbol, so a new one can't be an email address.  If "
        "you signed up long enough ago that yours is, it *will* be visible to non-logged-in users "
        "and you'll probably get spam &mdash; [change your username](/username/).",
    ),
]


def _rerender(post):
    """BlogPostView renders ``body_rendered``; the historical MarkdownField won't regenerate it."""
    from markdownfield.rendering import render_markdown
    from markdownfield.validators import VALIDATOR_STANDARD

    post.body_rendered = render_markdown(post.body, VALIDATOR_STANDARD)


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.all():
        changed = False
        for field in ("content", "html_content"):
            text = getattr(template, field) or ""
            for old, new in EMAIL_LINK_REPLACEMENTS:
                if old in text:
                    text = text.replace(old, new)
                    changed = True
            if changed:
                setattr(template, field, text)
        if changed:
            template.save()
    # The loop above walks every row, translations included: post_office keeps a translated body on
    # its own row rather than falling back to the parent, so a link on one would otherwise survive.
    EmailTemplate.objects.filter(name__in=RETIRED_TEMPLATES).delete()

    BlogPost = apps.get_model("auctions", "BlogPost")
    for post in BlogPost.objects.filter(slug="privacy"):
        body = post.body or ""
        for old, new in BLOG_REPLACEMENTS:
            body = body.replace(old, new)
        if body != post.body:
            post.body = body
            _rerender(post)
            post.save()


def backwards(apps, schema_editor):
    """The links go back; the deleted template does not.

    Reversing is for a rollback of the code that moved these pages, and the retired email has no
    code left to send it in either direction -- 0175 is where its text lives if it is ever wanted.
    """
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.all():
        changed = False
        for field in ("content", "html_content"):
            text = getattr(template, field) or ""
            for old, new in EMAIL_LINK_REPLACEMENTS:
                if new in text:
                    text = text.replace(new, old)
                    changed = True
            if changed:
                setattr(template, field, text)
        if changed:
            template.save()

    BlogPost = apps.get_model("auctions", "BlogPost")
    for post in BlogPost.objects.filter(slug="privacy"):
        body = post.body or ""
        for old, new in BLOG_REPLACEMENTS:
            body = body.replace(new, old)
        if body != post.body:
            post.body = body
            _rerender(post)
            post.save()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0419_taptopayattempt"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
