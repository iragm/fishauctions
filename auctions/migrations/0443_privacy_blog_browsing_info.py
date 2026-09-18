"""Add browsing/IP tracking to the privacy policy's "information we store" list.

Done as a targeted replacement, the way 0420 did it, rather than pasting the whole post again:
0355 already holds the full current text, and a second full copy is a second chance for the file
and the database to disagree. A replacement that finds nothing is a no-op, which is the right
failure if a site has edited the post by hand.

The policy listed name/email/phone/mailing address/location as the things this site keeps track
of, but said nothing about the IP address and pages-viewed logging that ``PageView`` does on every
page load (see its docstring in models.py) -- the same table the admin dashboard's active-user
stats are built from. One bullet, kept short on purpose: this is baseline traffic logging any
visitor should expect, not something that needs its own section.
"""

from django.db import migrations

BLOG_REPLACEMENTS = [
    (
        "- Your location\n\nWe don't collect or store any credit card information.",
        "- Your location\n\n"
        "- Browsing information like pages viewed and your IP address\n\n"
        "We don't collect or store any credit card information.",
    ),
]


def _rerender(post):
    """BlogPostView renders ``body_rendered``; the historical MarkdownField won't regenerate it."""
    from markdownfield.rendering import render_markdown
    from markdownfield.validators import VALIDATOR_STANDARD

    post.body_rendered = render_markdown(post.body, VALIDATOR_STANDARD)


def forwards(apps, schema_editor):
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
        ("auctions", "0442_userdata_running_total_tip_sent_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
