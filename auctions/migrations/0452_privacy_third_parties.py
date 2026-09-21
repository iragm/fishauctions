"""Say who else sees your information, correct the email-visibility line, and give the logging a purpose.

Three targeted replacements, the way 0443 and 0420 did it rather than pasting the whole post again:
0355 holds the full current text and a second copy is a second chance for the file and the database
to disagree. A replacement that finds nothing is a no-op, which is the right failure if a site has
edited the post by hand.

* **Other services we use.** The policy named the payment processors and Firebase and stopped there,
  while the site also hands data to SES, Mailchimp/Brevo, reCAPTCHA, Google Maps, Cloudflare Images,
  Discord, the two wallet providers and a language model. The model is the one a member would not
  guess: text they typed leaves the site. Analytics and advertising are deliberately absent -- the
  template blocks for those are dead on this deployment, and a policy that describes tracking nobody
  runs is as wrong as one that omits tracking somebody does.

* **Email visibility.** The policy said an address is visible unless you hide it. ``UserData
  .email_visible`` defaults to ``False``, so it is hidden unless you show it. Wrong in the safe
  direction, but a notice that doesn't match the code undermines the parts that do.

* **Why the browsing log is kept.** 0443 added the bullet; this says what it is for, which is the
  part a member (and a state privacy statute) actually cares about.
"""

from django.db import migrations

THIRD_PARTIES = """### Other services we use

Running this site means handing small pieces of your information to companies that each do one job with it.  None of them are given it for their own purposes:

- Amazon SES sends the site's email, so it handles your email address and the message.

- Mailchimp or Brevo hold a club's mailing list, if that club uses one: your name and email address.

- Google's reCAPTCHA checks that a sign-up isn't a robot, Google Maps turns an address into a location on the map, and Firebase delivers push notifications to the app.  Signing in with Google, Apple or Facebook tells us only what their screen says it will.

- Cloudflare stores and resizes the photos on lots.

- Discord receives a club's announcement, if the club has connected a Discord server.

- Apple Wallet and Google Wallet hold a membership card you have chosen to add to your phone, with your name and member number on it.

- A language model provider (currently OpenAI) handles text where the site writes or reads something for you: matching a lot to a species, drafting a club's request for a donation, summarizing a reply, and answering what you type into the command palette.  It sees only the text involved in that one job, under API terms that say it isn't used to train their models, and nothing is sent unless you use one of those features.

"""

BLOG_REPLACEMENTS = [
    # Fix the direction of the email-visibility rule.
    (
        "- Your email address is visible to all users on [your contact page](/account/), unless you hide it "
        "in [preferences](/preferences/).  Only signed in users can see any of your info.",
        "- Your email address is hidden until you choose to show it on [your contact page](/account/), "
        "which you can do in [preferences](/preferences/).  Only signed in users can see any of your info.",
    ),
    # Say what the browsing log is for.
    (
        "- Browsing information like pages viewed and your IP address",
        "- Browsing information like pages viewed and your IP address, which we keep so we can see "
        "which parts of the site people struggle with and make them easier to use",
    ),
    # Name the rest of the processors, just before the closing section.
    (
        "### Law enforcement and security",
        THIRD_PARTIES + "### Law enforcement and security",
    ),
]


def _rerender(post):
    """BlogPostView renders ``body_rendered``; the historical MarkdownField won't regenerate it."""
    from markdownfield.rendering import render_markdown
    from markdownfield.validators import VALIDATOR_STANDARD

    post.body_rendered = render_markdown(post.body, VALIDATOR_STANDARD)


def _apply(apps, pairs):
    BlogPost = apps.get_model("auctions", "BlogPost")
    for post in BlogPost.objects.filter(slug="privacy"):
        body = post.body or ""
        for old, new in pairs:
            body = body.replace(old, new)
        if body != post.body:
            post.body = body
            _rerender(post)
            post.save()


def forwards(apps, schema_editor):
    _apply(apps, BLOG_REPLACEMENTS)


def backwards(apps, schema_editor):
    _apply(apps, [(new, old) for old, new in BLOG_REPLACEMENTS])


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0451_email_footer_on_every_template"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
