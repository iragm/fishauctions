# Generated manually to update the privacy blog post with mobile app / push notification details
from django.db import migrations


def update_privacy_blog_post(apps, schema_editor):
    # BlogPostView renders body_rendered (not body). The historical MarkdownField in a data migration
    # doesn't regenerate body_rendered on save (its rendered_field is dropped by deconstruct), so we
    # render the HTML explicitly here and write both fields. Earlier privacy migrations (0219, 0236)
    # only set body, so their text never actually appeared on the page — writing body_rendered now
    # also repairs that: privacy_content below is the full, current text.
    from markdownfield.rendering import render_markdown
    from markdownfield.validators import VALIDATOR_STANDARD

    BlogPost = apps.get_model("auctions", "BlogPost")

    privacy_content = """It's pretty important to know who you're sharing your personal information with.  This site keeps track of the following pieces of information:

- Your name

- Your email address

- Your phone number

- Your mailing address

- Your location

We don't collect or store any credit card information.  For the most part, your contact information isn't shared with anyone else on the site.  Here are the *only* times your information is shared with other users:

- Your email address is visible to all users on [your contact page](/account/), unless you hide it in [preferences](/preferences/).  Only signed in users can see any of your info.

- Your username is visible whenever you place a bid.  You can uncheck *Username Visible* in [preferences](/preferences/), which will hide your username when you bid.  Even if this is unchecked, your username will always be shown when you sell a lot or make a chat message.  (It's worth mentioning that when your username is hidden, you're still not completely anonymous.  Auction admins can still see your username, and, behind the scenes, a unique identifier is used and visible in the page source to other bidders.)

- If your username is an email address, that *will* be visible to non-logged-in users, and you'll probably get spam.  You'll get an email recommending that you [change your username](/username/), which is likely the reason you're reading this page.

- When you confirm your pickup location for an auction, all of your contact information is made available to the organizer of the auction.  This is visible to them even if you don't sell or buy any lots in the auction.

- When you sell a lot that is part of an auction, your real name is given to the winner of that lot.

- When you win a lot that is part of an auction, your real name is given to the seller of that lot.

- When you sell a lot that is *not* part of an auction, all your contact information is given to the winner of that lot.

- When you win a lot that is *not* part of an auction, all your contact information is given to the seller of that lot.

### On-site payments

When you pay for an invoice using PayPal or Square on this site, we store a record of your payment including:

- Your name

- Your email address

- Your mailing address (as provided by the payment processor)

- The amount paid and currency

- A transaction ID from the payment processor

We do *not* store any credit card numbers, bank account details, or other sensitive payment credentials.  All payment processing is handled securely by PayPal or Square.

When auction organizers connect their PayPal or Square accounts to receive payments, we store their merchant IDs and authentication tokens (encrypted at rest for Square).  This allows us to process payments on their behalf without storing any buyer payment credentials.

### Mobile app and notifications

If you use our mobile app, we store some information about your device so the app can work and notify you:

- The device name, platform (iOS or Android), and the app version

- A push notification token, which we send to Google's Firebase Cloud Messaging service so it can deliver notifications to your device

Push notifications are optional.  You choose whether to receive them, and you can turn them off at any time in the app or in your device's settings.  When you sign out of the app, the push token for that device is cleared, so a signed-out phone never receives your notifications.  We only use these tokens to deliver the notifications you've asked for.

The app can also take card payments in person using Square's Mobile Payments SDK &mdash; for example, when you pay an auction organizer at an in-person event.  As with online payments, the card is processed securely by Square and we never store card numbers or other sensitive payment credentials.

### Updating your contact information

When you [update your contact information](/contact_info/), the changes will also be applied to any auctions you've recently joined and to any clubs you're a member of.  This ensures that auction organizers, club admins, sellers, and buyers have your current information.  A record of the change is kept in the auction and club history for the organizers' reference.

### Club memberships

If you join a fish club through this site or are added to a club by a club admin, the club will have access to:

- Your name

- Your email address

- Your phone number

- Your mailing address

This information is visible to club members with the appropriate admin permissions.  You can check and update your contact information at any time through [your contact info page](/contact_info/).

Club admins may export member lists.  If you do not want your contact information shared in these exports, contact the club admin to update your contact preferences.

### Law enforcement and security

We have never had any personal information requests from law enforcement, and we'll remove this message if we receive one.

We take your privacy and security seriously.  If you see something that doesn't seem right, reach out and we'll fix it."""

    BlogPost.objects.update_or_create(
        slug="privacy",
        defaults={
            "title": "Privacy",
            "body": privacy_content,
            "body_rendered": render_markdown(privacy_content, VALIDATOR_STANDARD),
            "extra_js": "",
        },
    )


def reverse_func(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0325_alter_auction_tax_alter_invoice_status"),
    ]

    operations = [
        migrations.RunPython(update_privacy_blog_post, reverse_func),
    ]
